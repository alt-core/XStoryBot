"""TSVとSheetsで共有する値の変換と、同期基準の読込み。"""

import csv
import hashlib
import json
import math
from pathlib import Path
import re


MAX_COLUMNS = 26
_VALUE_KEYS = frozenset({'stringValue', 'numberValue', 'boolValue', 'formulaValue'})
_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')


def _a1(row, column):
    name = ''
    while column >= 0:
        column, remainder = divmod(column, 26)
        name = chr(65 + remainder) + name
        column -= 1
    return f'{name}{row + 1}'


def _kind(cell):
    if not isinstance(cell, dict) or len(cell) > 1 or cell.keys() - _VALUE_KEYS:
        raise ValueError('セルは一種類のExtendedValueまたは空のobjectで指定してください')
    if not cell:
        return None
    kind, value = next(iter(cell.items()))
    if kind in ('stringValue', 'formulaValue'):
        if not isinstance(value, str) or (kind == 'formulaValue' and not value.startswith('=')):
            raise ValueError('文字列または数式のセル値が不正です')
    elif kind == 'boolValue':
        if type(value) is not bool:
            raise ValueError('boolValueには真偽値を指定してください')
    else:
        try:
            finite = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError('numberValueには有限の数値を指定してください')
    return kind


def _rows(rows):
    if not isinstance(rows, list):
        raise ValueError('行データは配列で指定してください')
    for index, row in enumerate(rows):
        if not isinstance(row, list):
            raise ValueError(f'A{index + 1}: 行は配列で指定してください')
        if any(cell not in ('', {}) for cell in row[MAX_COLUMNS:]):
            raise ValueError(f'AA{index + 1}: A:Zの26列を超えています')
    return rows


def normalize(rows):
    """末尾の未設定セルと空行だけを除く。空文字stringValueは未設定と区別する。"""
    result = []
    for row_index, row in enumerate(_rows(rows)):
        copied = []
        for column, cell in enumerate(row[:MAX_COLUMNS]):
            try:
                _kind(cell)
            except ValueError as error:
                raise ValueError(f'{_a1(row_index, column)}: {error}') from error
            copied.append(dict(cell))
        while copied and not copied[-1]:
            copied.pop()
        result.append(copied)
    while result and not result[-1]:
        result.pop()
    return result


def content_hash(rows):
    """セル型を含む内容hash。Sheetsの同じ数値1と1.0、-0と0は同一視する。"""
    canonical = normalize(rows)
    for row in canonical:
        for cell in row:
            value = cell.get('numberValue')
            if isinstance(value, float) and value.is_integer():
                cell['numberValue'] = int(value)
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def read_base_values(path):
    """型を判断する基準は内容hashを確認してから使う。"""
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        rows = normalize(value['values'])
        if content_hash(rows) != value['sha256']:
            raise ValueError('内容hashが一致しません')
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(f'同期基準が不正です: {path}: {error}') from error
    return rows


def sync_baselines(manifest_path, paths):
    """同期コピーの場合だけ、TSVと一致するシートの基準pathを返す。"""
    root = Path(manifest_path).resolve().parent
    metadata_path = root / 'sheets-sync.json'
    if not metadata_path.exists():
        if (root / '.sheets-sync').exists():
            raise ValueError(f'同期情報がありません: {metadata_path}')
        return {}
    try:
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        if (type(metadata.get('schema_version')) is not int or metadata['schema_version'] != 1
                or not isinstance(metadata.get('spreadsheet_id'), str)
                or not metadata['spreadsheet_id'] or not isinstance(metadata.get('sheets'), list)):
            raise ValueError('形式が不正です')
        entries, ids = {}, set()
        for entry in metadata['sheets']:
            name, sheet_id = entry['name'], entry['sheet_id']
            if (not isinstance(name, str) or not name or name in entries
                    or type(sheet_id) is not int or sheet_id < 0 or sheet_id in ids
                    or entry['path'] != f'sheet-{sheet_id}.tsv'):
                raise ValueError('シート名・sheetId・pathが不正です')
            entries[name] = entry
            ids.add(sheet_id)
        result = {}
        for name, path in paths.items():
            entry = entries[name]
            expected = (root / entry['path']).resolve()
            if expected != Path(path).resolve() or not expected.is_relative_to(root):
                raise ValueError(f'TSVのpathが一致しません: {name}')
            result[name] = root / '.sheets-sync' / f'base-{entry["sheet_id"]}.json'
        return result
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError(f'同期情報が不正です: {metadata_path}: {error}') from error


def to_text(cell):
    """TSVへ表示する値。文字列・式は原文、数値・真偽値は既存loader同様strを使う。"""
    kind = _kind(cell)
    return '' if kind is None else str(cell[kind])


def write_tsv(path, rows):
    """親directoryは呼出し側が用意する。quoted改行を含め標準TSVで保存する。"""
    values = normalize(rows)
    with Path(path).open('w', encoding='utf-8', newline='') as target:
        writer = csv.writer(target, dialect='excel-tab')
        writer.writerows([[to_text(cell) for cell in row] for row in values])


def read_tsv(path):
    """CSV recordを一行として読み、空行・空セルをそのまま返す。"""
    rows = []
    with Path(path).open(encoding='utf-8', newline='') as source:
        reader = csv.reader(source, dialect='excel-tab', strict=True)
        while True:
            physical_line = reader.line_num + 1
            try:
                row = next(reader)
            except StopIteration:
                break
            except csv.Error as error:
                raise ValueError(f'{path}:{physical_line}行目: TSVの形式が不正です: {error}') from error
            if any(row[MAX_COLUMNS:]):
                raise ValueError(f'AA{len(rows) + 1} ({path}:{physical_line}行目): A:Zの26列を超えています')
            rows.append(row[:MAX_COLUMNS])
    return rows


def local_values(text_rows, base_rows):
    """未変更の型を維持し、編集された=開始セルはTSV評価と同じく式にする。"""
    _rows(text_rows)
    base = normalize(base_rows)
    result = []
    for row_index in range(max(len(text_rows), len(base))):
        texts = text_rows[row_index][:MAX_COLUMNS] if row_index < len(text_rows) else []
        old_row = base[row_index] if row_index < len(base) else []
        row = []
        for column in range(max(len(texts), len(old_row))):
            text = texts[column] if column < len(texts) else ''
            old = old_row[column] if column < len(old_row) else {}
            position = _a1(row_index, column)
            if not isinstance(text, str):
                raise ValueError(f'{position}: TSVのセル値は文字列にしてください')
            kind = _kind(old)
            if column >= len(texts):
                cell = {}
            elif text == to_text(old):
                cell = dict(old)
            elif text == '':
                cell = {}
            elif text.lstrip().startswith('='):
                cell = {'formulaValue': text.lstrip()}
            elif kind == 'boolValue' and text.strip().upper() in ('TRUE', 'FALSE'):
                cell = {'boolValue': text.strip().upper() == 'TRUE'}
            elif kind == 'numberValue' and _NUMBER.fullmatch(text.strip()):
                number = float(text.strip())
                if not math.isfinite(number):
                    raise ValueError(f'{position}: 数値は有限の範囲で指定してください')
                cell = {'numberValue': int(number) if number.is_integer() else number}
            else:
                cell = {'stringValue': text}
            row.append(cell)
        result.append(row)
    return normalize(result)
