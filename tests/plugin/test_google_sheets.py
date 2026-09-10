# coding: utf-8

import copy
import importlib.util
from pathlib import Path
import re
import sys
import types
import unicodedata
import unittest
from unittest import mock

# 共有の純粋処理を、下のutility stubへ結び付けない。
from plugin import scenario_table


TARGET = Path(__file__).resolve().parents[2] / 'plugin' / 'google_sheets.py'


def deep_merge(left, right):
    result = copy.deepcopy(left)
    for key, value in right.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_google_sheets_module():
    """Google API を呼ばずに対象モジュールを読み込む。"""
    hub = types.ModuleType('hub')
    hub.register_scenario_loader_factory = mock.Mock()
    utility = types.ModuleType('utility')
    utility.deep_merge = deep_merge
    utility.to_hankaku = lambda value: unicodedata.normalize('NFKC', value)
    utility.merge_params = lambda base, extra: {**base, **extra}
    settings = types.ModuleType('settings')
    settings.DEPLOY_ENV = 'test'
    credential_source = mock.Mock()
    credential_source.get_google_service_account.side_effect = (
        lambda reference: types.SimpleNamespace(
            file_path=reference,
            inline_json=None,
            use_default=False,
        ))
    cloud_backend = types.ModuleType('cloud_backend')
    cloud_backend.create_credential_source = mock.Mock(
        return_value=credential_source)

    module_name = 'tests_target_google_sheets'
    spec = importlib.util.spec_from_file_location(module_name, TARGET)
    module = importlib.util.module_from_spec(spec)
    replacements = {
        'cloud_backend': cloud_backend,
        'hub': hub,
        'utility': utility,
        'settings': settings,
        module_name: module,
    }
    with mock.patch.dict(sys.modules, replacements):
        spec.loader.exec_module(module)
    return module, credential_source


def google_auth_stubs():
    """google-auth の service account 資格情報と AuthorizedSession の最小 stub。"""
    credential_calls = []
    credential_info_calls = []
    session_calls = []

    class Credentials:
        @classmethod
        def from_service_account_file(cls, key_file_name, scopes):
            credential = object()
            credential_calls.append((key_file_name, scopes, credential))
            return credential

        @classmethod
        def from_service_account_info(cls, info, scopes):
            credential = object()
            credential_info_calls.append((info, scopes, credential))
            return credential

    class AuthorizedSession:
        def __init__(self, credentials):
            self.credentials = credentials
            session_calls.append(self)

    google = types.ModuleType('google')
    google.__path__ = []
    oauth2 = types.ModuleType('google.oauth2')
    oauth2.__path__ = []
    service_account = types.ModuleType('google.oauth2.service_account')
    service_account.Credentials = Credentials
    auth = types.ModuleType('google.auth')
    auth.__path__ = []
    transport = types.ModuleType('google.auth.transport')
    transport.__path__ = []
    transport_requests = types.ModuleType('google.auth.transport.requests')
    transport_requests.AuthorizedSession = AuthorizedSession
    modules = {
        'google': google, 'google.oauth2': oauth2,
        'google.oauth2.service_account': service_account,
        'google.auth': auth, 'google.auth.transport': transport,
        'google.auth.transport.requests': transport_requests,
    }
    return modules, credential_calls, credential_info_calls, session_calls


class FakeResponse:
    def __init__(self, status_code, body=None, text=''):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError('not json')
        return self._body


class FakeSession:
    """session.get(url, params=, timeout=) を記録し、用意した応答を順に返す。"""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({'url': url, 'params': params, 'timeout': timeout})
        response = self.responses.pop(0)
        if isinstance(response, FakeResponse):
            return response
        return FakeResponse(200, response)


def sheet_metadata(titles):
    return {'sheets': [
        {'properties': {'sheet_id': index, 'title': title}}
        for index, title in enumerate(titles)
    ]}


class GoogleSheetsPluginTest(unittest.TestCase):
    def setUp(self):
        self.module, self.credential_source = load_google_sheets_module()
        self.module._sessions.clear()

    def test_sessionはkey_fileごとに1つ作りcacheする(self):
        modules, credential_calls, _info_calls, session_calls = google_auth_stubs()
        with mock.patch.dict(sys.modules, modules):
            first = self.module._get_google_session('/keys/sheets.json')
            second = self.module._get_google_session('/keys/sheets.json')

        self.assertIs(first, second)
        self.assertEqual(len(credential_calls), 1)
        key_file_name, scopes, credential = credential_calls[0]
        self.assertEqual(key_file_name, '/keys/sheets.json')
        self.assertEqual(scopes, self.module.SCOPES)
        self.assertEqual([first], session_calls)
        self.assertIs(first.credentials, credential)
        self.credential_source.get_google_service_account.assert_called_once_with(
            '/keys/sheets.json')

    def test_inline_JSONから資格情報を生成する(self):
        self.credential_source.get_google_service_account.return_value = (
            types.SimpleNamespace(
                file_path=None,
                inline_json='{"project_id":"test-project"}',
                use_default=False,
            ))
        self.credential_source.get_google_service_account.side_effect = None
        modules, _calls, credential_info_calls, session_calls = google_auth_stubs()

        with mock.patch.dict(sys.modules, modules):
            session = self.module._get_google_session('/parameter/sheets-key')

        self.assertEqual(len(credential_info_calls), 1)
        info, scopes, credential = credential_info_calls[0]
        self.assertEqual({'project_id': 'test-project'}, info)
        self.assertEqual(self.module.SCOPES, scopes)
        self.assertIs(session.credentials, credential)
        self.assertEqual([session], session_calls)

    def test_module_importではgoogle_authを読み込まない(self):
        # google-auth の import は builder が session を作るときだけ（API／worker の起動を軽くする）
        source = TARGET.read_text(encoding='utf-8')
        top_level_imports = [
            line for line in source.splitlines()
            if line.startswith(('import ', 'from ')) and 'google' in line]
        self.assertEqual([], top_level_imports)

    def test_一時障害の5xxと429は再試行し4xxは即座に伝播する(self):
        loader = self.module.GoogleSheetPlugin_Loader({})
        session = FakeSession([
            FakeResponse(503, text='unavailable'), FakeResponse(429, {'error': {'message': 'quota'}}),
            {'values': [['ok']]}])
        with mock.patch.object(self.module.time, 'sleep') as sleep:
            self.assertEqual({'values': [['ok']]}, loader._get_json(session, 'sheet/values:batchGet', {'ranges': []}))
        self.assertEqual(3, len(session.calls))
        self.assertEqual([mock.call(5), mock.call(10)], sleep.call_args_list)
        self.assertEqual('https://sheets.googleapis.com/v4/spreadsheets/sheet/values:batchGet', session.calls[0]['url'])
        self.assertEqual((30, 120), session.calls[0]['timeout'])

        session = FakeSession([FakeResponse(404, {'error': {'message': 'Requested entity was not found.'}})])
        with mock.patch.object(self.module.time, 'sleep') as sleep:
            with self.assertRaises(self.module.SheetsApiError) as captured:
                loader._get_json(session, 'sheet', {})
        sleep.assert_not_called()
        self.assertEqual(404, captured.exception.status_code)
        self.assertEqual('Requested entity was not found.', captured.exception.message)

    def test_batch_get_preserves_target_order_and_formula_text(self):
        session = FakeSession([{
            'valueRanges': [
                {'values': [['second']]},
                {'values': [['first']]},
                {},
            ],
        }])
        loader = self.module.GoogleSheetPlugin_Loader({
            'key_file_json': '/keys/sheets.json',
            'evaluate_formula': False,
        })

        result = loader._batch_get_sheet_values(
            session, 'spreadsheet', ['second', 'first', 'empty'])

        self.assertEqual(result, {
            'second': [['second']],
            'first': [['first']],
            'empty': [],
        })
        self.assertEqual(session.calls, [{
            'url': 'https://sheets.googleapis.com/v4/spreadsheets/spreadsheet/values:batchGet',
            'params': {
                'ranges': ['second!A:Z', 'first!A:Z', 'empty!A:Z'],
                'valueRenderOption': 'FORMULA',
            },
            'timeout': (30, 120),
        }])

    def test_formula_evaluation_is_opt_in(self):
        loader = self.module.GoogleSheetPlugin_Loader({
            'key_file_json': '/keys/sheets.json',
        })

        self.assertFalse(loader.evaluate_formula)

    def test_不正なシート選択条件は通信前の初期化時に拒否する(self):
        with self.assertRaises(re.error):
            self.module.GoogleSheetPlugin_Loader({'script_sheet': '('})

    def test_formula_evaluation_keeps_image_formula_and_empty_result(self):
        session = FakeSession([
            {
                'valueRanges': [
                    {'values': [
                        ['=1+1', '=IMAGE("https://example.invalid/a.png")', 'plain'],
                        ['=EMPTY()'],
                    ]},
                    {},
                ],
            },
            {
                'valueRanges': [
                    {'values': [[2, 'image-result', 'plain']]},
                    {},
                ],
            },
        ])
        loader = self.module.GoogleSheetPlugin_Loader({
            'key_file_json': '/keys/sheets.json',
            'evaluate_formula': True,
        })

        result = loader._batch_get_sheet_values(
            session, 'spreadsheet', ['story', 'empty'])

        self.assertEqual(result['story'], [
            [2, '=IMAGE("https://example.invalid/a.png")', 'plain'],
            [''],
        ])
        self.assertEqual(result['empty'], [])
        self.assertEqual(session.calls[0]['params']['valueRenderOption'], 'FORMULA')
        self.assertEqual(session.calls[1]['params']['valueRenderOption'], 'UNFORMATTED_VALUE')
        self.assertEqual(session.calls[0]['params']['ranges'], ['story!A:Z', 'empty!A:Z'])

    def test_same_name_environment_sheet_is_extended_in_place(self):
        titles = [
            'story', 'story.test', 'story.prod', '_ignored',
            '$const', '$const.test',
        ]
        values = [
            {'values': [['base-row'], []]},
            {'values': [['test-row-1'], [], ['test-row-2']]},
            {'values': [['base_value', 'value'], ['', 'base']]},
            {'values': [['test_value', 'value'], ['', 'test']]},
        ]
        session = FakeSession([sheet_metadata(titles), {'valueRanges': values}])
        loader = self.module.GoogleSheetPlugin_Loader({
            'key_file_json': '/keys/sheets.json',
            'evaluate_formula': False,
        })
        loader.get_session = lambda: session

        sheets, constants = loader._get_table_from_google_sheets('spreadsheet')

        self.assertEqual(sheets, [(
            'story',
            [['base-row'], [], ['test-row-1'], [], ['test-row-2']],
        )])
        self.assertTrue(all(isinstance(row, list) for row in sheets[0][1]))
        self.assertEqual([
            ('story', 0), ('story', 1),
            ('story.test', 0), ('story.test', 1), ('story.test', 2),
        ], [row.source_position for row in sheets[0][1]])
        self.assertEqual(constants, {
            'base_value': 'base',
            'test_value': 'test',
        })
        self.assertEqual(
            'https://sheets.googleapis.com/v4/spreadsheets/spreadsheet', session.calls[0]['url'])
        self.assertEqual({'fields': 'sheets(properties(sheet_id,title))'}, session.calls[0]['params'])
        self.assertEqual(session.calls[1]['params']['ranges'], [
            'story!A:Z', 'story.test!A:Z', '$const!A:Z', '$const.test!A:Z',
        ])

    def test_環境別sheetが先でも入力順と元sheet名を維持する(self):
        session = FakeSession([
            sheet_metadata(['story.test', 'story']),
            {'valueRanges': [{'values': [['環境別']]}, {'values': [['共通']]}]},
        ])
        loader = self.module.GoogleSheetPlugin_Loader({})
        loader.get_session = lambda: session
        sheets, constants = loader._get_table_from_google_sheets('spreadsheet')
        self.assertEqual([('story', [['環境別'], ['共通']])], sheets)
        self.assertEqual(
            [('story.test', 0), ('story', 0)],
            [row.source_position for row in sheets[0][1]])
        self.assertEqual({}, constants)

    def test_spreadsheet_idはURLのpath用に符号化する(self):
        loader = self.module.GoogleSheetPlugin_Loader({'key_file_json': '/keys/sheets.json'})
        session = FakeSession([sheet_metadata([]), ])
        loader.get_session = lambda: session
        loader._get_table_from_google_sheets('a/b c')
        self.assertEqual(
            'https://sheets.googleapis.com/v4/spreadsheets/a%2Fb%20c', session.calls[0]['url'])


if __name__ == '__main__':
    unittest.main()
