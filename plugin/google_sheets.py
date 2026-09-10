import time
import json
import logging
from urllib.parse import quote

from cloud_backend import create_credential_source
import hub
import utility
import settings
from plugin.scenario_table import SheetSelector, assemble_tables, convert_value, parse_table

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


class GoogleSheetPlugin_Loader:
    def __init__(self, params):
        self.params = params
        self.sheet_selector = SheetSelector(params)
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

        selected = self.sheet_selector.select(sheet_titles, settings.DEPLOY_ENV)
        target_titles = [title for title, _name, _is_constant in selected]
        all_values = self._batch_get_sheet_values(session, spreadsheet_id, target_titles)
        return assemble_tables(selected, all_values)

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
