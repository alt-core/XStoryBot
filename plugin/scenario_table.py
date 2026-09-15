"""取得元に依存しないシナリオ表の選択・定数解釈。"""

import logging
import re

from utility import deep_merge, to_hankaku


class SourceRow(list):
    """連結後も元の表示位置を保持し、snapshotへ保存できる行。"""

    def __init__(self, cells, sheet_title, line_no, source_path=None):
        super().__init__(cells)
        self.source_path = source_path
        title = f'{sheet_title} ({source_path})' if source_path else sheet_title
        self.source_position = (title, line_no)


class SheetSelector:
    """取得前に選択条件を検査し、元の順序と論理名を保って選別する。"""

    def __init__(self, params):
        self.script_sheet = re.compile(params.get('script_sheet', r'^[^$]'), re.IGNORECASE)
        self.constant_sheet = re.compile(params.get('constant_sheet', r'^\$'), re.IGNORECASE)
        self.ignore_sheet = re.compile(params.get('ignore_sheet', r'^_'), re.IGNORECASE)

    def select(self, sheet_titles, environment):
        selected = []
        for title in sheet_titles:
            if self.ignore_sheet.match(title):
                continue
            parts = title.split('.')
            name = parts[0]
            if environment is not None and len(parts) >= 2 and parts[-1].lower() != environment.lower():
                continue
            is_constant = bool(self.constant_sheet.match(name))
            if is_constant or (name != '' and self.script_sheet.match(name)):
                selected.append((title, name, is_constant))
        return selected


def assemble_tables(selected_sheets, values_by_title):
    """環境別の行を連結し、定数を入力順にdeep mergeする。"""
    tables = {}
    constants = {}
    for title, name, is_constant in selected_sheets:
        values = values_by_title.get(title, [])
        if is_constant:
            logging.info(f'loading constant sheet: {title}')
            constants = deep_merge(constants, parse_table(values))
        else:
            logging.info(f'loading script sheet: {title}')
            rows = [
                row if isinstance(row, SourceRow) else SourceRow(row, title, line_no)
                for line_no, row in enumerate(values)
            ]
            tables.setdefault(name, []).extend(rows)
    return list(tables.items()), constants


def convert_value(s):
    # 文字列を変換する
    # まず、strip する
    # 1. 数値型ならそのまま返す
    # 2. "null" なら None に変換
    # 3. "true" または "false" なら bool 型に変換
    # 4. 数値っぽいなら int または float に変換
    # 5. それ以外は文字列として返す
    orig = s
    if isinstance(s, (int, float)):
        return s
    s = s.strip()
    low = s.lower()
    if low == "null":
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if s.lstrip("-").isdigit():
        try:
            return int(s)
        except ValueError:
            pass
    try:
        f = float(s)
        return f
    except ValueError:
        pass
    return s


def parse_table(table):
    env = {}
    i = 0
    n = len(table)
    while i < n:
        row = table[i]
        if row and len(row) > 0:
            var_name = to_hankaku(row[0]).strip().lower()
            if var_name == "" or var_name.startswith(";") or var_name.startswith("；"):
                # コメント行
                i += 1
                continue
            var_type = row[1].strip().lower() if len(row) >= 2 else ""

            if var_type == "value":
                i += 1
                # value 型は2列目が値
                value = None
                while i < n:
                    if (not table[i]) or table[i][0].startswith(";") or table[i][0].startswith("；"):
                        i += 1
                        continue
                    if table[i][0] != "":
                        break
                    value = convert_value(table[i][1])
                    i += 1
                env[var_name] = value
            elif var_type == "list":
                # list 型は2列目が値
                env[var_name] = []
                i += 1
                # 次のヘッダー行まで
                while i < n:
                    if (not table[i]) or table[i][0].startswith(";") or table[i][0].startswith("；"):
                        i += 1
                        continue
                    if table[i][0] != "":
                        break
                    data_row = table[i]
                    if len(data_row) >= 2:
                        value = convert_value(data_row[1])
                        if value != "":
                            env[var_name].append(value)
                    i += 1
            elif var_type == "dict":
                # dict 型は2列目がキー、3列目が値
                env[var_name] = {}
                i += 1
                # 次のヘッダー行まで
                while i < n:
                    if (not table[i]) or table[i][0].startswith(";") or table[i][0].startswith("；"):
                        i += 1
                        continue
                    if table[i][0] != "":
                        break
                    data_row = table[i]
                    if len(data_row) >= 2:
                        key = convert_value(data_row[1])
                        if key:
                            value = None
                            if len(data_row) >= 3:
                                value = convert_value(data_row[2])
                            env[var_name][key] = value
                    i += 1
            elif var_type == "list_table":
                # list_table 型は2列目が空欄で、3列目以降がサブ辞書の値
                sub_keys = [cell.strip() for cell in row[2:]]
                env[var_name] = []
                i += 1
                # 次のヘッダー行まで
                while i < n:
                    if (not table[i]) or table[i][0].startswith(";") or table[i][0].startswith("；"):
                        i += 1
                        continue
                    if table[i][0] != "":
                        break
                    data_row = table[i]
                    # list_table 型は2列目が空欄、3列目以降が値群
                    # 全部空白ならスキップ
                    if len(data_row) > 2 and any([str(cell).strip() for cell in data_row[2:]]):
                        sub_dict = {}
                        for idx, sub_key in enumerate(sub_keys):
                            cell = data_row[idx+2] if idx+2 < len(data_row) else ""
                            sub_dict[sub_key] = convert_value(cell)
                        env[var_name].append(sub_dict)
                    i += 1
            elif var_type == "dict_table":
                # dict_table 型は2列目がキー、3列目以降がサブ辞書の値
                sub_keys = [cell.strip() for cell in row[2:]]
                env[var_name] = {}
                i += 1
                # 次のヘッダー行まで
                while i < n:
                    if (not table[i]) or table[i][0].startswith(";") or table[i][0].startswith("；"):
                        i += 1
                        continue
                    if table[i][0] != "":
                        break
                    data_row = table[i]
                    # table 型は2列目がキー、3列目以降が値群
                    if len(data_row) >= 2:
                        main_key = convert_value(data_row[1])
                        if main_key:
                            sub_dict = {}
                            for idx, sub_key in enumerate(sub_keys):
                                cell = data_row[idx+2] if idx+2 < len(data_row) else ""
                                sub_dict[sub_key] = convert_value(cell)
                            env[var_name][main_key] = sub_dict
                    i += 1
            elif var_type == "":
                raise ValueError(f"型が指定されていません '{var_name}'")
            else:
                raise ValueError(f"不明な型 '{var_type}' です")
        else:
            # ヘッダー行の前など
            i += 1
    return env
