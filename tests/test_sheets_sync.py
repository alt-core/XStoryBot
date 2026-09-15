"""人工Sheetsで作業コピーと書込みの境界を確認する。外部通信は行わない。"""

import copy
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import yaml

from plugin.tsv import TsvPlugin_Loader
from tests.plugin.test_google_sheets import load_google_sheets_module
from tools import sheets_sync as sync
from tools.sheets_api import ApiError
from plugin.tsv_values import normalize, read_tsv


def text(value):
    return {'stringValue': value}


class FakeSheets:
    def __init__(self):
        self.properties = [
            {'sheetId': index, 'title': title, 'sheetType': 'GRID',
             'gridProperties': {'rowCount': 1000, 'columnCount': 30}}
            for index, title in enumerate(('story', 'story.test', 'story.prod', '$const', '_helper'))
        ]
        self.values = {
            0: [[text('開始'), text('こんにちは\t"世界"\n次の行'), text('001'), text('TRUE'),
                 text('=literal'), {'numberValue': 1}, {'boolValue': True},
                 {'formulaValue': '=IMAGE("https://example.invalid/image.png")'}],
                [text('次へ'), text('終わり')]],
            1: [[text('環境別'), text('テスト')]],
            2: [[text('環境別'), text('本番')]],
            3: [[text('title'), text('value')], [{}, text('物語')]],
            4: [[text('補助データ')]],
        }
        self.writes = []
        self.reads = []
        self.metadata_calls = 0
        self.before_write = None
        self.after_write = None
        self.fail_write = None

    def metadata(self):
        self.metadata_calls += 1
        return copy.deepcopy(self.properties)

    def read_sheets(self, properties):
        self.reads.append([prop['sheetId'] for prop in properties])
        for prop in properties:
            yield copy.deepcopy(prop), copy.deepcopy([row[:26] for row in self.values[prop['sheetId']]])

    def write_rows(self, requests, response_ranges):
        if self.before_write:
            self.before_write()
        self.writes.append(copy.deepcopy(requests))
        if self.fail_write == len(self.writes):
            raise ApiError('人工的なtimeout', uncertain=True)
        sheets = {}
        for request in requests:
            update = request['updateCells']
            assert update['fields'] == 'userEnteredValue'
            start = update['start']
            sheet_id = start['sheetId']
            data = self.values[sheet_id]
            for index, row in enumerate(update['rows'], start['rowIndex']):
                while len(data) <= index:
                    data.append([])
                width = len(row['values'])
                data[index] = [cell.get('userEnteredValue', {}) for cell in row['values']] + data[index][width:]
            sheet = sheets.setdefault(sheet_id, {'properties': {'sheetId': sheet_id}, 'data': []})
            # 空欄の末尾を省くAPIの応答でも照合できるようにする。
            echoed = copy.deepcopy(update['rows'])
            for row in echoed:
                while row['values'] and not row['values'][-1]:
                    row['values'].pop()
            while echoed and not echoed[-1]['values']:
                echoed.pop()
            sheet['data'].append({'startRow': start['rowIndex'], 'rowData': echoed})
        response = {'updatedSpreadsheet': {'sheets': list(sheets.values())}}
        if self.after_write:
            self.after_write(response)
        return response


class SheetsSyncTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.copy = self.root / 'copy'
        self.manifest = self.copy / 'manifest.json'
        self.client = FakeSheets()
        self.credentials = self.root / 'unused.json'

    def args(self, command, *extra):
        common = ['--sheet-id', 'sheet-test', '--credentials', str(self.credentials)]
        if command == 'pull':
            return sync.parse_args([command, *common, '--output', str(self.copy), *extra])
        if command == 'push':
            extra = ('--backup-dir', str(self.root / 'backups'), *extra)
        return sync.parse_args([command, *common, '--manifest', str(self.manifest), *extra])

    def pull(self, *extra):
        return sync.execute(self.args('pull', *extra), client=self.client)

    def change(self, sheet_id, row, column, value):
        path = self.copy / f'sheet-{sheet_id}.tsv'
        rows = read_tsv(path)
        while len(rows) <= row:
            rows.append([])
        while len(rows[row]) <= column:
            rows[row].append('')
        rows[row][column] = value
        with path.open('w', encoding='utf-8', newline='') as target:
            csv.writer(target, dialect='excel-tab').writerows(rows)

    def test_pullは全環境の物理シートをTSVへ保存する(self):
        result = self.pull()
        self.assertEqual(4, result['sheets'])
        manifest = json.loads(self.manifest.read_text())
        self.assertEqual({'sheets'}, set(manifest))
        self.assertEqual(['story', 'story.test', 'story.prod', '$const'],
                         [entry['name'] for entry in manifest['sheets']])
        with patch.dict('sys.modules', {'settings': types.SimpleNamespace(DEPLOY_ENV='test')}):
            tables, constants = TsvPlugin_Loader({'manifest': str(self.manifest)}).load_scenario()
        self.assertEqual('テスト', tables[0][1][-1][1])
        self.assertEqual('True', tables[0][1][0][6])
        self.assertEqual({'title': '物語'}, constants)
        self.assertFalse(self.credentials.exists())

    def test_明示したsheetは既定の除外にかかわらず取得できる(self):
        self.pull('--sheet', '_helper')
        manifest = json.loads(self.manifest.read_text())
        self.assertEqual(['_helper'], [entry['name'] for entry in manifest['sheets']])

    def test_台詞と式の編集をローカル評価してSheetsへ戻しても意味が一致する(self):
        self.client.values[0] = [[text('開始'), text('こんばんは'), text(' =A1'), text('=literal')]]
        self.client.values[4] = [[text('花子')]]
        self.pull('--sheet', 'story', '--sheet', '_helper')
        module, _credentials = load_google_sheets_module()
        google_loader = module.GoogleSheetPlugin_Loader({'evaluate_formula': True})
        tsv_loader = TsvPlugin_Loader({'manifest': str(self.manifest), 'evaluate_formula': True})
        formula = '=\'_helper\'!A1&"さん"'
        for edited, expected, expected_type in (
                ('  ' + formula, '花子さん', 'formulaValue'),
                ('="=そのまま"', '=そのまま', 'formulaValue'),
                ('普通の台詞', '普通の台詞', 'stringValue')):
            with self.subTest(edited=edited):
                self.change(0, 0, 1, edited)
                with patch.dict('sys.modules', {'settings': types.SimpleNamespace(DEPLOY_ENV='test')}):
                    local = tsv_loader.load_scenario()[0][0][1]
                self.assertEqual(expected, local[0][1])
                result = sync.execute(self.args('push', '--verify'), self.client)
                self.assertTrue(result['ok'], result)
                self.assertIn(expected_type, self.client.values[0][0][1])

                def sheet_values(session, sheet_id, ranges, render):
                    cells = self.client.values[0][0]
                    values = [next(iter(cell.values())) if cell else '' for cell in cells]
                    while values and values[-1] == '':
                        values.pop()
                    # 計算結果は人工値。式として送られた場合だけSheetsが計算する契約を表す。
                    if render == 'UNFORMATTED_VALUE' and 'formulaValue' in cells[1]:
                        values[1] = expected
                    return [{'values': [values]}]

                with patch.object(google_loader, '_batch_get_values', side_effect=sheet_values):
                    remote = google_loader._batch_get_sheet_values(None, 'sheet-test', ['story'])['story']
                self.assertEqual(local, remote)
                with patch.dict('sys.modules', {'settings': types.SimpleNamespace(DEPLOY_ENV='test')}):
                    self.assertEqual(local, tsv_loader.load_scenario()[0][0][1])
                self.assertEqual(text(' =A1'), self.client.values[0][0][2])
                self.assertEqual(text('=literal'), self.client.values[0][0][3])

    def test_一セルの往復で他の型とZより右を変えず次回pushは通信しない(self):
        self.client.values[0][0] += [{}] * 18 + [text('Zの右の値')]
        original = copy.deepcopy(self.client.values[0])
        self.pull()
        self.change(0, 0, 1, '変更後\nの台詞')
        result = sync.execute(self.args('push'), self.client)
        self.assertTrue(result['ok'])
        original[0][1] = text('変更後\nの台詞')
        self.assertEqual(original, self.client.values[0])
        self.assertEqual(1, len(self.client.writes))
        self.assertEqual(1, len(self.client.writes[0][0]['updateCells']['rows']))
        backup = Path(result['backup']).parent
        self.assertEqual('こんにちは\t"世界"\n次の行', read_tsv(backup / 'sheet-0.tsv')[0][1])
        self.client.metadata_calls = 0
        self.client.reads.clear()
        self.assertTrue(sync.execute(self.args('push'), self.client)['ok'])
        self.assertEqual([], self.client.reads)
        self.assertEqual(0, self.client.metadata_calls)
        self.assertEqual(1, len(self.client.writes))

    def test_同期コピーの数値と真偽値は検証と書込で同じ値に整える(self):
        self.client.values[0] = [[text('開始'), {'numberValue': 1}, {'boolValue': True},
                                  text('001'), text('TRUE')]]
        self.pull('--sheet', 'story')
        for number, boolean, expected in (
                ('+01', ' TRUE ', ['開始', '1', 'True', '001', 'TRUE']),
                ('+02.00', 'false', ['開始', '2', 'False', '001', 'TRUE'])):
            with self.subTest(number=number, boolean=boolean):
                self.change(0, 0, 1, number)
                self.change(0, 0, 2, boolean)
                raw = (self.copy / 'sheet-0.tsv').read_bytes()
                for evaluate_formula in (False, True):
                    with patch.dict('sys.modules', {'settings': types.SimpleNamespace(DEPLOY_ENV='test')}):
                        loaded = TsvPlugin_Loader({
                            'manifest': str(self.manifest), 'evaluate_formula': evaluate_formula,
                        }).load_scenario()[0][0][1]
                    self.assertEqual([expected], loaded)
                result = sync.execute(self.args('push', '--verify'), self.client)
                self.assertTrue(result['ok'], result)
                remote_text = [str(next(iter(cell.values()))) if cell else ''
                               for cell in self.client.values[0][0]][:5]
                self.assertEqual(expected, remote_text)
                self.assertEqual(raw, (self.copy / 'sheet-0.tsv').read_bytes())
        self.assertEqual(1, len(self.client.writes))

        self.change(0, 0, 1, '数値ではない本文')
        for evaluate_formula in (False, True):
            with patch.dict('sys.modules', {'settings': types.SimpleNamespace(DEPLOY_ENV='test')}):
                rows = TsvPlugin_Loader({'manifest': str(self.manifest),
                                        'evaluate_formula': evaluate_formula}).load_scenario()[0][0][1]
            self.assertEqual('数値ではない本文', rows[0][1])
        self.assertTrue(sync.execute(self.args('push'), self.client)['ok'])
        self.assertEqual(text('数値ではない本文'), self.client.values[0][0][1])

    def test_定数の通常の行編集を検証して書き戻せる(self):
        base = [[text('items'), text('list')], [{}, {'numberValue': 1}],
                [{}, {'numberValue': 2}], [text('enabled'), text('value')],
                [{}, {'boolValue': True}]]
        self.client.values[3] = copy.deepcopy(base)
        self.pull('--sheet', '$const')
        original = read_tsv(self.copy / 'sheet-3.tsv')
        cases = [([['; 説明']] + original, {'items': [1, 2], 'enabled': True}),
                 (original[:1] + original[2:], {'items': [2], 'enabled': True}),
                 (original[3:] + original[:3], {'items': [1, 2], 'enabled': True})]
        for edited, expected in cases:
            with self.subTest(edited=edited):
                self.client.values[3] = copy.deepcopy(base)
                sync._save_base(self.copy, 3, base)
                with (self.copy / 'sheet-3.tsv').open('w', encoding='utf-8', newline='') as target:
                    csv.writer(target, dialect='excel-tab').writerows(edited)
                for evaluate_formula in (False, True):
                    with patch.dict('sys.modules', {'settings': types.SimpleNamespace(DEPLOY_ENV='test')}):
                        tables, constants = TsvPlugin_Loader({
                            'manifest': str(self.manifest), 'evaluate_formula': evaluate_formula,
                        }).load_scenario()
                    self.assertEqual(expected, constants)
                result = sync.execute(self.args('push', '--verify'), self.client)
                self.assertTrue(result['ok'], result)
                module, _credentials = load_google_sheets_module()
                remote = [[next(iter(cell.values())) if cell else '' for cell in row]
                          for row in normalize(self.client.values[3])]
                self.assertEqual(expected, module.parse_table(remote))

    def test_diffとdry_runは同じ差分を返し基準を更新しない(self):
        self.pull()
        baseline = sync._base_path(self.copy, 0).read_bytes()
        self.change(0, 0, 1, 'ローカルの編集')
        self.client.values[0][0][0] = text('作家の編集')
        diff = sync.execute(self.args('diff'), self.client)
        dry = sync.execute(self.args('push', '--dry-run'), self.client)
        self.assertEqual(diff['sheets'], dry['sheets'])
        self.assertTrue(diff['sheets'][0]['conflict'])
        details = [json.loads(line) for line in Path(diff['diff_file']).read_text().splitlines()]
        self.assertEqual('作家の編集', details[0]['remote'][0]['stringValue'])
        self.assertEqual(baseline, sync._base_path(self.copy, 0).read_bytes())
        self.assertEqual([], self.client.writes)

    def test_行数不足でもdiffを返し実pushだけを拒否する(self):
        self.pull('--sheet', 'story')
        self.client.properties[0]['gridProperties']['rowCount'] = 2
        self.change(0, 2, 1, '追加行')
        baseline = sync._base_path(self.copy, 0).read_bytes()
        for command, options in (('diff', ()), ('push', ('--dry-run',)), ('push', ())):
            with self.subTest(command=command, options=options):
                result = sync.execute(self.args(command, *options), self.client)
                self.assertEqual(2 if command == 'push' and not options else 0, result['exit_code'])
                details = [json.loads(line) for line in Path(result['diff_file']).read_text().splitlines()]
                self.assertEqual(3, details[0]['row'])
                self.assertIn('write_error', result['sheets'][0])
                self.assertEqual(baseline, sync._base_path(self.copy, 0).read_bytes())
                self.assertEqual([], self.client.writes)

    def test_remoteで行が削除された場合も差分を残す(self):
        self.pull('--sheet', 'story')
        self.client.properties[0]['gridProperties']['rowCount'] = 1
        self.client.values[0] = self.client.values[0][:1]
        self.change(0, 0, 1, 'ローカル編集')
        result = sync.execute(self.args('diff'), self.client)
        self.assertTrue(result['ok'])
        details = [json.loads(line) for line in Path(result['diff_file']).read_text().splitlines()]
        self.assertEqual([1, 2], [row['row'] for row in details])
        self.assertEqual([], details[1]['remote'])
        self.assertEqual(1, sync.execute(self.args('push'), self.client)['exit_code'])
        self.assertEqual([], self.client.writes)

    def test_一シートでも競合なら全て書かずバックアップも不要(self):
        self.pull()
        self.change(0, 0, 1, 'ローカル')
        self.change(1, 0, 1, 'ローカル')
        self.client.values[1][0][1] = text('共同編集')
        result = sync.execute(self.args('push'), self.client)
        self.assertEqual(1, result['exit_code'])
        self.assertEqual([], self.client.writes)
        self.assertFalse((self.root / 'backups').exists())

    def test_sheet指定で未選択の編集には触れない(self):
        self.pull()
        self.change(0, 0, 1, '採用')
        self.change(1, 0, 1, '未選択')
        result = sync.execute(self.args('push', '--sheet', 'story'), self.client)
        self.assertTrue(result['ok'])
        self.assertEqual('テスト', self.client.values[1][0][1]['stringValue'])
        self.assertEqual([0], self.client.reads[-1])

    def test_短くした末尾を消して行追加は既存グリッド内で行う(self):
        self.client.values[1].append([{}, text('')])
        self.pull('--sheet', 'story.test')
        self.change(1, 2, 1, '追加')
        self.assertTrue(sync.execute(self.args('push'), self.client)['ok'])
        (self.copy / 'sheet-1.tsv').write_text('開始\t短い台本\n', encoding='utf-8')
        self.assertTrue(sync.execute(self.args('push'), self.client)['ok'])
        self.assertEqual([[text('開始'), text('短い台本')]], normalize(self.client.values[1]))

    def test_送信中のローカル編集を同期済みにしない(self):
        self.pull()
        self.change(0, 0, 1, '送信する値')
        self.client.before_write = lambda: self.change(0, 0, 1, '送信中の編集')
        result = sync.execute(self.args('push'), self.client)
        self.assertTrue(result['ok'])
        self.assertEqual('送信する値', sync._read_base(self.copy, 0)[0][1]['stringValue'])
        diff = sync.execute(self.args('diff'), self.client)
        self.assertEqual(1, diff['sheets'][0]['local_changed_rows'])

    def test_分割途中失敗で完了したシートだけ基準を進める(self):
        self.pull()
        self.change(0, 0, 1, '更新0')
        self.change(1, 0, 1, '更新1')
        old_base = sync._base_path(self.copy, 1).read_bytes()
        self.client.fail_write = 2
        with patch.object(sync, 'MAX_BATCH_BYTES', 1):
            result = sync.execute(self.args('push'), self.client)
        self.assertEqual(2, result['exit_code'])
        self.assertEqual(['confirmed', 'unconfirmed'], [item['status'] for item in result['writes']])
        self.assertEqual('更新0', sync._read_base(self.copy, 0)[0][1]['stringValue'])
        self.assertEqual(old_base, sync._base_path(self.copy, 1).read_bytes())
        self.assertTrue(Path(result['backup']).exists())

    def test_同じシートの分割途中失敗では基準を進めない(self):
        self.pull()
        old_base = sync._base_path(self.copy, 0).read_bytes()
        self.change(0, 0, 1, '更新1')
        self.change(0, 1, 1, '更新2')
        self.client.fail_write = 2
        with patch.object(sync, 'MAX_BATCH_BYTES', 1):
            result = sync.execute(self.args('push'), self.client)
        self.assertEqual(2, result['exit_code'])
        self.assertEqual(1, result['writes'][0]['written_rows'])
        self.assertEqual(old_base, sync._base_path(self.copy, 0).read_bytes())

    def test_送信中の中断でも結果とバックアップを返す(self):
        self.pull()
        self.change(0, 0, 1, '変更')
        with patch.object(self.client, 'write_rows', side_effect=KeyboardInterrupt):
            result = sync.execute(self.args('push'), self.client)
        self.assertEqual(2, result['exit_code'])
        self.assertTrue(result['write_result_uncertain'])
        self.assertEqual('unconfirmed', result['writes'][0]['status'])
        backup = Path(result['backup']).parent
        self.assertEqual('変更', json.loads(
            (backup / '.sheets-sync/planned-0.json').read_text())[0][1]['stringValue'])

    def test_既知の書き込み拒否は結果不明と区別する(self):
        self.pull()
        self.change(0, 0, 1, '変更')
        with patch.object(self.client, 'write_rows', side_effect=ApiError('拒否', status=400)):
            result = sync.execute(self.args('push'), self.client)
        self.assertFalse(result['write_result_uncertain'])
        self.assertEqual('rejected', result['writes'][0]['status'])

    def test_verifyは再取得で変化を報告して基準を進めない(self):
        self.pull()
        self.change(0, 0, 1, '変更')
        old_base = sync._base_path(self.copy, 0).read_bytes()
        self.client.after_write = lambda response: self.client.values[0][0].__setitem__(1, text('直後の編集'))
        result = sync.execute(self.args('push', '--verify'), self.client)
        self.assertEqual(1, result['exit_code'])
        self.assertEqual(old_base, sync._base_path(self.copy, 0).read_bytes())

    def test_応答欠落は基準を進めず同じ候補が既に反映済みなら再送しない(self):
        self.pull()
        self.change(0, 0, 1, '変更')
        old_base = sync._base_path(self.copy, 0).read_bytes()
        self.client.after_write = lambda response: response.clear()
        result = sync.execute(self.args('push'), self.client)
        self.assertEqual(1, result['exit_code'])
        self.assertEqual(old_base, sync._base_path(self.copy, 0).read_bytes())
        self.client.after_write = None
        result = sync.execute(self.args('push'), self.client)
        self.assertTrue(result['ok'])
        self.assertEqual('already_applied', result['writes'][0]['status'])
        self.assertEqual(1, len(self.client.writes))

    def test_不正な応答形状でも復旧情報を残す(self):
        self.pull('--sheet', 'story')
        self.change(0, 0, 1, '更新')
        baseline = sync._base_path(self.copy, 0).read_bytes()
        self.client.after_write = lambda response: response.update(updatedSpreadsheet=None)
        result = sync.execute(self.args('push'), self.client)
        self.assertFalse(result['ok'])
        self.assertTrue(Path(result['backup']).exists())
        self.assertEqual('unconfirmed', result['writes'][0]['status'])
        self.assertEqual(baseline, sync._base_path(self.copy, 0).read_bytes())

    def test_バックアップ失敗では書かずpull失敗では不完全コピーを公開しない(self):
        self.pull()
        self.change(0, 0, 1, '変更')
        with patch.object(sync, 'write_tsv', side_effect=OSError('ディスク容量不足')):
            with self.assertRaises(OSError):
                sync.execute(self.args('push'), self.client)
        self.assertEqual([], self.client.writes)
        self.copy = self.root / 'failed-copy'
        with patch.object(sync, 'write_tsv', side_effect=OSError('ディスク容量不足')):
            with self.assertRaises(OSError):
                self.pull()
        self.assertFalse(self.copy.exists())

    def test_改名と異なるspreadsheet指定は書き込まない(self):
        self.pull()
        self.change(0, 0, 1, '変更')
        self.client.properties[0]['title'] = '改名済み'
        self.assertEqual(1, sync.execute(self.args('push'), self.client)['exit_code'])
        with self.assertRaisesRegex(ValueError, '異なります'):
            sync.execute(self.args('push', '--sheet-id', 'different'), self.client)
        self.assertEqual([], self.client.writes)

    def test_同期pathの改変でバックアップが作業TSVを上書きしない(self):
        self.pull()
        self.change(0, 0, 1, '消してはいけない編集')
        original = (self.copy / 'sheet-0.tsv').read_bytes()
        metadata = json.loads((self.copy / sync.SYNC_FILE).read_text())
        manifest = json.loads(self.manifest.read_text())
        for path in (str(self.copy / 'sheet-0.tsv'), '../copy/sheet-0.tsv'):
            with self.subTest(path=path):
                metadata['sheets'][0]['path'] = path
                manifest['sheets'][0]['path'] = path
                (self.copy / sync.SYNC_FILE).write_text(json.dumps(metadata))
                self.manifest.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, 'path'):
                    sync.execute(self.args('push'), self.client)
                self.assertEqual(original, (self.copy / 'sheet-0.tsv').read_bytes())
                self.assertEqual([], self.client.writes)

    def test_設定はクラウドを初期化せず環境と資格情報上書きを読む(self):
        settings = self.root / 'settings.yaml'
        settings.write_text(yaml.safe_dump({'*': {
            'cloud': {'provider': 'aws'},
            'plugins': {'google_sheets': {'ignore_sheet': '^skip'}},
            'bots': {'bot': {'scenario': {'type': 'google_sheets',
                     'params': {'sheet_id': 'base', 'key_file_json': 'read.json'}}}},
        }, 'test': {'bots': {'bot': {'scenario': {'params': {'sheet_id': 'test'}}}}}}))
        args = sync.parse_args(['pull', '--settings', str(settings), '--bot', 'bot',
                               '--credentials', str(self.credentials), '--output', str(self.copy)])
        with patch.dict(os.environ, {'XSBOT_DEPLOY_ENV': 'test'}):
            target, credential, params = sync._connection(args)
        self.assertEqual('test', target)
        self.assertEqual(str(self.credentials), credential)
        self.assertEqual('^skip', params['ignore_sheet'])

    def test_CLIの標準出力は成功も失敗もJSON一件(self):
        for argv in (['pull'], ['pull', '--sheet-id', 'sheet-test', '--credentials', str(self.credentials),
                              '--output', str(self.copy)]):
            with self.subTest(argv=argv), patch.object(sync, 'SheetsClient', return_value=self.client), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                code = sync.main(argv)
            result = json.loads(output.getvalue())
            self.assertEqual(code, result['exit_code'])

    def test_不正な選択regexもJSONの入力エラーにする(self):
        argv = ['pull', '--sheet-id', 'sheet-test', '--credentials', str(self.credentials),
                '--output', str(self.copy)]
        with patch.object(sync, '_connection', return_value=('sheet-test', str(self.credentials), {'script_sheet': '['})), \
                patch.object(sync, 'SheetsClient', return_value=self.client), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(2, sync.main(argv))
        self.assertFalse(json.loads(output.getvalue())['ok'])
        self.assertFalse(self.copy.exists())

    def test_実CLIのno_opは親のクラウド設定や資格情報を読まずJSONを返す(self):
        self.pull()
        # 存在しない設定・資格情報でも、未変更pushは認証もcloud初期化も不要。
        environment = {**os.environ, 'XSBOT_SETTINGS_FILE': str(self.credentials),
                       'GOOGLE_APPLICATION_CREDENTIALS': str(self.credentials),
                       'XSBOT_CLOUD_PROVIDER': 'aws'}
        result = subprocess.run(
            [sys.executable, str(sync.PROJECT_ROOT / 'tools/sheets_sync.py'), 'push',
             '--manifest', str(self.manifest), '--credentials', str(self.credentials)],
            cwd=self.root, env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], json.loads(result.stdout)['writes'])

    def test_変更なしpushは直前のremote差分を消さず通信もしない(self):
        self.pull('--sheet', 'story')
        baseline = sync._base_path(self.copy, 0).read_bytes()
        self.client.values[0][0][1] = text('共同作家の新しい台詞')
        diff = sync.execute(self.args('diff'), self.client)
        report = Path(diff['diff_file'])
        recorded = report.read_bytes()
        self.assertTrue(recorded)
        self.assertEqual(1, diff['sheets'][0]['remote_changed_rows'])
        self.client.metadata_calls = 0
        self.client.reads.clear()

        result = sync.execute(self.args('push'), self.client)

        self.assertTrue(result['ok'])
        self.assertNotIn('diff_file', result)
        self.assertEqual(recorded, report.read_bytes())
        self.assertEqual(baseline, sync._base_path(self.copy, 0).read_bytes())
        self.assertEqual(0, self.client.metadata_calls)
        self.assertEqual([], self.client.reads)
        self.assertEqual([], self.client.writes)

    def test_反映済みの基準保存で途中失敗しても進捗を返して再実行できる(self):
        self.pull('--sheet', 'story', '--sheet', 'story.test')
        for sheet_id in (0, 1):
            self.change(sheet_id, 0, 1, '反映済み')
            self.client.values[sheet_id][0][1] = text('反映済み')
        old_second = sync._base_path(self.copy, 1).read_bytes()
        save_base = sync._save_base

        def fail_second(root, sheet_id, rows):
            if root == self.copy and sheet_id == 1:
                raise OSError('人工的な容量不足')
            return save_base(root, sheet_id, rows)

        with patch.object(sync, '_save_base', side_effect=fail_second):
            result = sync.execute(self.args('push'), self.client)

        self.assertEqual(2, result['exit_code'])
        self.assertFalse(result['ok'])
        self.assertEqual(['already_applied', 'pending'], [item['status'] for item in result['writes']])
        self.assertEqual(text('反映済み'), sync._read_base(self.copy, 0)[0][1])
        self.assertEqual(old_second, sync._base_path(self.copy, 1).read_bytes())
        resumed = sync.execute(self.args('push'), self.client)
        self.assertTrue(resumed['ok'])
        self.assertEqual(['already_applied'], [item['status'] for item in resumed['writes']])
        self.assertEqual(text('反映済み'), sync._read_base(self.copy, 1)[0][1])
        self.assertEqual([], self.client.writes)

    def test_diffとdry_runは書込み用remote_snapshotを保存しない(self):
        self.pull()
        self.change(0, 0, 1, 'ローカルの台詞')
        self.client.values[1][0][1] = text('共同作家の台詞')
        summaries = []
        for arguments in (('diff',), ('push', '--dry-run')):
            with self.subTest(command=arguments), patch.object(sync, '_save_json', wraps=sync._save_json) as save:
                result = sync.execute(self.args(*arguments), self.client)
            self.assertTrue(result['ok'])
            self.assertTrue(Path(result['diff_file']).read_bytes())
            remote_saves = [item for item in save.call_args_list
                            if Path(item.args[0]).name.endswith('-remote.json')]
            self.assertEqual([], remote_saves)
            self.assertEqual(1, result['sheets'][0]['local_changed_rows'])
            self.assertEqual(1, result['sheets'][1]['remote_changed_rows'])
            summaries.append(result['sheets'])
        self.assertEqual(summaries[0], summaries[1])
        self.assertEqual([], self.client.writes)

    def test_改名後もdiffとdry_runは現在名で読み旧名と値差分を報告する(self):
        self.pull('--sheet', 'story')
        metadata = (self.copy / sync.SYNC_FILE).read_bytes()
        baseline = sync._base_path(self.copy, 0).read_bytes()
        self.change(0, 0, 1, 'ローカルの編集')
        self.client.properties[0]['title'] = 'chapter'
        for arguments in (('diff',), ('push', '--dry-run')):
            with self.subTest(command=arguments), patch.object(
                    self.client, 'read_sheets', wraps=self.client.read_sheets) as read:
                result = sync.execute(self.args(*arguments), self.client)
            self.assertTrue(result['ok'])
            self.assertEqual(0, result['exit_code'])
            self.assertEqual('chapter', read.call_args.args[0][0]['title'])
            self.assertEqual('story', result['sheets'][0]['name'])
            self.assertEqual('chapter', result['sheets'][0]['remote_name'])
            self.assertEqual(1, result['sheets'][0]['local_changed_rows'])
            self.assertTrue(any('story → chapter' in warning for warning in result['warnings']))
            self.assertTrue(Path(result['diff_file']).read_bytes())
        self.assertEqual(metadata, (self.copy / sync.SYNC_FILE).read_bytes())
        self.assertEqual(baseline, sync._base_path(self.copy, 0).read_bytes())
        self.assertEqual([], self.client.writes)

    def test_削除シートは診断だけを返し他のシートの比較を続ける(self):
        self.pull('--sheet', 'story', '--sheet', 'story.test')
        self.change(0, 0, 1, '比較できる編集')
        self.client.properties = [prop for prop in self.client.properties if prop['sheetId'] != 1]
        for arguments in (('diff',), ('push', '--dry-run')):
            with self.subTest(command=arguments):
                self.client.reads.clear()
                result = sync.execute(self.args(*arguments), self.client)
            self.assertTrue(result['ok'])
            self.assertEqual(['story', 'story.test'], [item['name'] for item in result['sheets']])
            self.assertEqual(1, result['sheets'][0]['local_changed_rows'])
            self.assertEqual({'name': 'story.test', 'remote_status': 'deleted'}, result['sheets'][1])
            self.assertEqual([[0]], self.client.reads)
            self.assertTrue(any('削除' in warning and 'story.test' in warning for warning in result['warnings']))
            details = [json.loads(line) for line in Path(result['diff_file']).read_text().splitlines()]
            self.assertEqual(['story'], [item['sheet'] for item in details])
        self.change(1, 0, 1, '削除されたシートへの編集')
        self.assertEqual(1, sync.execute(self.args('push'), self.client)['exit_code'])
        self.assertEqual([], self.client.writes)

    def test_全シート削除でもreadonlyは比較不能の一覧を返して成功する(self):
        self.pull('--sheet', 'story', '--sheet', 'story.test')
        metadata = (self.copy / sync.SYNC_FILE).read_bytes()
        self.client.properties = []
        for arguments in (('diff',), ('push', '--dry-run')):
            with self.subTest(command=arguments):
                self.client.reads.clear()
                result = sync.execute(self.args(*arguments), self.client)
            self.assertTrue(result['ok'])
            self.assertEqual(0, result['exit_code'])
            self.assertEqual([
                {'name': 'story', 'remote_status': 'deleted'},
                {'name': 'story.test', 'remote_status': 'deleted'},
            ], result['sheets'])
            self.assertEqual(2, len(result['warnings']))
            self.assertEqual(b'', Path(result['diff_file']).read_bytes())
            self.assertEqual([], self.client.reads)
        self.assertEqual(metadata, (self.copy / sync.SYNC_FILE).read_bytes())
        self.assertEqual([], self.client.writes)

    def test_取得中の改名もreadonlyは警告して比較しpushだけを止める(self):
        self.pull('--sheet', 'story')
        baseline = sync._base_path(self.copy, 0).read_bytes()
        self.change(0, 0, 1, 'ローカルの編集')
        original_read = self.client.read_sheets

        def renamed(properties):
            for prop, rows in original_read(properties):
                prop['title'] = '途中の改名'
                yield prop, rows

        with patch.object(self.client, 'read_sheets', side_effect=renamed):
            for arguments in (('diff',), ('push', '--dry-run')):
                result = sync.execute(self.args(*arguments), self.client)
                self.assertTrue(result['ok'])
                self.assertEqual('途中の改名', result['sheets'][0]['remote_name'])
                self.assertEqual(1, result['sheets'][0]['local_changed_rows'])
                self.assertTrue(any('取得中' in warning for warning in result['warnings']))
            result = sync.execute(self.args('push'), self.client)
        self.assertEqual(1, result['exit_code'])
        self.assertFalse(result['ok'])
        self.assertEqual(baseline, sync._base_path(self.copy, 0).read_bytes())
        self.assertEqual([], self.client.writes)


if __name__ == '__main__':
    unittest.main()
