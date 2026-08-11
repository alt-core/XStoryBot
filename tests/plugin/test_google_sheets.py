# coding: utf-8

import copy
import importlib.util
from pathlib import Path
import sys
import types
import unicodedata
import unittest
from unittest import mock


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
    """Google APIを呼ばずに対象モジュールを読み込む。"""
    credential_calls = []
    credential_info_calls = []
    build_calls = []

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

    def build(*args, **kwargs):
        service = object()
        build_calls.append((args, kwargs, service))
        return service

    google = types.ModuleType('google')
    google_oauth2 = types.ModuleType('google.oauth2')
    service_account = types.ModuleType('google.oauth2.service_account')
    service_account.Credentials = Credentials
    google_oauth2.service_account = service_account

    googleapiclient = types.ModuleType('googleapiclient')
    discovery = types.ModuleType('googleapiclient.discovery')
    discovery.build = build
    errors = types.ModuleType('googleapiclient.errors')
    errors.HttpError = type('HttpError', (Exception,), {})

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
        'google': google,
        'google.oauth2': google_oauth2,
        'google.oauth2.service_account': service_account,
        'googleapiclient': googleapiclient,
        'googleapiclient.discovery': discovery,
        'googleapiclient.errors': errors,
        'hub': hub,
        'utility': utility,
        'settings': settings,
        module_name: module,
    }
    with mock.patch.dict(sys.modules, replacements):
        spec.loader.exec_module(module)
    return (
        module,
        credential_calls,
        credential_info_calls,
        build_calls,
        credential_source,
    )


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeValues:
    def __init__(self, batch_results):
        self.batch_results = list(batch_results)
        self.batch_calls = []

    def batchGet(self, **kwargs):
        self.batch_calls.append(kwargs)
        return FakeRequest(self.batch_results.pop(0))


class FakeSpreadsheets:
    def __init__(self, sheet_titles, batch_results):
        self.sheet_titles = sheet_titles
        self.values_api = FakeValues(batch_results)
        self.get_calls = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        sheets = [
            {'properties': {'sheet_id': index, 'title': title}}
            for index, title in enumerate(self.sheet_titles)
        ]
        return FakeRequest({'sheets': sheets})

    def values(self):
        return self.values_api


class FakeService:
    def __init__(self, sheet_titles=(), batch_results=()):
        self.spreadsheets_api = FakeSpreadsheets(sheet_titles, batch_results)

    def spreadsheets(self):
        return self.spreadsheets_api


class GoogleSheetsPluginTest(unittest.TestCase):
    def setUp(self):
        (
            self.module,
            self.credential_calls,
            self.credential_info_calls,
            self.build_calls,
            self.credential_source,
        ) = load_google_sheets_module()

    def test_service_uses_configured_key_file_and_is_cached(self):
        first = self.module._get_google_service('/keys/sheets.json')
        second = self.module._get_google_service('/keys/sheets.json')

        self.assertIs(first, second)
        self.assertEqual(len(self.credential_calls), 1)
        key_file_name, scopes, credential = self.credential_calls[0]
        self.assertEqual(key_file_name, '/keys/sheets.json')
        self.assertEqual(scopes, self.module.SCOPES)
        self.assertEqual(len(self.build_calls), 1)
        args, kwargs, built_service = self.build_calls[0]
        self.assertEqual(args, ('sheets', 'v4'))
        self.assertIs(kwargs['credentials'], credential)
        self.assertIs(first, built_service)
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

        service = self.module._get_google_service('/parameter/sheets-key')

        self.assertEqual(len(self.credential_info_calls), 1)
        info, scopes, credential = self.credential_info_calls[0]
        self.assertEqual({'project_id': 'test-project'}, info)
        self.assertEqual(self.module.SCOPES, scopes)
        self.assertIs(self.build_calls[0][1]['credentials'], credential)
        self.assertIs(service, self.build_calls[0][2])

    def test_batch_get_preserves_target_order_and_formula_text(self):
        service = FakeService(batch_results=[{
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
            service, 'spreadsheet', ['second', 'first', 'empty'])

        self.assertEqual(result, {
            'second': [['second']],
            'first': [['first']],
            'empty': [],
        })
        self.assertEqual(service.spreadsheets_api.values_api.batch_calls, [{
            'spreadsheetId': 'spreadsheet',
            'ranges': ['second!A:Z', 'first!A:Z', 'empty!A:Z'],
            'valueRenderOption': 'FORMULA',
        }])

    def test_formula_evaluation_keeps_image_formula_and_empty_result(self):
        service = FakeService(batch_results=[
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
            service, 'spreadsheet', ['story', 'empty'])

        self.assertEqual(result['story'], [
            [2, '=IMAGE("https://example.invalid/a.png")', 'plain'],
            [''],
        ])
        self.assertEqual(result['empty'], [])
        calls = service.spreadsheets_api.values_api.batch_calls
        self.assertEqual(calls[0]['valueRenderOption'], 'FORMULA')
        self.assertEqual(calls[1]['valueRenderOption'], 'UNFORMATTED_VALUE')
        self.assertEqual(calls[0]['ranges'], ['story!A:Z', 'empty!A:Z'])

    def test_same_name_environment_sheet_is_extended_in_place(self):
        titles = [
            'story', 'story.test', 'story.prod', '_ignored',
            '$const', '$const.test',
        ]
        values = [
            {'values': [['base-row']]},
            {'values': [['test-row-1'], ['test-row-2']]},
            {'values': [['base_value', 'value'], ['', 'base']]},
            {'values': [['test_value', 'value'], ['', 'test']]},
        ]
        service = FakeService(titles, [{'valueRanges': values}])
        loader = self.module.GoogleSheetPlugin_Loader({
            'key_file_json': '/keys/sheets.json',
            'evaluate_formula': False,
        })
        loader.get_service = lambda: service

        sheets, constants = loader._get_table_from_google_sheets('spreadsheet')

        self.assertEqual(sheets, [(
            'story',
            [['base-row'], ['test-row-1'], ['test-row-2']],
        )])
        self.assertEqual(constants, {
            'base_value': 'base',
            'test_value': 'test',
        })
        batch_call = service.spreadsheets_api.values_api.batch_calls[0]
        self.assertEqual(batch_call['ranges'], [
            'story!A:Z', 'story.test!A:Z', '$const!A:Z', '$const.test!A:Z',
        ])


if __name__ == '__main__':
    unittest.main()
