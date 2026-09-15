"""Sheets同期用の小さいHTTP境界。既存ローダーの認証設定とは共有しない。"""

from collections import deque
import json
import logging
import os
from pathlib import Path
import time
from urllib.parse import quote

import requests


API_ROOT = 'https://sheets.googleapis.com/v4/spreadsheets'
READ_SCOPE = 'https://www.googleapis.com/auth/spreadsheets.readonly'
WRITE_SCOPE = 'https://www.googleapis.com/auth/spreadsheets'
REQUEST_TIMEOUT = (10, 120)
READ_BATCH_SIZE = 5
PROPERTIES_FIELDS = 'sheetId,title,sheetType,gridProperties(rowCount,columnCount)'
READ_FIELDS = (
    'sheets(properties(' + PROPERTIES_FIELDS + '),'
    'data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue))))'
)
WRITE_FIELDS = (
    'spreadsheetId,replies,updatedSpreadsheet(sheets(properties(sheetId,title),'
    'data(startRow,startColumn,rowData(values(userEnteredValue)))))'
)


class ApiError(RuntimeError):
    """HTTP状態と、書込み結果が不明かどうかを呼出し側へ渡す。"""

    def __init__(self, message, status=None, uncertain=False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


class _GoogleAuthOnly(requests.auth.AuthBase):
    """proxy・CA設定は使い、netrcでGoogleの認証を上書きさせない。"""

    def __call__(self, request):
        return request


class SheetsClient:
    def __init__(self, sheet_id, credentials_path, write=False):
        if not isinstance(sheet_id, str) or not sheet_id.strip():
            raise ValueError('spreadsheet IDを指定してください')
        if (not isinstance(credentials_path, (str, os.PathLike))
                or not str(credentials_path).strip()
                or str(credentials_path).lstrip().startswith(('{', '['))):
            raise ValueError('資格情報はJSON本文ではなくファイルpathで指定してください')
        self.sheet_id = sheet_id
        self.credentials_path = str(Path(credentials_path).expanduser().resolve())
        self.write_enabled = write
        self._url = f'{API_ROOT}/{quote(sheet_id, safe="")}'
        self._session = None
        self._auth_errors = ()
        self._write_times = deque(maxlen=55)

    def _authorized_session(self):
        if self._session is None:
            try:
                from google.auth.exceptions import GoogleAuthError
                from google.auth.transport.requests import AuthorizedSession, Request
                from google.oauth2 import service_account

                self._auth_errors = (GoogleAuthError,)
                credentials = service_account.Credentials.from_service_account_file(
                    self.credentials_path,
                    scopes=[WRITE_SCOPE if self.write_enabled else READ_SCOPE])
                auth_session = requests.Session()
                auth_session.auth = _GoogleAuthOnly()
                auth_session.max_redirects = 0
                auth_request = Request(session=auth_session)
                # 401後の認証更新によるPOSTの再送も行わない。
                self._session = AuthorizedSession(
                    credentials, max_refresh_attempts=0, refresh_timeout=30,
                    auth_request=auth_request)
                self._session.auth = _GoogleAuthOnly()
                self._session.max_redirects = 0
                self._session.headers.update({
                    'Accept-Encoding': 'gzip',
                    'User-Agent': 'XStoryBot Sheets sync (gzip)',
                })
            except ImportError as error:
                raise ApiError('Google認証の依存packageがありません') from error
            except Exception as error:
                raise ApiError('指定したGoogle資格情報ファイルを読み込めません') from error
        return self._session

    def _pace_write(self):
        if len(self._write_times) == self._write_times.maxlen:
            remaining = 60 - (time.monotonic() - self._write_times[0])
            if remaining > 0:
                logging.info('Sheetsの書込み制限を避けるため%.1f秒待機します', remaining)
                time.sleep(remaining)
        self._write_times.append(time.monotonic())

    def _request(self, method, suffix='', params=None, body=None):
        session = self._authorized_session()
        attempts = 3 if method == 'GET' else 1
        data = None if body is None else json.dumps(
            body, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if method == 'POST':
            self._pace_write()
        for attempt in range(attempts):
            try:
                response = session.request(
                    method, self._url + suffix, params=params, data=data,
                    headers={'Content-Type': 'application/json; charset=utf-8'},
                    timeout=REQUEST_TIMEOUT, max_allowed_time=180, allow_redirects=False)
            except self._auth_errors as error:
                raise ApiError('Google認証に失敗しました。指定した資格情報を確認してください') from error
            except requests.RequestException as error:
                raise ApiError('Google Sheetsとの通信に失敗しました',
                               uncertain=method == 'POST') from error
            status = response.status_code
            if (method == 'GET' and (status == 429 or 500 <= status < 600)
                    and attempt + 1 < attempts):
                response.close()
                time.sleep(2 ** attempt)
                continue
            if not 200 <= status < 300:
                message = f'Google Sheets APIがHTTP {status}を返しました'
                try:
                    detail = response.json().get('error', {}).get('message')
                    if isinstance(detail, str):
                        message += ': ' + detail[:500]
                except (ValueError, AttributeError):
                    pass
                raise ApiError(message, status=status,
                               uncertain=method == 'POST' and status >= 500)
            try:
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError('JSON objectではありません')
            except ValueError as error:
                raise ApiError('Google Sheets APIの応答JSONが不正です', status=status,
                               uncertain=method == 'POST') from error
            return result

    def metadata(self):
        result = self._request('GET', params={
            'fields': f'sheets(properties({PROPERTIES_FIELDS}))',
        })
        return [sheet['properties'] for sheet in result.get('sheets', [])]

    def read_sheets(self, properties):
        """5シートずつ取得し、A列・1行目を基点とする入力値だけを返す。"""
        properties = list(properties)
        for offset in range(0, len(properties), READ_BATCH_SIZE):
            selected = properties[offset:offset + READ_BATCH_SIZE]
            ranges = []
            widths = {}
            for item in selected:
                width = min(26, item.get('gridProperties', {}).get('columnCount', 0))
                if width < 1:
                    raise ApiError('取得対象に列のある通常シートを指定してください')
                title = item['title'].replace("'", "''")
                ranges.append(f"'{title}'!A:{chr(ord('A') + width - 1)}")
                widths[item['sheetId']] = width
            result = self._request('GET', params={'ranges': ranges, 'fields': READ_FIELDS})
            returned = {sheet['properties']['sheetId']: sheet for sheet in result.get('sheets', [])}
            for item in selected:
                sheet = returned.get(item['sheetId'])
                if sheet is None:
                    raise ApiError(f"取得対象のシートが応答にありません: {item['title']}")
                columns = sheet['properties'].get('gridProperties', {}).get('columnCount')
                if type(columns) is not int or min(columns, 26) != widths[item['sheetId']]:
                    raise ApiError(f"取得中にシートの列数が変わりました。再取得してください: {item['title']}")
                yield sheet['properties'], self._read_rows(sheet, widths[item['sheetId']])

    @staticmethod
    def _read_rows(sheet, width):
        rows = []
        title = sheet['properties']['title']
        for block in sheet.get('data', []):
            start_row = block.get('startRow', 0)
            start_column = block.get('startColumn', 0)
            for index, row in enumerate(block.get('rowData', []), start_row):
                cells = row.get('values', [])[:max(0, width - start_column)]
                if not cells:
                    continue
                while len(rows) <= index:
                    rows.append([])
                while len(rows[index]) < start_column + len(cells):
                    rows[index].append({})
                for column, cell in enumerate(cells, start_column):
                    entered = cell.get('userEnteredValue') or {}
                    if not entered and cell.get('effectiveValue'):
                        position = f'{title}!{chr(ord("A") + column)}{index + 1}'
                        raise ApiError(f'数式の自動展開など、入力値のない計算結果は取得できません: {position}')
                    rows[index][column] = entered
        return rows

    def write_rows(self, requests, response_ranges):
        """値だけを一度書き込み、座標を含む応答を呼出し側の照合へ渡す。"""
        if not self.write_enabled:
            raise ApiError('このSheetsセッションは読取り専用です')
        if not requests or any(
                set(item) != {'updateCells'}
                or item['updateCells'].get('fields') != 'userEnteredValue'
                for item in requests):
            raise ValueError('書込みにはuserEnteredValueだけのUpdateCellsを指定してください')
        return self._request('POST', ':batchUpdate', params={'fields': WRITE_FIELDS}, body={
            'requests': requests,
            'includeSpreadsheetInResponse': True,
            'responseRanges': response_ranges,
            'responseIncludeGridData': True,
        })
