#!/usr/bin/env python3
"""Sheetsの作業コピーを取得・比較し、確認済みの変更行を書き戻す。"""

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cloud_backend.local.storage import atomic_write
from plugin.scenario_table import SheetSelector
from tools.sheets_api import ApiError, SheetsClient
from plugin.tsv_values import (
    content_hash, local_values, normalize, read_base_values, read_tsv, write_tsv,
)


MAX_BATCH_BYTES = 1_800_000
SYNC_FILE = 'sheets-sync.json'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parse_args(argv):
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('pull', 'diff', 'push'):
        child = commands.add_parser(name)
        child.add_argument('--settings', help='設定YAML。--bot指定時の既定はsettings.yaml')
        child.add_argument('--bot')
        child.add_argument('--sheet-id')
        child.add_argument('--credentials', help='明示したサービスアカウントJSONのpath')
        child.add_argument('--sheet', action='append', default=[], help='対象シート名（複数指定可）')
        if name == 'pull':
            child.add_argument('--output', required=True, help='新しく作る作業ディレクトリ')
            child.add_argument('--all-sheets', action='store_true', help='除外・選択条件によらず全GRIDシートを取得')
        else:
            child.add_argument('--manifest', required=True)
        if name == 'push':
            child.add_argument('--dry-run', action='store_true', help='diffと同じ読み取りだけを行う')
            child.add_argument('--verify', action='store_true', help='書き込み応答の確認に加えて再取得する')
            child.add_argument('--backup-dir', default='outputs/sheets-backup')
    return parser.parse_args(argv)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def _read_json(path):
    def invalid(value):
        raise ValueError(f'JSONに有限でない数値があります: {path}')
    return json.loads(path.read_text(encoding='utf-8'), parse_constant=invalid)


def _save_json(path, value):
    atomic_write(path, _json_bytes(value))


def _base_path(root, sheet_id):
    return root / '.sheets-sync' / f'base-{sheet_id}.json'


def _save_base(root, sheet_id, rows):
    _save_json(_base_path(root, sheet_id), {
        'sha256': content_hash(rows), 'values': rows,
    })


def _read_base(root, sheet_id):
    return read_base_values(_base_path(root, sheet_id))


def _inside(root, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError('manifestのpathが不正です')
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('同期TSVのpathは作業ディレクトリ内の相対pathにしてください')
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('同期するTSVはmanifestと同じディレクトリ内に置いてください')
    return path


def _working_copy(manifest):
    manifest = Path(manifest).resolve()
    root = manifest.parent
    metadata = _read_json(root / SYNC_FILE)
    if metadata.get('schema_version') != 1 or not isinstance(metadata.get('spreadsheet_id'), str):
        raise ValueError('同期情報が不正です。pullで作成した作業コピーを指定してください')
    entries = metadata.get('sheets')
    if not isinstance(entries, list):
        raise ValueError('同期シート一覧が不正です')
    by_name, ids = {}, set()
    for entry in entries:
        name, sheet_id = entry.get('name'), entry.get('sheet_id')
        if (not isinstance(name, str) or not name or name in by_name
                or type(sheet_id) is not int or sheet_id < 0 or sheet_id in ids):
            raise ValueError('同期シート名またはsheetIdが不正・重複しています')
        if entry.get('path') != f'sheet-{sheet_id}.tsv':
            raise ValueError('同期TSVのpathはpullで作成した名前を維持してください')
        _inside(root, entry['path'])
        by_name[name] = entry
        ids.add(sheet_id)
    declared = _read_json(manifest)
    if not isinstance(declared, dict) or set(declared) != {'sheets'} or not isinstance(declared['sheets'], list):
        raise ValueError('manifestにはsheets配列だけを指定してください')
    selected, seen = [], set()
    for entry in declared['sheets']:
        if not isinstance(entry, dict) or set(entry) != {'name', 'path'}:
            raise ValueError('manifestの各シートにはnameとpathを指定してください')
        name = entry['name']
        if not isinstance(name, str) or name in seen or name not in by_name:
            raise ValueError('シートの追加・改名・重複には対応しません。新しくpullしてください')
        if entry['path'] != by_name[name]['path']:
            raise ValueError(f'TSVのpath変更には対応しません: {name}')
        selected.append(by_name[name])
        seen.add(name)
    warnings = [f'manifestから外したシートは変更しません: {name}' for name in by_name.keys() - seen]
    return root, metadata, selected, warnings


def _connection(args, spreadsheet_id=None):
    params = {}
    base = Path.cwd()
    if args.settings or args.bot:
        import yaml
        from utility import deep_merge, load_settings_yaml
        if not args.bot:
            raise ValueError('--settingsを使う場合は--botも指定してください')
        path = Path(args.settings or 'settings.yaml').resolve()
        try:
            source = load_settings_yaml(path)
        except yaml.YAMLError:
            raise ValueError('設定YAMLの構文が不正です') from None
        env = os.environ.get('XSBOT_DEPLOY_ENV', '')
        config = deep_merge(source.get('*', {}), source.get(env, {}))
        bot = config.get('bots', {}).get(args.bot)
        if not isinstance(bot, dict):
            raise ValueError(f'設定にBotがありません: {args.bot}')
        scenario = bot.get('scenario', {})
        params.update(config.get('options', {}))
        params.update(config.get('plugins', {}).get('google_sheets', {}))
        if scenario.get('type') == 'google_sheets':
            params.update(scenario.get('params', {}))
        base = path.parent
    target = args.sheet_id or params.get('sheet_id') or spreadsheet_id
    if not isinstance(target, str) or not target:
        raise ValueError('--sheet-idまたはGoogle SheetsのBot設定が必要です')
    if spreadsheet_id and target != spreadsheet_id:
        raise ValueError('指定したspreadsheet IDがpull時の同期情報と異なります')
    credential = args.credentials or params.get('key_file_json')
    if not isinstance(credential, str) or not credential or credential.lstrip().startswith(('{', '[')):
        raise ValueError('--credentialsまたはkey_file_jsonにJSONファイルのpathを指定してください')
    credential_base = Path.cwd() if args.credentials else base
    return target, str((credential_base / credential).resolve()), params


def _select(entries, names, key):
    if not names:
        return entries
    missing = set(names) - {entry[key] for entry in entries}
    if missing:
        raise ValueError(f'対象シートがありません: {sorted(missing)}')
    return [entry for entry in entries if entry[key] in names]


def _grid(property):
    grid = property.get('gridProperties', {})
    if (property.get('sheetType', 'GRID') != 'GRID'
            or type(grid.get('rowCount')) is not int or grid['rowCount'] <= 0
            or type(grid.get('columnCount')) is not int or grid['columnCount'] <= 0):
        raise ValueError(f'通常のGRIDシートだけを扱います: {property.get("title")}')
    return grid['rowCount'], min(grid['columnCount'], 26)


def _entry(property):
    return {'sheet_id': property['sheetId'], 'name': property['title'],
            'path': f'sheet-{property["sheetId"]}.tsv',
            'row_count': property['gridProperties']['rowCount'],
            'column_count': property['gridProperties']['columnCount']}


def _finish_copy(root, spreadsheet_id, entries):
    _save_json(root / 'manifest.json', {'sheets': [
        {'name': entry['name'], 'path': entry['path']} for entry in entries]})
    _save_json(root / SYNC_FILE, {
        'schema_version': 1, 'spreadsheet_id': spreadsheet_id,
        'pulled_at': datetime.now(timezone.utc).isoformat(), 'sheets': entries,
    })


def pull(args, client, params, spreadsheet_id):
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError('pullの出力先には未作成のディレクトリを指定してください')
    properties = client.metadata()
    if args.sheet:
        properties = _select(properties, args.sheet, 'title')
    elif not args.all_sheets:
        selector = SheetSelector(params)
        names = {title for title, _logical, _constant in selector.select(
            [prop['title'] for prop in properties], None)}
        properties = [prop for prop in properties if prop['title'] in names]
    for prop in properties:
        _grid(prop)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.sheets-pull-', dir=output.parent) as staging:
        root = Path(staging) / 'copy'
        root.mkdir()
        entries = []
        for prop, rows in client.read_sheets(properties):
            rows = normalize(rows)
            entry = _entry(prop)
            write_tsv(root / entry['path'], rows)
            _save_base(root, entry['sheet_id'], rows)
            entries.append(entry)
        _finish_copy(root, spreadsheet_id, entries)
        root.rename(output)
    return {'ok': True, 'exit_code': 0, 'command': 'pull',
            'manifest': str(output / 'manifest.json'), 'sheets': len(entries)}


def _row(rows, index):
    return rows[index] if index < len(rows) else []


def _changed_rows(before, after):
    return [index for index in range(max(len(before), len(after)))
            if _row(before, index) != _row(after, index)]


def _report(stream, entry, base, local, remote):
    local_changed, remote_changed = _changed_rows(base, local), _changed_rows(base, remote)
    for index in sorted(set(local_changed) | set(remote_changed)):
        stream.write(_json_bytes({
            'sheet': entry['name'], 'row': index + 1,
            'base': _row(base, index), 'local': _row(local, index), 'remote': _row(remote, index),
        }) + b'\n')
    return {'name': entry['name'], 'local_changed_rows': len(local_changed),
            'remote_changed_rows': len(remote_changed),
            'conflict': bool(local_changed and remote_changed and local != remote)}


def _request(property, start, rows):
    width = _grid(property)[1]
    return {'updateCells': {
        'start': {'sheetId': property['sheetId'], 'rowIndex': start, 'columnIndex': 0},
        'rows': [{'values': [
            {'userEnteredValue': cell} if cell else {}
            for cell in row + [{}] * (width - len(row))]} for row in rows],
        'fields': 'userEnteredValue',
    }}


def _batches(plans):
    batch, size = [], 0
    for plan in plans:
        desired = _read_json(plan['candidate'])
        prop = plan['property']
        for index in plan['changed_rows']:
            row = _row(desired, index)
            added = _request(prop, index, [row])
            cost = len(_json_bytes(added)) + len(prop['title'].encode('utf-8')) + 100
            if batch and size + cost > MAX_BATCH_BYTES:
                yield batch
                batch, size = [], 0
            if (batch and batch[-1][0] is plan
                    and batch[-1][1]['updateCells']['start']['rowIndex']
                    + len(batch[-1][1]['updateCells']['rows']) == index):
                batch[-1][1]['updateCells']['rows'].extend(added['updateCells']['rows'])
            else:
                batch.append((plan, added))
            size += cost
    if batch:
        yield batch


def _range(property, request):
    update = request['updateCells']
    start = update['start']['rowIndex'] + 1
    end = start + len(update['rows']) - 1
    title = property['title'].replace("'", "''")
    column = chr(64 + _grid(property)[1])
    return f"'{title}'!A{start}:{column}{end}"


def _verify_response(response, batch):
    sheets = response.get('updatedSpreadsheet', {}).get('sheets', [])
    by_id = {sheet['properties']['sheetId']: sheet for sheet in sheets}
    cells = {}
    for sheet_id, sheet in by_id.items():
        for block in sheet.get('data', []):
            for row_index, row in enumerate(block.get('rowData', []), block.get('startRow', 0)):
                for column, cell in enumerate(row.get('values', []), block.get('startColumn', 0)):
                    if cell.get('userEnteredValue'):
                        cells[sheet_id, row_index, column] = cell['userEnteredValue']
    for plan, request in batch:
        update = request['updateCells']
        sheet_id = plan['entry']['sheet_id']
        if sheet_id not in by_id:
            return False
        for row_index, row in enumerate(update['rows'], update['start']['rowIndex']):
            for column, cell in enumerate(row['values']):
                if cells.get((sheet_id, row_index, column), {}) != cell.get('userEnteredValue', {}):
                    return False
    return True


def _push(args, client, root, spreadsheet_id, plans, result):
    pending = [plan for plan in plans if plan['changed_rows']]
    backup = None
    if pending:
        backup = Path(args.backup_dir).resolve() / (
            datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S-') + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True)
        for plan in pending:
            entry = plan['entry']
            rows = _read_json(plan['remote'])
            write_tsv(backup / entry['path'], rows)
            _save_base(backup, entry['sheet_id'], rows)
            _save_json(backup / '.sheets-sync' / f'planned-{entry["sheet_id"]}.json',
                       _read_json(plan['candidate']))
        _finish_copy(backup, spreadsheet_id, [plan['entry'] for plan in pending])
        result['backup'] = str(backup / 'manifest.json')
    states = {plan['entry']['sheet_id']: {
        'name': plan['entry']['name'], 'status': 'pending', 'written_rows': 0,
        'total_rows': len(plan['changed_rows']),
    } for plan in plans}
    result['writes'] = list(states.values())

    def confirm(plan, status='confirmed'):
        _save_base(root, plan['entry']['sheet_id'], _read_json(plan['candidate']))
        states[plan['entry']['sheet_id']]['status'] = status

    in_flight = []
    try:
        for plan in plans:
            if not plan['changed_rows']:
                confirm(plan, 'already_applied')
        for batch in _batches(pending):
            requests = [request for _plan, request in batch]
            ranges = [_range(plan['property'], request) for plan, request in batch]
            for plan, _request_data in batch:
                states[plan['entry']['sheet_id']]['status'] = 'unconfirmed'
            in_flight = [plan for plan, _request_data in batch]
            response = client.write_rows(requests, ranges)
            if not _verify_response(response, batch):
                result.update(ok=False, exit_code=1, error='書き込み応答が予定の値と一致しません。再取得して確認してください')
                return result
            in_flight = []
            for plan, request in batch:
                state = states[plan['entry']['sheet_id']]
                state['written_rows'] += len(request['updateCells']['rows'])
                state['status'] = 'partial'
                if state['written_rows'] == state['total_rows']:
                    if args.verify:
                        state['status'] = 'awaiting_verify'
                    else:
                        confirm(plan)
        if args.verify:
            by_id = {plan['property']['sheetId']: plan for plan in pending}
            for prop, rows in client.read_sheets([plan['property'] for plan in pending]):
                plan = by_id[prop['sheetId']]
                if normalize(rows) != _read_json(plan['candidate']):
                    states[prop['sheetId']]['status'] = 'unconfirmed'
                    result.update(ok=False, exit_code=1, error='書き込み後の再取得が予定の値と一致しません')
                    return result
                confirm(plan)
    except KeyboardInterrupt:
        result.update(ok=False, exit_code=2, error='pushを中断しました',
                      write_result_uncertain=bool(in_flight))
        return result
    except (ApiError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        result.update(ok=False, exit_code=2, error=str(error))
        if isinstance(error, ApiError):
            result['write_result_uncertain'] = error.uncertain
            if not error.uncertain:
                for plan in in_flight:
                    state = states[plan['entry']['sheet_id']]
                    state['status'] = 'partial' if state['written_rows'] else 'rejected'
        return result
    return result


def compare(args, client, root, entries, spreadsheet_id, warnings):
    writing = args.command == 'push' and not args.dry_run
    result = {'ok': True, 'exit_code': 0, 'command': args.command,
              'sheets': [], 'warnings': warnings, 'writes': []}
    report = root / '.sheets-sync' / 'diff.jsonl'
    with tempfile.TemporaryDirectory(prefix='sheets-plan-') as temporary:
        temporary = Path(temporary)
        plans = []
        for entry in entries:
            base = _read_base(root, entry['sheet_id'])
            try:
                desired = local_values(read_tsv(_inside(root, entry['path'])), base)
            except ValueError as error:
                raise ValueError(f'{entry["name"]}: {error}') from error
            if writing and base == desired:
                continue
            candidate = temporary / f'{entry["sheet_id"]}-local.json'
            _save_json(candidate, desired)
            plans.append({'entry': entry, 'candidate': candidate})
        if not plans:
            if not writing:
                atomic_write(report, b'')
                result['diff_file'] = str(report)
            return result
        properties = {prop['sheetId']: prop for prop in client.metadata()}
        read_plans = []
        for plan in plans:
            entry = plan['entry']
            prop = properties.get(entry['sheet_id'])
            if prop is None or prop['title'] != entry['name']:
                if writing:
                    result.update(ok=False, exit_code=1,
                                  error=f'シートが削除・改名されています。新しくpullしてください: {entry["name"]}')
                    return result
                if prop is None:
                    result['warnings'].append(f'シートが削除されているため値を比較できません: {entry["name"]}')
                    result['sheets'].append({'name': entry['name'], 'remote_status': 'deleted'})
                    continue
                result['warnings'].append(f'シート名が変更されています: {entry["name"]} → {prop["title"]}')
            _grid(prop)
            plan['property'] = prop
            read_plans.append(plan)
        by_id = {plan['entry']['sheet_id']: plan for plan in read_plans}
        report_tmp = temporary / 'diff.jsonl'
        with report_tmp.open('wb') as stream:
            remote_sheets = client.read_sheets([plan['property'] for plan in read_plans]) if read_plans else ()
            for prop, remote in remote_sheets:
                plan = by_id[prop['sheetId']]
                entry = plan['entry']
                if writing and prop['title'] != entry['name']:
                    result.update(ok=False, exit_code=1,
                                  error=f'取得中にシート名が変わりました: {entry["name"]}')
                    return result
                if prop['title'] != plan['property']['title']:
                    result['warnings'].append(
                        f'取得中にシート名が変わりました: {plan["property"]["title"]} → {prop["title"]}')
                plan['property'] = prop
                base = _read_base(root, entry['sheet_id'])
                desired, remote = _read_json(plan['candidate']), normalize(remote)
                summary = _report(stream, entry, base, desired, remote)
                if prop['title'] != entry['name']:
                    summary['remote_name'] = prop['title']
                result['sheets'].append(summary)
                rows, columns = _grid(prop)
                if len(desired) > rows or any(len(row) > columns for row in desired):
                    summary['write_error'] = 'Sheetsの行列数が不足しています。Sheets側で増やしてください'
                if writing:
                    plan['changed_rows'] = _changed_rows(remote, desired)
                    plan['remote'] = temporary / f'{entry["sheet_id"]}-remote.json'
                    _save_json(plan['remote'], remote)
        # 詳細は行ごとに保存し、標準出力やメモリへ全シートの値を集めない。
        report.parent.mkdir(parents=True, exist_ok=True)
        with report_tmp.open('rb') as source, tempfile.NamedTemporaryFile(dir=report.parent, delete=False) as target:
            target_path = Path(target.name)
            try:
                import shutil
                shutil.copyfileobj(source, target)
                target.close()
                target_path.replace(report)
            finally:
                target_path.unlink(missing_ok=True)
        result['diff_file'] = str(report)
        if not writing:
            order = {plan['entry']['name']: index for index, plan in enumerate(plans)}
            result['sheets'].sort(key=lambda sheet: order[sheet['name']])
            return result
        if any(sheet['conflict'] for sheet in result['sheets']):
            result.update(ok=False, exit_code=1, error='Sheetsに競合する編集があります。書き込みは行いません')
            return result
        if any(sheet.get('write_error') for sheet in result['sheets']):
            result.update(ok=False, exit_code=2, error='Sheetsの行列数が不足しているため書き込めません')
            return result
        return _push(args, client, root, spreadsheet_id, plans, result)


def execute(args, client=None):
    if args.command == 'pull':
        target, credential, params = _connection(args)
        client = client or SheetsClient(target, credential, write=False)
        return pull(args, client, params, target)
    root, metadata, entries, warnings = _working_copy(args.manifest)
    entries = _select(entries, args.sheet, 'name')
    target, credential, _params = _connection(args, metadata['spreadsheet_id'])
    write = args.command == 'push' and not args.dry_run
    client = client or SheetsClient(target, credential, write=write)
    return compare(args, client, root, entries, target, warnings)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        args = parse_args(argv)
        result = execute(args)
    except KeyboardInterrupt:
        result = {'ok': False, 'exit_code': 2, 'error': '処理を中断しました'}
    except (ApiError, OSError, ValueError, KeyError, TypeError, AttributeError, re.error) as error:
        result = {'ok': False, 'exit_code': 2, 'error': str(error)}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return result['exit_code']


if __name__ == '__main__':
    sys.exit(main())
