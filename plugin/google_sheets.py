import re
import time
import json
import logging
from urllib.parse import quote

from cloud_backend import create_credential_source
import hub
import utility
import settings
from utility import deep_merge, to_hankaku

# Google Sheets API v4 を service account で直接呼ぶ。
# https://developers.google.com/sheets/api/reference/rest
# 使うのは spreadsheets.get（sheet 一覧）と spreadsheets.values.batchGet だけ。
SHEETS_API = 'https://sheets.googleapis.com/v4/spreadsheets'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
REQUEST_TIMEOUT = (30, 120)  # (接続, 読取) 秒

_sessions = {}
_credential_source = None


class SheetsApiError(Exception):
    """Google Sheets API が 2xx 以外を返したときの例外。"""

    def __init__(self, status_code, message):
        super().__init__(f'Google Sheets API error: status={status_code} message={message}')
        self.status_code = status_code
        self.message = message

    @classmethod
    def from_response(cls, response):
        try:
            message = response.json().get('error', {}).get('message')
        except (ValueError, AttributeError):
            message = None
        return cls(response.status_code, message if message is not None else response.text[:200])


def _get_credential_source():
    global _credential_source
    if _credential_source is None:
        _credential_source = create_credential_source()
    return _credential_source

def _get_google_session(key_file_name):
    """service account で認可済みの requests.Session（token は自動更新）。key file ごとに1つ。"""
    if key_file_name not in _sessions:
        # google-auth を使うのは builder だけなので、ここで import して API／worker の起動を軽くする
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession

        credential_data = (
            _get_credential_source().get_google_service_account(
                key_file_name))
        if credential_data.inline_json is not None:
            credentials = (
                service_account.Credentials.from_service_account_info(
                    json.loads(credential_data.inline_json),
                    scopes=SCOPES))
        else:
            credentials = (
                service_account.Credentials.from_service_account_file(
                    credential_data.file_path,
                    scopes=SCOPES))
        _sessions[key_file_name] = AuthorizedSession(credentials)

    return _sessions[key_file_name]


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

    def get_session(self):
        return _get_google_session(self.params['key_file_json'])

    def _get_json(self, session, path, params, max_attempts=6, base_delay=5):
        """GET して JSON を返す。429 と 5xx は指数バックオフで数回リトライし、他の失敗は SheetsApiError。"""
        delay = base_delay
        for attempt in range(max_attempts):
            response = session.get(f'{SHEETS_API}/{path}', params=params, timeout=REQUEST_TIMEOUT)
            status = response.status_code
            if 200 <= status < 300:
                return response.json()
            retryable = status == 429 or 500 <= status < 600
            if retryable and attempt < max_attempts - 1:
                logging.warning(
                    "Google Sheets API returned %s (attempt %d/%d). Retrying in %.1f seconds.",
                    status,
                    attempt + 1,
                    max_attempts,
                    delay
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise SheetsApiError.from_response(response)

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
                    # 式以外は式文字列側の値を使う。
                    combined_row.append(cell_value)
            combined.append(combined_row)
        return combined

    def _batch_get_values(self, session, spreadsheet_id, ranges, value_render_option):
        result = self._get_json(
            session, f'{quote(spreadsheet_id, safe="")}/values:batchGet',
            {'ranges': ranges, 'valueRenderOption': value_render_option})
        return result.get('valueRanges', [])

    def _batch_get_sheet_values(self, session, spreadsheet_id, sheet_titles):
        """複数シートの値を batchGet で一括取得する"""
        if not sheet_titles:
            return {}

        ranges = [title + "!A:Z" for title in sheet_titles]

        if not self.evaluate_formula:
            # 式は文字列のまま返す（=IMAGE の判定などに利用する）
            value_ranges = self._batch_get_values(session, spreadsheet_id, ranges, "FORMULA")
            return {
                sheet_titles[i]: value_ranges[i].get('values', [])
                for i in range(len(value_ranges))
            }

        # evaluate_formula が True の場合は FORMULA と UNFORMATTED_VALUE の両方を取得
        # UNFORMATTED_VALUE を指定すると数値は数値のまま返るため、後段の convert_value で自然に処理できる
        formula_ranges = self._batch_get_values(session, spreadsheet_id, ranges, "FORMULA")
        value_ranges = self._batch_get_values(session, spreadsheet_id, ranges, "UNFORMATTED_VALUE")

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
        session = self.get_session()
        result = self._get_json(
            session, quote(spreadsheet_id, safe=""),
            {'fields': 'sheets(properties(sheet_id,title))'})
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

        all_values = self._batch_get_sheet_values(session, spreadsheet_id, target_sheet_titles)

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
                    # 同名sheetに環境別の行を追加する。
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
