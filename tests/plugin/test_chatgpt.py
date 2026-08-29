# coding: utf-8

import copy
import datetime
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


TARGET = Path(__file__).resolve().parents[2] / 'plugin' / 'chatgpt.py'


def load_chatgpt_module():
    """外部依存を最小の代用品へ差し替えて対象モジュールを読み込む。"""
    commands = types.ModuleType('commands')
    requests = types.ModuleType('requests')
    requests.post = mock.Mock()

    pytz = types.ModuleType('pytz')
    pytz.timezone = lambda _name: datetime.timezone.utc
    pytz.exceptions = types.SimpleNamespace(UnknownTimeZoneError=ValueError)

    module_name = 'tests_target_chatgpt'
    spec = importlib.util.spec_from_file_location(module_name, TARGET)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
        'commands': commands,
        'requests': requests,
        'pytz': pytz,
        module_name: module,
    }):
        spec.loader.exec_module(module)
    return module, requests


class FakeResponse:
    def __init__(self, status_code, content, json_value=None, json_error=None):
        self.status_code = status_code
        self.content = content
        self._json_value = json_value
        self._json_error = json_error
        self.json_calls = 0

    def json(self):
        self.json_calls += 1
        if self._json_error is not None:
            raise self._json_error
        return self._json_value


class Status(dict):
    scene = 'scene-1'


class ChatGPTPluginTest(unittest.TestCase):
    def setUp(self):
        self.module, self.requests = load_chatgpt_module()
        self.runtime = self.module.ChatGPTPlugin_Runtime({
            'api_key': 'secret-api-key',
            'model': 'test-model',
        })

    def test_utf8_json_bytes_are_sent_with_data(self):
        response = FakeResponse(
            200,
            b'{"choices": [{"message": {"content": "ok"}}]}',
            {'choices': [{'message': {'content': 'ok'}}]},
        )
        self.requests.post.return_value = response

        history = [{'role': 'assistant', 'content': '前の返答'}]
        original_history = copy.deepcopy(history)
        result = self.runtime.call_chatgpt_chat('システム', 'こんにちは', history)

        self.assertEqual(result, 'ok')
        self.assertEqual(history, original_history)
        _, kwargs = self.requests.post.call_args
        self.assertNotIn('json', kwargs)
        self.assertEqual(kwargs['timeout'], 120)
        self.assertIsInstance(kwargs['data'], bytes)
        payload = json.loads(kwargs['data'].decode('utf-8'))
        self.assertEqual(payload['model'], 'test-model')
        self.assertEqual(payload['messages'][-1]['content'], 'こんにちは')
        self.assertEqual(response.json_calls, 1)

    def test_request_and_json_errors_propagate(self):
        self.requests.post.side_effect = RuntimeError('接続失敗')
        with self.assertRaisesRegex(RuntimeError, '接続失敗'):
            self.runtime.call_chatgpt_chat('system', 'user')

        self.requests.post.side_effect = None
        self.requests.post.return_value = FakeResponse(
            200, b'not-json', json_error=ValueError('不正JSON'))
        with self.assertRaisesRegex(ValueError, '不正JSON'):
            self.runtime.call_chatgpt_chat('system', 'user')

    def test_non_200_returns_none_and_keeps_response_log(self):
        response = FakeResponse(503, b'service unavailable')
        self.requests.post.return_value = response

        with self.assertLogs(level='INFO') as captured:
            result = self.runtime.call_chatgpt_chat('system', 'user')

        self.assertIsNone(result)
        self.assertEqual(response.json_calls, 0)
        output = '\n'.join(captured.output)
        self.assertIn('503', output)
        self.assertIn("b'service unavailable'", output)
        self.assertNotIn('secret-api-key', output)

    def test_invalid_response_structure_is_logged(self):
        response_json = {'choices': []}
        self.requests.post.return_value = FakeResponse(
            200, b'{"choices": []}', response_json)

        with self.assertLogs(level='INFO') as captured:
            result = self.runtime.call_chatgpt_chat('system', 'user')

        self.assertIsNone(result)
        self.assertIn("{'choices': []}", '\n'.join(captured.output))

    def test_json_command_logs_parse_failure_and_invalid_value(self):
        context = types.SimpleNamespace(
            status=Status(),
            reactions=[],
            user='line:user-1',
        )

        self.runtime.call_chatgpt_chat = mock.Mock(return_value='not-json')
        with self.assertLogs(level='ERROR') as captured:
            handled = self.runtime.run_command(
                context, None, '@chatgptjson', ['system', 'user', 'answer'])
        self.assertTrue(handled)
        self.assertFalse(context.status['$$result'])
        self.assertIn('not-json', '\n'.join(captured.output))

        self.runtime.call_chatgpt_chat = mock.Mock(return_value='{"answer": []}')
        with self.assertLogs(level='WARNING') as captured:
            handled = self.runtime.run_command(
                context, None, '@chatgptjson', ['system', 'user', 'answer'])
        self.assertTrue(handled)
        self.assertFalse(context.status['$$result'])
        self.assertIn('answer: []', '\n'.join(captured.output))


if __name__ == '__main__':
    unittest.main()
