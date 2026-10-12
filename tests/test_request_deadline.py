import base64
import hashlib
import hmac
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from webtest import TestApp

import aws_request_context
import commands
import utility
from cloud_backend.contracts import StateStoreError
from cloud_backend.local.state_store import LocalStateStore
from tests.plugin.test_line_contracts import _load_module, _module


class LambdaDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.request = SimpleNamespace(environ={}, headers={})
        request = patch.object(aws_request_context, 'request', self.request)
        request.start()
        self.addCleanup(request.stop)
        environment = patch.dict(os.environ, {'AWS_LAMBDA_FUNCTION_NAME': 'example-api'})
        environment.start()
        self.addCleanup(environment.stop)

    def test_実際の残時間から終了処理の余裕を引く(self):
        self.request.headers['X-Amzn-Lambda-Context'] = json.dumps({'deadline': 105000})
        with patch.object(aws_request_context.time, 'time', return_value=100), \
                patch.object(aws_request_context.time, 'monotonic', return_value=50):
            self.assertEqual(53, aws_request_context.get_lambda_deadline())

    def test_Lambda以外には期限を追加しない(self):
        with patch.dict(os.environ, {'AWS_LAMBDA_FUNCTION_NAME': ''}):
            self.assertIsNone(aws_request_context.get_lambda_deadline())

    def test_起動待ちで消費したAPI_Gatewayの時間も差し引く(self):
        self.request.headers.update({
            'X-Amzn-Lambda-Context': '{"deadline":130000}',
            'X-Amzn-Request-Context': '{"timeEpoch":90000}',
        })
        with patch.object(aws_request_context.time, 'time', return_value=100), \
                patch.object(aws_request_context.time, 'monotonic', return_value=50):
            self.assertEqual(68, aws_request_context.get_lambda_deadline())

    def test_Lambdaで期限が取得不能なら処理前に停止する(self):
        for raw in ('', 'invalid-json', '[]', '{}', '{"deadline":true}', '{"deadline":"105000"}'):
            with self.subTest(raw=raw):
                self.request.environ.clear()
                self.request.headers['X-Amzn-Lambda-Context'] = raw
                with self.assertRaisesRegex(RuntimeError, '処理期限'):
                    aws_request_context.get_lambda_deadline()

    def test_同一要求のcontextを共有する(self):
        self.request.headers['X-Amzn-Lambda-Context'] = '{"deadline":105000}'
        first = aws_request_context.lambda_context()
        self.request.headers.clear()
        self.assertIs(first, aws_request_context.lambda_context())


class SynchronousEntryTest(unittest.TestCase):
    """実HTTP入口・Runtime・保存状態で世代floorと認証後の期限を確認する。"""

    def setUp(self):
        from plugin.line.interface import LinePlugin_Interface
        from plugin.liff.interface import LiffPlugin_Interface
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = LocalStateStore(directory.name)
        self.now = 0
        self._patch(patch.dict(os.environ, {'AWS_LAMBDA_FUNCTION_NAME': ''}))
        self._patch(patch.object(aws_request_context.time, 'monotonic', side_effect=lambda: self.now))
        self._patch(patch('cloud_backend.create_state_store', return_value=self.store))
        self.models = _load_module('_entry_models', 'models.py', {})
        self._patch(patch('context._player_status_class', self.models.PlayerStatusDB))
        self._patch(patch.object(commands, 'get_runtime_object_dictionary', return_value={}))
        self._patch(patch.object(commands, 'invoke_runtime_construct_response', return_value=False))
        artifacts = {uri: SimpleNamespace(version=3, constants={}, edition=uri)
                     for uri in ('first', 'second')}
        self._patch(patch.dict('sys.modules', {'scenario': _module(
            'scenario', Scenario=SimpleNamespace(load_from_uri=lambda uri: artifacts[uri]))}))
        self.runtime = _load_module('_entry_runtime', 'runtime.py', {
            'settings': _module('settings', CONSTANTS={}, OPTIONS={'scenario_check_interval': 10}),
        })

        class Director:
            def __init__(self, scenario, context):
                self.scenario, self.context = scenario, context

            def plan_reactions(self):
                self.context.status['$edition'] = self.scenario.edition
                self.context.reactions = [([None, self.scenario.edition], None)]

        self.runtime._director_class = Director
        self.line = LinePlugin_Interface('bot', {
            'line_access_token': 'artificial-access', 'line_channel_secret': 'artificial-secret',
        })
        self.line.api = Mock()
        self.liff = LiffPlugin_Interface('bot', {'allow_origin': '*', 'login_channel_id': '123'})
        self.bot = self.runtime.BotRuntime('bot', {'line': self.line, 'liff': self.liff}, None)
        self.bot.check_reload = Mock(side_effect=AssertionError('入口で状態より先に版を選択しない'))
        main = _module('main', get_bot=lambda name: self.bot if name == 'bot' else None)
        self.line_api = _load_module('_entry_line_api', 'plugin/line/webapi.py', {
            'main': main, 'auth': _module('auth'),
        })
        self.liff_api = _load_module('_entry_liff_api', 'plugin/liff/webapi.py', {
            'main': main, 'auth': _module('auth'),
        })
        self._patch(patch.object(self.liff_api, '_line_json', side_effect=self._authenticate))
        self.clients = {'line': TestApp(self.line_api.app), 'liff': TestApp(self.liff_api.app)}
        self.store.save_global_bot_variables('bot', 'first')
        self.bot.select_scenario()
        self._save_generation(1)

    def _patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def _authenticate(self, url, **kwargs):
        return ({'client_id': '123', 'expires_in': 600} if '/verify' in url
                else {'userId': 'Utest'})

    def _save_generation(self, generation):
        player = self.models.PlayerStatusDB('bot', 'line:user,Utest')
        player.scenario_generations = {'bot': generation}
        player.save()

    def _request(self, service, *, status=200, authenticated=True, headers=None):
        headers = dict(headers or {})
        if service == 'line':
            body = json.dumps({'events': [{
                'type': 'message', 'source': {'type': 'user', 'userId': 'Utest'},
                'replyToken': 'artificial-reply', 'message': {'type': 'text', 'text': 'go'},
            }]}).encode('utf-8')
            signature = base64.b64encode(hmac.new(
                b'artificial-secret', body, hashlib.sha256).digest()).decode('ascii')
            headers['X-Line-Signature'] = signature if authenticated else 'invalid'
            path = '/line/callback/bot'
        else:
            body = b'{"action":"go"}'
            headers['Authorization'] = 'Bearer artificial-access' if authenticated else ''
            path = '/liff/bot/message'
        return self.clients[service].post(
            path, body, headers=headers, content_type='application/json',
            status=status, expect_errors=status >= 400)

    def _assert_cache_recovers_for_newer_save(self, service):
        self.now = 10
        with patch.object(self.store, 'get_global_bot_variables', side_effect=StateStoreError('pointer failure')):
            with self.assertRaises(StateStoreError):
                self.bot.select_scenario()
        self.now = 11
        old = self.store.load_player_status('bot:line:user,Utest')
        with patch.object(self.store, 'get_global_bot_variables', wraps=self.store.get_global_bot_variables) as read:
            self._request(service, status=500)
            self._request(service, status=500)
        read.assert_not_called()
        self.assertEqual(old, self.store.load_player_status('bot:line:user,Utest'))
        self.store.save_global_bot_variables('bot', 'second')
        self._save_generation(2)
        with patch.object(self.store, 'get_global_bot_variables', wraps=self.store.get_global_bot_variables) as read:
            self._request(service)
        read.assert_called_once_with('bot')
        saved = self.models.PlayerStatusDB('bot', 'line:user,Utest')
        self.assertEqual('second', saved['$edition'])
        self.assertEqual({'bot': 2}, saved.scenario_generations)
        self.bot.check_reload.assert_not_called()

    def test_LINEで旧saveは負cacheを守り新版saveはTTL前に再確認する(self):
        self._assert_cache_recovers_for_newer_save('line')

    def test_LIFFで旧saveは負cacheを守り新版saveはTTL前に再確認する(self):
        self._assert_cache_recovers_for_newer_save('liff')

    def test_LINE署名とLIFF認証より前に保存先を参照しない(self):
        with patch.object(self.store, 'load_player_status') as player, \
                patch.object(self.store, 'get_global_bot_variables') as pointer:
            self._request('line', authenticated=False, status=401)
            self._request('liff', authenticated=False, status=400)
            with patch.object(self.liff_api, '_line_json', return_value={
                    'client_id': 'other-channel', 'expires_in': 600}):
                self._request('liff', status=401)
        player.assert_not_called()
        pointer.assert_not_called()

    def test_LIFF認証で使った時間をAWS期限から差し引き保存前に止める(self):
        def authenticate(url, **kwargs):
            self.now += 15
            return self._authenticate(url, **kwargs)
        with patch.dict(os.environ, {'AWS_LAMBDA_FUNCTION_NAME': 'example-api'}), \
                patch.object(aws_request_context.time, 'time', side_effect=lambda: 100 + self.now), \
                patch.object(self.liff_api, '_line_json', side_effect=authenticate), \
                patch.object(self.store, 'load_player_status') as player, \
                patch.object(self.store, 'get_global_bot_variables') as pointer, \
                self.assertLogs(level='ERROR'):
            result = self._request('liff', headers={
                'X-Amzn-Lambda-Context': '{"deadline":130000}',
                'X-Amzn-Request-Context': '{"timeEpoch":100000}',
            })
        self.assertEqual('Failure', json.loads(result.text)['result'])
        player.assert_not_called()
        pointer.assert_not_called()


class TokenLogTest(unittest.TestCase):
    def test_既知tokenだけを除き本文と操作と元payloadを保つ(self):
        payload = {'events': [{
            'type': 'postback', 'replyToken': 'private-reply',
            'source': {'type': 'user', 'userId': 'U1'},
            'postback': {'data': 'choose@@private-choice'},
            'videoPlayComplete': {'trackingId': 'private-tracking'},
            'message': {'type': 'text', 'text': '本文@@これは台詞'},
        }]}
        logged = utility.redact_line_payload(payload)
        event = logged['events'][0]
        self.assertEqual('[redacted]', event['replyToken'])
        self.assertEqual('choose@@[redacted]', event['postback']['data'])
        self.assertEqual('[redacted]', event['videoPlayComplete']['trackingId'])
        self.assertEqual('U1', event['source']['userId'])
        self.assertEqual('本文@@これは台詞', event['message']['text'])
        self.assertEqual('private-reply', payload['events'][0]['replyToken'])
        self.assertEqual('choose@@private-choice', payload['events'][0]['postback']['data'])

    def test_出力メッセージとencoded_actionからもtokenを除く(self):
        messages = [{'type': 'template', 'template': {'actions': [
            {'type': 'postback', 'label': '選ぶ', 'data': 'choose@@private-choice'},
        ]}}]
        logged = utility.redact_line_payload(messages)
        self.assertEqual('choose@@[redacted]', logged[0]['template']['actions'][0]['data'])
        self.assertEqual('choose', utility.action_for_log('choose@@private-choice'))


if __name__ == '__main__':
    unittest.main()
