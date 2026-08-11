import re
import time
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import logging

import hub
import utility
import settings
from utility import deep_merge, to_hankaku

_google_services = {}

SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']

def _get_google_service(key_file_name):
    if key_file_name not in _google_services:
        # 認証情報の作成
        credentials = service_account.Credentials.from_service_account_file(
            key_file_name,
            scopes=SCOPES
        )
        _google_services[key_file_name] = build('sheets', 'v4', credentials=credentials)

    return _google_services[key_file_name]


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


class GoogleSheetPlugin_Loader:
    def __init__(self, params):
        self.params = params
        self.script_sheet = re.compile(self.params.get('script_sheet', r'^[^$]'), re.IGNORECASE)
        self.constant_sheet = re.compile(self.params.get('constant_sheet', r'^\$'), re.IGNORECASE)
        self.ignore_sheet = re.compile(self.params.get('ignore_sheet', r'^_'), re.IGNORECASE)
        self.evaluate_formula = bool(self.params.get('evaluate_formula', False))

    def get_service(self):
        return _get_google_service(self.params['key_file_json'])

    def _execute_with_retry(self, request, max_attempts=6, base_delay=5):
        # Google Sheets API から 429 (Too Many Requests) が返った場合に備えて、指数バックオフで数回リトライする
        delay = base_delay
        for attempt in range(max_attempts):
            try:
                return request.execute()
            except HttpError as exc:
                if exc.resp is not None and exc.resp.status == 429 and attempt < max_attempts - 1:
                    logging.warning(
                        "Google Sheets API rate limit hit (attempt %d/%d). Retrying in %.1f seconds.",
                        attempt + 1,
                        max_attempts,
                        delay
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise

    def _get_sheet_values(self, service, spreadsheet_id, sheet_title):
        if not self.evaluate_formula:
            # evaluate_formula が False の場合は従来どおり、式の文字列をそのまま返す
            request = service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=sheet_title + "!A:Z",
                valueRenderOption="FORMULA"
            )
            result = self._execute_with_retry(request)
            return result.get('values', [])
        # evaluate_formula が True の場合だけ、式セルを評価した結果に差し替える
        # この場合でも API 呼び出し回数は 2 回なので、負荷が極端に跳ね上がるわけではない
        return self._get_sheet_values_with_formula(service, spreadsheet_id, sheet_title)

    def _get_sheet_values_with_formula(self, service, spreadsheet_id, sheet_title):
        # まず式の文字列表現を取得する（=IMAGE の判定などに利用する）
        formula_request = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=sheet_title + "!A:Z",
            valueRenderOption="FORMULA"
        )
        formula_result = self._execute_with_retry(formula_request).get('values', [])
        # 続けて式の評価結果を取得する（=IMAGE 以外のセルはこちらを利用する）
        # UNFORMATTED_VALUE を指定すると数値は数値のまま返るため、後段の convert_value で自然に処理できる
        value_request = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=sheet_title + "!A:Z",
            valueRenderOption="UNFORMATTED_VALUE"
        )
        value_result = self._execute_with_retry(value_request)
        values = value_result.get('values', [])

        def is_formula(cell_value):
            # Google Sheets では '=foo' と入力すれば文字列扱いになるため、先頭 '=' だけで十分に式判定できる
            if not isinstance(cell_value, str):
                return False
            stripped = cell_value.strip()
            if not stripped.startswith("="):
                return False
            # =IMAGE のケースだけはプレビュー用として式文字列を保ちたいので除外する
            return not stripped.upper().startswith("=IMAGE")

        # 式でない行はそのまま、式行は評価済みの値を返す
        return self._combine_formula_values(sheet_title, formula_result, values, is_formula)

    def _combine_formula_values(self, sheet_title, formula_result, values, is_formula):
        combined = []
        for row_idx, row in enumerate(formula_result):
            # 1 行ずつ走査し、式セルなら評価結果に差し替える
            combined_row = []
            for col_idx, cell_value in enumerate(row):
                if is_formula(cell_value):
                    evaluated = ""
                    if row_idx < len(values) and col_idx < len(values[row_idx]):
                        # 評価結果は values 側にも存在する場合だけ使用し、足りない場合は空文字扱いにする
                        # 式を評価した結果、値が空欄になったケースでは values 側が刈り込まれている場合があるため
                        evaluated = values[row_idx][col_idx]
                    combined_row.append(evaluated)
                    logging.info(f"Evaluated formula at {sheet_title}!R{row_idx+1}C{col_idx+1}: '{cell_value}' => '{evaluated}'")
                else:
                    # 式でなければそのままの値を利用するため、従来と完全に同じ挙動になる
                    combined_row.append(cell_value)
            combined.append(combined_row)
        return combined

    def _batch_get_sheet_values(self, service, spreadsheet_id, sheet_titles):
        """複数シートの値を batchGet で一括取得する"""
        if not sheet_titles:
            return {}

        ranges = [title + "!A:Z" for title in sheet_titles]

        if not self.evaluate_formula:
            request = service.spreadsheets().values().batchGet(
                spreadsheetId=spreadsheet_id,
                ranges=ranges,
                valueRenderOption="FORMULA"
            )
            result = self._execute_with_retry(request)
            value_ranges = result.get('valueRanges', [])
            return {
                sheet_titles[i]: value_ranges[i].get('values', [])
                for i in range(len(value_ranges))
            }

        # evaluate_formula が True の場合は FORMULA と UNFORMATTED_VALUE の両方を取得
        formula_request = service.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=ranges,
            valueRenderOption="FORMULA"
        )
        formula_result = self._execute_with_retry(formula_request)
        formula_ranges = formula_result.get('valueRanges', [])

        value_request = service.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=ranges,
            valueRenderOption="UNFORMATTED_VALUE"
        )
        value_result = self._execute_with_retry(value_request)
        value_ranges = value_result.get('valueRanges', [])

        def is_formula(cell_value):
            # Google Sheets では '=foo' と入力すれば文字列扱いになるため、先頭 '=' だけで十分に式判定できる
            if not isinstance(cell_value, str):
                return False
            stripped = cell_value.strip()
            if not stripped.startswith("="):
                return False
            # =IMAGE のケースだけはプレビュー用として式文字列を保ちたいので除外する
            return not stripped.upper().startswith("=IMAGE")

        result = {}
        for i, sheet_title in enumerate(sheet_titles):
            formula_values = formula_ranges[i].get('values', []) if i < len(formula_ranges) else []
            evaluated_values = value_ranges[i].get('values', []) if i < len(value_ranges) else []
            result[sheet_title] = self._combine_formula_values(sheet_title, formula_values, evaluated_values, is_formula)

        return result

    def _get_table_from_google_sheets(self, spreadsheet_id):
        logging.info(f"loading google sheet: {spreadsheet_id}")
        service = self.get_service()
        request = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets(properties(sheet_id,title))"
        )
        result = self._execute_with_retry(request)
        sheet_titles = [sheet_prop['properties']['title'] for sheet_prop in result.get('sheets', [])]

        # 対象シートを収集してbatchGetで一括取得
        target_sheet_titles = []
        for sheet_title in sheet_titles:
            if self.ignore_sheet.match(sheet_title):
                continue
            sheet_parts = sheet_title.split('.')
            parsed_sheet_title = sheet_parts[0]
            if len(sheet_parts) >= 2:
                sheet_env = sheet_parts[-1]
                if sheet_env.lower() != settings.DEPLOY_ENV.lower():
                    continue
            if self.constant_sheet.match(parsed_sheet_title) or (parsed_sheet_title != "" and self.script_sheet.match(parsed_sheet_title)):
                target_sheet_titles.append(sheet_title)

        all_values = self._batch_get_sheet_values(service, spreadsheet_id, target_sheet_titles)

        sheets = []
        constants = {}
        for sheet_title in sheet_titles:
            if self.ignore_sheet.match(sheet_title):
                # ignore 対象はスキップ
                continue

            sheet_parts = sheet_title.split('.')
            parsed_sheet_title = sheet_parts[0]
            if len(sheet_parts) >= 2:
                # . が付いていたら、末尾と実行環境を比較
                sheet_env = sheet_parts[-1]
                if sheet_env.lower() != settings.DEPLOY_ENV.lower():
                    continue

            if self.constant_sheet.match(parsed_sheet_title):
                logging.info(f"loading constant sheet: {sheet_title}")

                # 定数シートの読み込み
                sheet_values = all_values.get(sheet_title, [])
                constants = deep_merge(constants, parse_table(sheet_values))
            elif parsed_sheet_title != "" and self.script_sheet.match(parsed_sheet_title):
                logging.info(f"loading script sheet: {sheet_title}")

                # スクリプトシートの読み込み
                sheet_values = all_values.get(sheet_title, [])
                if parsed_sheet_title not in [s[0] for s in sheets]:
                    sheets.append((parsed_sheet_title, sheet_values))
                else:
                    # sheets 内に sheet_title が存在していたら、sheets の既存エントリに追加(環境別のエントリを追加するレアケース)
                    for s in sheets:
                        if s[0] == parsed_sheet_title:
                            s[1].extend(sheet_values)

        #import pprint
        #pprint.pprint(constants)
        #import logging
        #logging.info(f"Constants: {constants}")
        return sheets, constants

    def load_scenario(self):
        return self._get_table_from_google_sheets(self.params['sheet_id'])


class GoogleSheetPlugin_LoaderFactory:
    def __init__(self, params):
        self.params = params

    def create_loader(self, params):
        return GoogleSheetPlugin_Loader(utility.merge_params(self.params, params))


def load_plugin(params):
    factory = GoogleSheetPlugin_LoaderFactory(params)
    hub.register_scenario_loader_factory(
        type_name="google_sheets",
        factory=factory
    )


# if __name__ == "__main__":
#     sheet_id = list(settings.BOTS.values())[0]['sheet_id']
#     sheets = get_table_from_google_sheets(sheet_id)
#     for title, table in sheets:
#         print(title)
#         print(utility.table_to_str(table))
