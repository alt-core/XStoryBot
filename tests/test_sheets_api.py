"""実通信なしでSheets同期の取得範囲と再送境界を確認する。"""

import json
import os
import sys
import types
import unittest
from unittest.mock import Mock, call, patch

import requests

from tools.sheets_api import ApiError, READ_SCOPE, WRITE_SCOPE, SheetsClient


class SheetsApiTest(unittest.TestCase):
    @staticmethod
    def _response(status=200, data=None):
        response = Mock(status_code=status)
        response.json.return_value = {} if data is None else data
        return response

    def _client(self, write=False):
        client = SheetsClient('test-spreadsheet', '/synthetic/credentials.json', write=write)
        client._session = Mock()
        return client

    @staticmethod
    def _properties(index, title=None, columns=26):
        return {'sheetId': index, 'title': title or f'sheet-{index}', 'sheetType': 'GRID',
                'gridProperties': {'rowCount': 1000, 'columnCount': columns}}

    @staticmethod
    def _updates():
        return [{'updateCells': {'range': {'sheetId': 1, 'startRowIndex': 0,
                                         'endRowIndex': 1, 'startColumnIndex': 0, 'endColumnIndex': 1},
                                 'rows': [{'values': [{'userEnteredValue': {'stringValue': '台詞'}}]}],
                                 'fields': 'userEnteredValue'}}]

    def test_明示ファイルの認証は初回だけ行いscopeと自動再送禁止を固定する(self):
        modules = {name: types.ModuleType(name) for name in (
            'google', 'google.auth', 'google.auth.exceptions', 'google.auth.transport',
            'google.auth.transport.requests', 'google.oauth2', 'google.oauth2.service_account')}
        modules['google.auth.exceptions'].GoogleAuthError = type('GoogleAuthError', (Exception,), {})
        credential_factory = Mock()
        modules['google.oauth2.service_account'].Credentials = credential_factory
        modules['google.oauth2'].service_account = modules['google.oauth2.service_account']
        session_factory = Mock()
        session_factory.return_value.headers = {}
        modules['google.auth.transport.requests'].AuthorizedSession = session_factory
        request_factory = Mock()
        modules['google.auth.transport.requests'].Request = request_factory
        with patch.dict(sys.modules, modules):
            for writable, scope in ((False, READ_SCOPE), (True, WRITE_SCOPE)):
                with self.subTest(write=writable):
                    credential_factory.reset_mock()
                    session_factory.reset_mock()
                    request_factory.reset_mock()
                    client = SheetsClient('test', '/synthetic/credentials.json', write=writable)
                    credential_factory.from_service_account_file.assert_not_called()
                    first = client._authorized_session()
                    self.assertIs(first, client._authorized_session())
                    credential_factory.from_service_account_file.assert_called_once_with(
                        '/synthetic/credentials.json', scopes=[scope])
                    session_factory.assert_called_once_with(
                        credential_factory.from_service_account_file.return_value,
                        max_refresh_attempts=0, refresh_timeout=30,
                        auth_request=request_factory.return_value)
                    self.assertEqual('gzip', first.headers['Accept-Encoding'])
                    self.assertIn('gzip', first.headers['User-Agent'])

    def test_APIとtoken取得でnetrcを使わずproxyとCA環境を維持する(self):
        from google.oauth2.credentials import Credentials

        for api_status in (200, 401, 302):
            with self.subTest(api_status=api_status):
                credentials = Credentials(
                    token=None, refresh_token='synthetic-refresh',
                    token_uri='https://oauth2.googleapis.com/token',
                    client_id='synthetic-client', client_secret='synthetic-secret')
                sent = []

                def send(request, **kwargs):
                    sent.append((request, kwargs))
                    response = requests.Response()
                    response.request = request
                    if request.url == 'https://oauth2.googleapis.com/token':
                        response.status_code = 200
                        body = {'access_token': 'synthetic-token', 'expires_in': 3600}
                    else:
                        response.status_code = api_status
                        body = {'spreadsheetId': 'test-spreadsheet'}
                        if api_status == 302:
                            response.headers['Location'] = 'https://example.invalid/sheets'
                    response._content = json.dumps(body).encode('utf-8')
                    return response

                environment = {'HTTPS_PROXY': 'http://proxy.example.invalid:8080',
                               'REQUESTS_CA_BUNDLE': '/synthetic/ca.pem',
                               'NETRC': '/synthetic/netrc'}
                with patch.dict(os.environ, environment, clear=True), \
                        patch('google.oauth2.service_account.Credentials.from_service_account_file',
                              return_value=credentials), \
                        patch('requests.sessions.get_netrc_auth',
                              return_value=('synthetic-user', 'synthetic-password')) as netrc, \
                        patch.object(requests.adapters.HTTPAdapter, 'send', side_effect=send):
                    client = SheetsClient('test-spreadsheet', '/synthetic/credentials.json', write=True)
                    if api_status == 200:
                        client.write_rows(self._updates(), ["'sheet-1'!A1:A1"])
                    else:
                        with self.assertRaises(ApiError) as caught:
                            client.write_rows(self._updates(), ["'sheet-1'!A1:A1"])
                        self.assertEqual(401 if api_status == 401 else None, caught.exception.status)
                        self.assertEqual(api_status == 302, caught.exception.uncertain)
                    netrc.assert_not_called()
                self.assertEqual(2, len(sent))
                token_request, token_options = sent[0]
                api_request, api_options = sent[1]
                self.assertEqual('POST', token_request.method)
                self.assertEqual('POST', api_request.method)
                self.assertNotIn('Authorization', token_request.headers)
                self.assertEqual('Bearer synthetic-token', api_request.headers['Authorization'])
                for options in (token_options, api_options):
                    self.assertEqual(environment['HTTPS_PROXY'], options['proxies']['https'])
                    self.assertEqual(environment['REQUESTS_CA_BUNDLE'], options['verify'])
                client._session.close()

    def test_token取得のredirectでもnetrcを調べず再送しない(self):
        from google.oauth2.credentials import Credentials

        credentials = Credentials(
            token=None, refresh_token='synthetic-refresh',
            token_uri='https://oauth2.googleapis.com/token',
            client_id='synthetic-client', client_secret='synthetic-secret')

        def redirect(request, **kwargs):
            response = requests.Response()
            response.request = request
            response.status_code = 302
            response.headers['Location'] = 'https://example.invalid/token'
            response._content = b'{}'
            return response

        with patch('google.oauth2.service_account.Credentials.from_service_account_file',
                   return_value=credentials), \
                patch('requests.sessions.get_netrc_auth',
                      return_value=('synthetic-user', 'synthetic-password')) as netrc, \
                patch.object(requests.adapters.HTTPAdapter, 'send', side_effect=redirect) as send:
            client = SheetsClient('test-spreadsheet', '/synthetic/credentials.json', write=True)
            with self.assertRaises(ApiError) as caught:
                client.write_rows(self._updates(), ["'sheet-1'!A1:A1"])
            self.assertFalse(caught.exception.uncertain)
            self.assertEqual(1, send.call_count)
            netrc.assert_not_called()
            client._session.close()

    def test_通常以外のsheetもmetadataでは値を取得せず返す(self):
        properties = self._properties(1)
        other = {'sheetId': 2, 'title': '外部データ', 'sheetType': 'DATA_SOURCE'}
        client = self._client()
        client._session.request.return_value = self._response(data={
            'sheets': [{'properties': properties}, {'properties': other}]})
        self.assertEqual([properties, other], client.metadata())
        fields = client._session.request.call_args.kwargs['params']['fields']
        self.assertIn('gridProperties(rowCount,columnCount)', fields)
        self.assertNotIn('rowData', fields)

    def test_五sheetずつ幅を制限して取得し離れたgrid座標と値型を保持する(self):
        properties = [self._properties(index) for index in range(6)]
        properties[0] = self._properties(0, "Writer's story", columns=3)
        sheets = [{'properties': item} for item in properties]
        sheets[0]['data'] = [
            {'rowData': [{'values': [{'userEnteredValue': {'stringValue': '=literal'},
                                     'effectiveValue': {'stringValue': '=literal'}}]}]},
            {'startRow': 2, 'startColumn': 1, 'rowData': [{'values': [
                {'userEnteredValue': {'boolValue': False}},
                {'userEnteredValue': {'numberValue': 0}},
                {'userEnteredValue': {'stringValue': '範囲外'}},
            ]}]},
        ]
        client = self._client()
        client._session.request.side_effect = [self._response(data={'sheets': sheets[:5]}),
                                               self._response(data={'sheets': sheets[5:]})]
        result = list(client.read_sheets(properties))
        self.assertEqual(properties, [item for item, _rows in result])
        self.assertEqual([[{'stringValue': '=literal'}], [], [{}, {'boolValue': False}, {'numberValue': 0}]], result[0][1])
        self.assertEqual([], result[-1][1])
        calls = client._session.request.call_args_list
        self.assertEqual(2, len(calls))
        self.assertEqual(5, len(calls[0].kwargs['params']['ranges']))
        self.assertEqual("'Writer''s story'!A:C", calls[0].kwargs['params']['ranges'][0])
        self.assertEqual(["'sheet-5'!A:Z"], calls[1].kwargs['params']['ranges'])
        fields = calls[0].kwargs['params']['fields']
        self.assertIn('userEnteredValue,effectiveValue', fields)
        self.assertNotIn('Format', fields)

    def test_入力値のない計算結果は位置を示して拒否する(self):
        client = self._client()
        properties = self._properties(1)
        client._session.request.return_value = self._response(data={'sheets': [{
            'properties': properties,
            'data': [{'startRow': 6, 'startColumn': 2, 'rowData': [
                {'values': [{'effectiveValue': {'boolValue': False}}]}]}],
        }]})
        with self.assertRaises(ApiError) as caught:
            list(client.read_sheets([properties]))
        self.assertIn('sheet-1!C7', str(caught.exception))
        self.assertIsNone(caught.exception.status)
        self.assertFalse(caught.exception.uncertain)

    def test_取得中の管理列幅の変更は未取得列を書かないよう停止する(self):
        for requested, returned, allowed in ((3, 4, False), (4, 3, False), (26, 30, True)):
            with self.subTest(requested=requested, returned=returned):
                client = self._client()
                client._session.request.return_value = self._response(data={'sheets': [{
                    'properties': self._properties(1, columns=returned), 'data': [],
                }]})
                result = client.read_sheets([self._properties(1, columns=requested)])
                if allowed:
                    self.assertEqual([(self._properties(1, columns=returned), [])], list(result))
                else:
                    with self.assertRaises(ApiError) as caught:
                        list(result)
                    self.assertIn('再取得', str(caught.exception))
                    self.assertIsNone(caught.exception.status)
                    self.assertFalse(caught.exception.uncertain)

    def test_GETの429と5xxだけを三回まで再試行する(self):
        client = self._client()
        client._session.request.side_effect = [self._response(429), self._response(503),
                                               self._response(data={'sheets': []})]
        with patch('tools.sheets_api.time.sleep') as sleep:
            self.assertEqual([], client.metadata())
        self.assertEqual([call(1), call(2)], sleep.call_args_list)
        self.assertEqual(3, client._session.request.call_count)
        options = client._session.request.call_args.kwargs
        self.assertEqual((10, 120), options['timeout'])
        self.assertEqual(180, options['max_allowed_time'])
        self.assertFalse(options['allow_redirects'])
        client = self._client()
        client._session.request.return_value = self._response(503)
        with patch('tools.sheets_api.time.sleep'), self.assertRaises(ApiError) as caught:
            client.metadata()
        self.assertEqual(503, caught.exception.status)
        self.assertEqual(3, client._session.request.call_count)
        for response in (self._response(401), self._response(403), requests.Timeout()):
            with self.subTest(response=response):
                client = self._client()
                client._session.request.side_effect = response if isinstance(response, Exception) else None
                client._session.request.return_value = response
                with self.assertRaises(ApiError) as caught:
                    client.metadata()
                self.assertFalse(caught.exception.uncertain)
                self.assertEqual(1, client._session.request.call_count)

    def test_POST失敗は再送せず成否不明を区別する(self):
        malformed = self._response()
        malformed.json.side_effect = ValueError('invalid json')
        for response, status, uncertain in (
                (self._response(401), 401, False), (self._response(429), 429, False),
                (self._response(503), 503, True), (requests.Timeout(), None, True),
                (malformed, 200, True)):
            with self.subTest(status=status, uncertain=uncertain):
                client = self._client(write=True)
                client._session.request.side_effect = response if isinstance(response, Exception) else None
                client._session.request.return_value = response
                with self.assertRaises(ApiError) as caught:
                    client.write_rows(self._updates(), ["'sheet-1'!A1:A1"])
                self.assertEqual(status, caught.exception.status)
                self.assertEqual(uncertain, caught.exception.uncertain)
                self.assertEqual(1, client._session.request.call_count)

    def test_書込みは値限定で一回送り座標付き応答をそのまま返す(self):
        client = self._client(write=True)
        response = {'updatedSpreadsheet': {'sheets': [{'properties': {'sheetId': 1},
                    'data': [{'startRow': 7, 'startColumn': 2, 'rowData': []}]}]}}
        client._session.request.return_value = self._response(data=response)
        result = client.write_rows(self._updates(), ["'sheet-1'!A1:A1"])
        self.assertIs(response, result)
        call_args = client._session.request.call_args
        self.assertEqual('POST', call_args.args[0])
        self.assertTrue(call_args.args[1].endswith(':batchUpdate'))
        body = json.loads(call_args.kwargs['data'])
        self.assertEqual(self._updates(), body['requests'])
        self.assertEqual(["'sheet-1'!A1:A1"], body['responseRanges'])
        self.assertTrue(body['includeSpreadsheetInResponse'])
        self.assertTrue(body['responseIncludeGridData'])
        self.assertIn('台詞'.encode('utf-8'), call_args.kwargs['data'])
        self.assertNotIn(b'\\u', call_args.kwargs['data'])
        self.assertFalse(call_args.kwargs['allow_redirects'])
        client = SheetsClient('test', '/synthetic/credentials.json')
        with self.assertRaises(ApiError):
            client.write_rows(self._updates(), [])
        self.assertIsNone(client._session)
        client = self._client(write=True)
        with self.assertRaises(ValueError):
            client.write_rows([{'deleteSheet': {'sheetId': 1}}], [])
        client._session.request.assert_not_called()

    def test_書込みだけを直近六十秒の五十五回に抑え通常の少数送信は待たない(self):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        client = self._client(write=True)
        client._session.request.return_value = self._response(data={'sheets': []})
        updates, ranges = self._updates(), ["'sheet-1'!A1:A1"]
        with patch('tools.sheets_api.time.monotonic', side_effect=lambda: now[0]), \
                patch('tools.sheets_api.time.sleep', side_effect=sleep) as pause:
            for _ in range(55):
                client.write_rows(updates, ranges)
            client.metadata()
            pause.assert_not_called()
            now[0] = 10
            with self.assertLogs(level='INFO') as logs:
                client.write_rows(updates, ranges)
            pause.assert_called_once_with(50)
            self.assertIn('50.0秒', logs.output[0])
            self.assertEqual(57, client._session.request.call_count)

            # 最初の固定window境界を跨いでも、直前の大量送信を忘れない。
            client = self._client(write=True)
            client._session.request.return_value = self._response()
            pause.reset_mock()
            now[0] = 0
            client.write_rows(updates, ranges)
            now[0] = 59
            for _ in range(54):
                client.write_rows(updates, ranges)
            now[0] = 60
            client.write_rows(updates, ranges)
            pause.assert_not_called()
            with self.assertLogs(level='INFO'):
                client.write_rows(updates, ranges)
            pause.assert_called_once_with(59)
            self.assertEqual(57, client._session.request.call_count)


if __name__ == '__main__':
    unittest.main()
