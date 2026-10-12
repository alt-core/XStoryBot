# coding: utf-8
"""LINE プラグインの公開版で維持する局所的な契約を確認する。

送信 JSON の同一性は tests/plugin/test_line_wire.py（golden）で、API 呼出しは
test_line_api.py で、署名検証は test_line_webhook.py で確認する。ここでは event から
action への変換、送信失敗の分類と再試行、Webhook callback の HTTP 契約を扱う。
"""

import base64
import copy
from contextlib import ExitStack
import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import requests
from webtest import TestApp

from cloud_backend import factory as backend_factory
from cloud_backend.contracts import RollbackConflictError, SaveResultUnknownError
from plugin.line import api as line_api
from plugin.line.api import LineApiError
from plugin.line.webhook import InvalidSignatureError
from scenario_selection import ScenarioSelection
import utility as utility_module


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MISSING = object()


def _module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _load_module(name, relative_path, replacements):
    """外部依存を一時的に差し替え、production module だけを読み込む。"""
    previous = {key: sys.modules.get(key, _MISSING) for key in replacements}
    previous[name] = sys.modules.get(name, _MISSING)
    try:
        sys.modules.update(replacements)
        spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relative_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in {**previous}.items():
            if value is _MISSING:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


def _base_stubs():
    commands = _module(
        'commands',
        CommandEntry=lambda **kwargs: SimpleNamespace(**kwargs),
        Default_Builder=lambda: object(),
        register_commands=Mock(),
        register_command=Mock(),
        get_command=Mock(),
        invoke_runtime_construct_response=Mock(return_value=False),
    )
    utility = _module(
        'utility',
        safe_list_get=lambda values, index, default=None: (
            values[index] if index < len(values) else default),
        merge_params=lambda base, extra: {**base, **extra},
        extract_params=lambda values, names: {
            name: values[name] for name in names if name in values},
        deep_dump=Mock(),
        parse_url=lambda value: re.match(r'^(https?|tel):', value or ''),
        encode_action_string=lambda value, **_kwargs: value,
        decode_action_string=lambda value: (value, {}),
        encode_line_video_tracking_id=(
            utility_module.encode_line_video_tracking_id),
        decode_line_video_tracking_id=(
            utility_module.decode_line_video_tracking_id),
        redact_line_payload=lambda value: utility_module.redact_line_payload(value),
        sanitize_action=lambda value: (
            ' ' + value if value.startswith(('*', '＊', '#', '＃', ':', '：')) else value),
    )
    return {
        'hub': _module('hub', register_interface_factory=Mock(), register_handler=Mock()),
        'commands': commands,
        'utility': utility,
    }


class _ActionContext:
    def __init__(self, bot_name, service_name, interface, user, action, attrs):
        self.bot_name = bot_name
        self.service_name = service_name
        self.interface = interface
        self.user = user
        self.action = action
        self.attrs = attrs


def _interface_replacements():
    return {
        **_base_stubs(),
        'common_commands': _module(
            'common_commands', AUDIO_CMDS=('@audio',),
            IMAGE_CMDS=('@image',), VIDEO_CMDS=('@video',),
            RAWIMAGE_CMDS=('@rawimage',)),
        'context': _module('context', ActionContext=_ActionContext),
        'users': _module('users', User=lambda service, user_id: SimpleNamespace(
            service=service, user_id=user_id)),
    }


class _HttpAbort(Exception):
    def __init__(self, status, body):
        super().__init__(status, body)
        self.status = status
        self.body = body


class _Bottle:
    def post(self, _path):
        return lambda function: function


class LineRuntimeImportTest(unittest.TestCase):
    def test_LINE経路のimportにSDK系packageが含まれない(self):
        code = (
            'import sys; '
            'import plugin.line.interface, plugin.line.default_commands, '
            'plugin.line.api, plugin.line.webhook, plugin.line.messages; '
            "print(sorted(name for name in ('linebot', 'pydantic', 'aiohttp') if name in sys.modules))")
        result = subprocess.run(
            [sys.executable, '-c', code], cwd=PROJECT_ROOT,
            capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('[]', result.stdout.strip())


class LineTemplateWebhookTest(unittest.TestCase):
    def test_templateの遅延設定は27秒超過を警告してHTTP処理を継続する(self):
        with patch.dict(os.environ, {
            'LINE_ACCESS_TOKEN': 'artificial-access-token',
            'LINE_CHANNEL_SECRET': 'artificial-channel-secret',
        }, clear=True):
            settings = utility_module.load_settings_yaml(
                PROJECT_ROOT / 'settings.yaml.template')['*']
        interface_module = _load_module(
            '_line_template_interface', 'plugin/line/interface.py', {})
        factory = interface_module.LinePlugin_InterfaceFactory(
            utility_module.merge_params(settings['options'], settings['plugins']['line']))
        interface = factory.create_interface(
            'bot', settings['bots']['bot']['interfaces'][0]['params'])
        self.assertEqual(interface.line_abort_duration_ms, 27000)
        self.assertTrue(interface.line_abort_duration_dont_break)
        bot = SimpleNamespace(
            get_interface=Mock(return_value=interface),
            check_reload=Mock(), handle_action=Mock(),
        )
        webapi = _load_module(
            '_line_template_webapi', 'plugin/line/webapi.py', {
                'main': _module('main', get_bot=Mock(return_value=bot)),
                'auth': _module('auth'),
            })
        client = TestApp(webapi.app)
        for delay_ms in (27000, 28000):
            with self.subTest(delay_ms=delay_ms):
                bot.handle_action.reset_mock()
                body = json.dumps({'events': [{
                    'type': 'message', 'timestamp': 1000000 - delay_ms,
                    'replyToken': 'artificial-reply-token',
                    'source': {'type': 'user', 'userId': 'Utest'},
                    'message': {'id': 'message-1', 'type': 'text', 'text': '開始'},
                }]}, ensure_ascii=False).encode('utf-8')
                signature = base64.b64encode(hmac.new(
                    interface.line_channel_secret.encode('utf-8'), body, hashlib.sha256,
                ).digest()).decode('ascii')
                with (
                    patch.object(webapi.time, 'time', return_value=1000),
                    patch.object(webapi.logging, 'warning') as warning,
                ):
                    result = client.post('/line/callback/bot', body,
                        headers={'X-Line-Signature': signature},
                        content_type='application/json', status=200)
                self.assertEqual(result.status_int, 200)
                bot.handle_action.assert_called_once()
                context = bot.handle_action.call_args.args[0]
                self.assertIsInstance(context, interface_module.LinePlugin_ActionContext)
                self.assertEqual(context.action, '開始')
                if delay_ms > 27000:
                    warning.assert_called_once_with(
                        '[LINE] webhook delivery delay exceeded limit; continue: 28000')
                else:
                    warning.assert_not_called()


class LineWebhookContractTest(unittest.TestCase):
    def setUp(self):
        self.secret = 'channel-secret'
        self.trace = []
        self.request = SimpleNamespace(headers={}, body=io.BytesIO())
        self.response = SimpleNamespace(content_type=None)

        def parse_webhook(body, signature):
            self.trace.append('parse')
            expected = base64.b64encode(hmac.new(
                self.secret.encode('utf-8'), body, hashlib.sha256
            ).digest()).decode('ascii')
            if not hmac.compare_digest(signature, expected):
                raise InvalidSignatureError()
            return [{'type': 'message'}]

        self.interface = SimpleNamespace(
            parse_webhook=parse_webhook,
            line_abort_duration_ms=0,
            line_abort_duration_dont_break=False,
            create_context_from_line_event=Mock(return_value=None),
        )
        self.bot = SimpleNamespace(
            get_interface=Mock(return_value=self.interface),
            check_reload=Mock(side_effect=lambda: self.trace.append('reload')),
            handle_action=Mock(),
        )
        bottle = _module(
            'bottle', request=self.request, response=self.response, Bottle=_Bottle,
            abort=lambda status, body: (_ for _ in ()).throw(_HttpAbort(status, body)),
        )
        utility = _module(
            'utility',
            make_error_json=lambda status, message: json.dumps({'status': status, 'message': message}),
            make_ok_json=lambda message: json.dumps({'message': message}),
            redact_line_payload=lambda value: utility_module.redact_line_payload(value),
        )
        replacements = {
            'bottle': bottle,
            'auth': _module('auth'),
            'utility': utility,
            'main': _module('main', get_bot=Mock(return_value=self.bot)),
            'users': _module('users'),
            'aws_request_context': _module('aws_request_context', get_lambda_deadline=lambda: None),
        }
        self.webapi = _load_module(
            '_line_contract_webapi', 'plugin/line/webapi.py', replacements)

    def _set_request(self, body, signature=_MISSING):
        self.request.body = io.BytesIO(body.encode('utf-8'))
        self.request.headers = {}
        if signature is not _MISSING:
            self.request.headers['X-Line-Signature'] = signature

    def test_missing_signature_is_rejected_before_parser_and_log(self):
        self._set_request('{"events":[]}')
        with patch.object(self.webapi.logging, 'info') as info:
            with self.assertRaises(_HttpAbort) as error:
                self.webapi.callback('bot')
        self.assertEqual(401, error.exception.status)
        self.assertEqual([], self.trace)
        info.assert_not_called()

    def test_undecodable_signature_header_is_rejected_as_invalid_signature(self):
        # Bottle は header 値を latin-1 → UTF-8 と読み直し、不正なバイト列で UnicodeDecodeError を上げる
        class _UndecodableHeaders(dict):
            def get(self, _name, default=None):
                raise UnicodeDecodeError('utf-8', b'\xe9', 0, 1, 'unexpected end of data')

        self.request.body = io.BytesIO(b'{"events":[]}')
        self.request.headers = _UndecodableHeaders()
        with patch.object(self.webapi.logging, 'info') as info:
            with self.assertRaises(_HttpAbort) as error:
                self.webapi.callback('bot')
        self.assertEqual(401, error.exception.status)
        self.assertEqual([], self.trace)
        info.assert_not_called()

    def test_nonempty_invalid_signature_is_rejected_by_parser_without_body_log(self):
        self._set_request('{"events":[]}', 'invalid-signature')
        with patch.object(self.webapi.logging, 'info') as info:
            with self.assertRaises(_HttpAbort) as error:
                self.webapi.callback('bot')
        self.assertEqual(401, error.exception.status)
        self.assertEqual(['parse'], self.trace)
        info.assert_not_called()

    def test_valid_signature_logs_body_only_after_parse_without_early_reload(self):
        body = '{"events":[]}'
        signature = base64.b64encode(hmac.new(
            self.secret.encode('utf-8'), body.encode('utf-8'), hashlib.sha256
        ).digest()).decode('ascii')
        self._set_request(body, signature)
        with patch.object(
                self.webapi.logging, 'info',
                side_effect=lambda *_args, **_kwargs: self.trace.append('log')) as info:
            result = self.webapi.callback('bot')
        self.assertEqual(['parse', 'log'], self.trace)
        self.bot.check_reload.assert_not_called()
        info.assert_called_once_with('Request body: %s', '{"events": []}')
        self.assertEqual({'message': 'OK'}, json.loads(result))
        self.interface.create_context_from_line_event.assert_called_once_with({'type': 'message'})


class LineWebhookHttpTest(unittest.TestCase):
    def setUp(self):
        interface_module = _load_module(
            '_line_http_interface', 'plugin/line/interface.py', _interface_replacements())
        self.interface = interface_module.LinePlugin_Interface('bot', {
            'line_access_token': 'artificial-access-token',
            'line_channel_secret': 'artificial-channel-secret',
        })
        self.bot = SimpleNamespace(
            get_interface=lambda _name: self.interface,
            check_reload=Mock(), handle_action=Mock(return_value='OK'))
        self.webapi = _load_module(
            '_line_http_webapi', 'plugin/line/webapi.py', {
                'main': _module('main', get_bot=lambda _name: self.bot),
                'auth': _module('auth'),
            })
        self.client = TestApp(self.webapi.app)

    def _post(self, body, *, signature=None, status=200):
        if signature is None:
            signature = base64.b64encode(hmac.new(
                self.interface.line_channel_secret.encode('utf-8'), body, hashlib.sha256,
            ).digest()).decode('ascii')
        return self.client.post(
            '/line/callback/bot', body, headers={'X-Line-Signature': signature},
            content_type='application/json', status=status, expect_errors=status >= 400)

    @staticmethod
    def _event(index):
        return {
            'type': 'message', 'webhookEventId': f'event-{index}',
            'source': {'type': 'user', 'userId': 'Utest'},
            'replyToken': f'artificial-reply-token-{index}',
            'message': {'type': 'text', 'text': f'入力{index}'},
        }

    def test_署名が不正なら不正UTF8でも401で本文を記録しない(self):
        with patch.object(self.webapi.logging, 'info') as info:
            self._post(b'\xff', signature='invalid', status=401)
        info.assert_not_called()
        self.bot.check_reload.assert_not_called()
        self.bot.handle_action.assert_not_called()

    def test_AWSのcontext不備でも署名不正を先に401で拒否する(self):
        with patch.dict(os.environ, {'AWS_LAMBDA_FUNCTION_NAME': 'artificial-api'}):
            self._post(b'\xff', signature='invalid', status=401)
        self.bot.check_reload.assert_not_called()
        self.bot.handle_action.assert_not_called()

    def test_署名済みの不正UTF8とJSONと構造は400で処理しない(self):
        for body in (b'\xff', b'{', b'[]', b'{"events":null}', b'{"events":[1]}'):
            with self.subTest(body=body), patch.object(self.webapi.logging, 'info') as info:
                self._post(body, status=400)
                info.assert_not_called()
        self.bot.check_reload.assert_not_called()
        self.bot.handle_action.assert_not_called()

    def test_未知sourceを挟んでも前後のeventを処理して200を返す(self):
        events = [self._event(1), {'type': 'delivery', 'webhookEventId': 'event-2'}, self._event(3)]
        body = json.dumps({'events': events}, ensure_ascii=False).encode('utf-8')
        with patch.object(self.webapi.logging, 'warning') as warning:
            result = self._post(body)
        self.assertEqual(200, result.status_int)
        self.assertEqual(['入力1', '入力3'], [
            call.args[0].action for call in self.bot.handle_action.call_args_list])
        warning.assert_called_once()

    def test_未知sourceやsourceなしは状態を作らず無視する(self):
        for source in ({'type': 'unknown'}, 'unknown', None):
            event = dict(self._event(1), source=source)
            with self.subTest(source=source), patch.object(self.webapi.logging, 'warning'):
                self._post(json.dumps({'events': [event]}).encode('utf-8'))
        self.bot.handle_action.assert_not_called()

    def test_既知sourceの必須ID欠落や型不正は500で伝える(self):
        for source in (
                {'type': 'user'}, {'type': 'user', 'userId': ''},
                {'type': 'group', 'groupId': None}, {'type': 'room', 'roomId': 1}):
            event = dict(self._event(1), source=source)
            with self.subTest(source=source):
                self._post(json.dumps({'events': [event]}).encode('utf-8'), status=500)
        self.bot.handle_action.assert_not_called()

    def test_eventの一般例外は既存どおり500にし以後を処理しない(self):
        malformed = self._event(2)
        del malformed['message']
        body = json.dumps({'events': [self._event(1), malformed, self._event(3)]}).encode('utf-8')
        self._post(body, status=500)
        self.assertEqual(1, self.bot.handle_action.call_count)

    def test_既存のNone失敗は200で次eventを処理する(self):
        self.bot.handle_action.side_effect = [None, 'OK', 'OK']
        body = json.dumps({'events': [self._event(index) for index in range(3)]}).encode('utf-8')
        self._post(body)
        self.assertEqual(3, self.bot.handle_action.call_count)

    def test_Rollback競合だけは当該eventを停止し後続を処理する(self):
        def handle(context):
            context.status = SimpleNamespace(scene='story/scene')
            if context.action == '入力1':
                raise RollbackConflictError('競合')
            return 'OK'
        self.bot.handle_action.side_effect = handle
        body = json.dumps({'events': [self._event(1), self._event(2)]}).encode('utf-8')
        with self.assertLogs(level='ERROR') as logs:
            self._post(body)
        self.assertEqual(2, self.bot.handle_action.call_count)
        self.assertIn('event_id=event-1', logs.output[0])
        self.assertIn('action=入力1 scene=story/scene', logs.output[0])
        self.assertIn('RollbackConflictError', logs.output[0])

    def test_保存結果不明も当該eventを停止し後続を処理する(self):
        def handle(context):
            context.status = SimpleNamespace(scene='story/scene')
            if context.action == '入力1':
                raise SaveResultUnknownError('保存を確認できません')
            return 'OK'
        self.bot.handle_action.side_effect = handle
        body = json.dumps({'events': [self._event(1), self._event(2)]}).encode('utf-8')
        with self.assertLogs(level='ERROR') as logs:
            self._post(body)
        self.assertEqual(2, self.bot.handle_action.call_count)
        self.assertIn('event_id=event-1', logs.output[0])
        self.assertIn('state recovery failed', logs.output[0])
        self.assertIn('SaveResultUnknownError', logs.output[0])
        self.assertNotIn('rollback failed', logs.output[0])

    def test_受信したAWSの期限を全eventで共有し期限なしも維持する(self):
        body = json.dumps({'events': [self._event(1), self._event(2)]}).encode('utf-8')
        for deadline in (None, 128):
            self.bot.handle_action.reset_mock()
            with self.subTest(deadline=deadline), patch.object(
                    self.webapi, 'get_lambda_deadline', return_value=deadline) as clock:
                self._post(body)
                clock.assert_called_once()
            self.assertEqual([deadline, deadline], [
                call.args[0].action_deadline for call in self.bot.handle_action.call_args_list])

    def test_認証済みログはtokenを隠し通常本文とUIDを残す(self):
        event = self._event(1)
        event.update(type='postback', postback={'data': '#選択@@ActionSecret'})
        body = json.dumps({'events': [event, self._event(2)]}, ensure_ascii=False).encode('utf-8')
        with self.assertLogs(level='INFO') as logs:
            self._post(body)
        logged = '\n'.join(logs.output)
        self.assertNotIn('artificial-reply-token', logged)
        self.assertNotIn('ActionSecret', logged)
        self.assertIn('#選択', logged)
        self.assertIn('Utest', logged)
        self.assertIn('入力2', logged)


class LineInterfaceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.interface_module = _load_module(
            '_line_contract_interface', 'plugin/line/interface.py', _interface_replacements())

    def setUp(self):
        self.interface = self.interface_module.LinePlugin_InterfaceFactory({
            'line_access_token': 'artificial-access-token',
            'line_channel_secret': 'artificial-channel-secret',
        }).create_interface('bot', {})
        self.interface.api = Mock()

    @staticmethod
    def _message_event(message):
        return {'type': 'message', 'replyToken': 'reply-token',
                'source': {'type': 'user', 'userId': 'U1'}, 'message': message}

    def _sending_context(self):
        self.interface.line_api_retry_count = 3
        self.interface.line_api_retry_sleep = 0.0
        return SimpleNamespace(
            event={'type': 'message', 'replyToken': 'reply-token'},
            source_id='U1', source_type='user',
            user='line:user,U1',
            status=SimpleNamespace(action_token='AAAAAAAA', scene='story/scene'),
        )

    def _send(self, side_effect):
        context = self._sending_context()
        self.interface.api.reply.side_effect = side_effect
        with patch.object(self.interface_module.time, 'sleep'):
            return self.interface.respond_reaction(
                context, [([None, 'こんにちは'], None)])

    @staticmethod
    def _api_error(status):
        return LineApiError(status, 'test')

    def test_LINEの5xxは同じ内容で送信側だけが再試行する(self):
        result = self._send([self._api_error(500), None])
        self.assertEqual('OK', result)
        self.assertEqual(2, self.interface.api.reply.call_count)
        self.assertEqual(self.interface.api.reply.call_args_list[0].args,
                         self.interface.api.reply.call_args_list[1].args)

    def test_LINEのpushの409だけを受付済みとして扱う(self):
        context = self._sending_context()
        context.event = None
        self.interface.api.push.side_effect = self._api_error(409)
        result = self.interface.respond_reaction(context, [([None, 'こんにちは'], None)])
        self.assertEqual('OK', result)
        self.assertEqual(1, self.interface.api.push.call_count)
        with self.assertRaises(LineApiError):
            self._send([self._api_error(409)])

    def test_LINEの400は再送せず即座に上へ返す(self):
        with self.assertRaises(LineApiError):
            self._send([self._api_error(400)])
        self.assertEqual(1, self.interface.api.reply.call_count)

    def test_LINEの429も既存どおり再試行する(self):
        self.assertEqual('OK', self._send([self._api_error(429), None]))
        self.assertEqual(2, self.interface.api.reply.call_count)

    def test_再試行を尽くした通信例外は元の型で上へ返す(self):
        with self.assertRaises(requests.ConnectionError):
            self._send(requests.ConnectionError('down'))
        self.assertEqual(3, self.interface.api.reply.call_count)

    def test_AWSの共有期限に残時間のtimeoutを収める(self):
        context = self._sending_context()
        context.action_deadline = 101
        with patch.object(self.interface_module.time, 'monotonic', return_value=100):
            self.interface.respond_reaction(context, [([None, '本文'], None)])
        self.assertEqual((0.5, 0.5), self.interface.api.reply.call_args.kwargs['timeout'])
        self.assertEqual(101, context.action_deadline)

    def test_期限切れは未送信で例外にしメニューも省略しない(self):
        context = self._sending_context()
        context.action_deadline = 100
        with patch.object(self.interface_module.time, 'monotonic', return_value=100):
            with self.assertRaises(TimeoutError):
                self.interface.respond_reaction(context, [([None, '本文'], None)])
            with self.assertRaises(TimeoutError):
                self.interface.link_rich_menu(context, 'richmenu-artificial')
        self.interface.api.reply.assert_not_called()
        self.interface.api.link_rich_menu.assert_not_called()

    def test_再試行のsleepが期限を越えるなら元のエラーで終了する(self):
        context = self._sending_context()
        context.action_deadline = 101
        self.interface.line_api_retry_sleep = 1
        failure = requests.ReadTimeout('lost')
        self.interface.api.reply.side_effect = failure
        with patch.object(self.interface_module.time, 'monotonic', return_value=100), \
                patch.object(self.interface_module.time, 'sleep') as sleep:
            with self.assertRaises(requests.ReadTimeout) as caught:
                self.interface.respond_reaction(context, [([None, '本文'], None)])
        self.assertIs(failure, caught.exception)
        self.interface.api.reply.assert_called_once()
        sleep.assert_not_called()

    def test_期限のない経路は既存の送信と外側の再試行設定を保つ(self):
        self.assertEqual(5, self.interface.line_api_retry_count)
        self.assertEqual(3, self.interface.get_retry_count())
        self.interface.api.reply.side_effect = requests.ReadTimeout('lost')
        context = self._sending_context()
        self.interface.line_api_retry_count = 5
        with patch.object(self.interface_module.time, 'sleep'):
            with self.assertRaises(requests.ReadTimeout):
                self.interface.respond_reaction(context, [([None, '本文'], None)])
        self.assertEqual(5, self.interface.api.reply.call_count)
        self.assertTrue(all('timeout' not in call.kwargs for call in self.interface.api.reply.call_args_list))

    def test_メニューのAPI失敗を握り潰さず同じ例外を返す(self):
        context = self._sending_context()
        failure = LineApiError(400, 'missing')
        self.interface.api.link_rich_menu.side_effect = failure
        with self.assertRaises(LineApiError) as caught:
            self.interface.link_rich_menu(context, 'richmenu-artificial')
        self.assertIs(failure, caught.exception)

    def test_通信例外でもLINEのエラーでもない例外は再試行しない(self):
        class Unexpected(Exception):
            pass

        with self.assertRaises(Unexpected):
            self._send(Unexpected('bug'))
        self.assertEqual(1, self.interface.api.reply.call_count)

    def test_6件以上のmessageは診断情報だけを一度記録して内部エラー1件に置き換える(self):
        context = self._sending_context()
        with self.assertLogs(level='WARNING') as captured:
            self.interface.respond_reaction(
                context, [([None, f'本文{index}'], None) for index in range(6)])
        self.assertEqual([record.getMessage() for record in captured.records], [
            '[LINE] message limit exceeded: bot=bot user=line:user,U1 scene=story/scene count=6',
        ])
        sent = self.interface.api.reply.call_args.args[1]
        self.assertEqual([{'type': 'text', 'text': '内部エラー: 送信するメッセージが多すぎます'}], sent)
        self.interface.api.reply.assert_called_once()
        self.interface.api.push.assert_not_called()

    def test_5件以内のmessageは警告せずそのまま送る(self):
        context = self._sending_context()
        with self.assertNoLogs(level='WARNING'):
            self.interface.respond_reaction(
                context, [([None, f'本文{index}'], None) for index in range(5)])
        self.interface.api.reply.assert_called_once_with('reply-token', [
            {'type': 'text', 'text': f'本文{index}'} for index in range(5)
        ])

    def test_eventが無ければpushしretry_keyを渡す(self):
        context = self._sending_context()
        context.event = None
        self.interface.respond_reaction(context, [([None, 'こんにちは'], None)])
        self.interface.api.push.assert_called_once()
        self.assertEqual('U1', self.interface.api.push.call_args.args[0])
        self.assertTrue(self.interface.api.push.call_args.kwargs['retry_key'])
        self.interface.api.reply.assert_not_called()

    def test_audio_reactionをLINE送信messageへ変換する(self):
        context = SimpleNamespace(response=None)
        messages = self.interface._construct_responses(
            context,
            [([None, '@audio', 'https://media.example/audio.mp3',
               1234, 'audio/mpeg'], None)],
        )
        self.assertEqual([{
            'type': 'audio',
            'originalContentUrl': 'https://media.example/audio.mp3',
            'duration': 1234,
        }], messages)

    def test_video完了actionを世代付きtracking_IDへ変換する(self):
        context = SimpleNamespace(
            response=None,
            source_type='user',
            status=SimpleNamespace(action_token='Generation'),
        )
        messages = self.interface._construct_responses(
            context,
            [([None, '@video', 'https://media.example/poster.png',
               'https://media.example/video.mp4', '*完了'], None)],
        )
        expected = utility_module.encode_line_video_tracking_id(
            '*完了', 'Generation')
        self.assertLessEqual(len(expected), 100)
        self.assertRegex(
            expected, r'^[a-zA-Z0-9\-.=,+*()%$&;:@{}!?<>\[\]]+$')
        self.assertEqual(expected, messages[0]['trackingId'])

        event = {'type': 'videoPlayComplete', 'videoPlayComplete': {'trackingId': expected}}
        action, attrs = self.interface._construct_action(event)
        self.assertEqual('*完了', action)
        self.assertEqual('Generation', attrs['action_token'])

    def test_group動画では完了actionを付けない(self):
        context = SimpleNamespace(
            response=None,
            source_type='group',
            status=SimpleNamespace(action_token='Generation'),
        )
        with patch.object(self.interface_module.logging, 'warning') as warning:
            messages = self.interface._construct_responses(
                context,
                [([None, '@video', 'https://media.example/poster.png',
                   'https://media.example/video.mp4', '*完了'], None)],
            )
        self.assertNotIn('trackingId', messages[0])
        warning.assert_called_once()

    def test_不正な旧video_tracking_IDは本文を出さず無視する(self):
        event = {'type': 'videoPlayComplete', 'videoPlayComplete': {'trackingId': '旧形式'}}
        with patch.object(self.interface_module.logging, 'warning') as warning:
            action, _attrs = self.interface._construct_action(event)
        self.assertIsNone(action)
        self.assertNotIn('旧形式', str(warning.call_args_list))

    def test_all_scenario_versions_use_same_latest_internal_action_mapping(self):
        provider = {'type': 'line'}
        cases = (
            (self._message_event({'type': 'location', 'title': '題', 'latitude': 35.0,
                                  'longitude': 139.0, 'address': '住所'}),
             ':LINE_LOCATION:題,35.0,139.0,住所'),
            (self._message_event({'type': 'sticker', 'packageId': '1', 'stickerId': '2'}),
             ':LINE_STICKER:1,2'),
            (self._message_event({'type': 'image', 'id': 'image-id', 'contentProvider': provider}),
             ':LINE_IMAGE:image-id'),
            (self._message_event({'type': 'video', 'id': 'video-id', 'duration': 1200,
                                  'contentProvider': provider}),
             ':LINE_VIDEO:video-id,1200'),
            (self._message_event({'type': 'audio', 'id': 'audio-id', 'duration': 800,
                                  'contentProvider': provider}),
             ':LINE_AUDIO:audio-id,800'),
            (self._message_event({'type': 'file', 'id': 'file-id', 'fileName': 'name.txt',
                                  'fileSize': 42}),
             ':LINE_FILE:file-id,name.txt,42'),
            (self._message_event({'type': 'image', 'id': 'external-id',
                                  'contentProvider': {'type': 'external',
                                                      'originalContentUrl': 'https://x/'}}),
             ':LINE_ETC:image'),
            # 旧SDKが知らない message type も 500 にせず action として渡す
            (self._message_event({'type': 'newthing', 'id': 'x'}), ':LINE_ETC:newthing'),
            ({'type': 'beacon', 'beacon': {'type': 'enter', 'hwid': 'beacon-id'}},
             ':LINE_BEACON:enter,beacon-id'),
            ({'type': 'postback', 'postback': {'data': '#next'}}, '#next'),
            ({'type': 'follow', 'replyToken': 'r'}, '##line.follow'),
            ({'type': 'unfollow'}, '##line.unfollow'),
            ({'type': 'join', 'replyToken': 'r'}, '##line.join'),
            ({'type': 'leave'}, '##line.leave'),
        )
        for version in (1, 2, 3):
            self.interface.params = {'scenario_version': version}
            for event, expected in cases:
                with self.subTest(version=version, expected=expected):
                    action, attrs = self.interface._construct_action(event)
                    self.assertEqual(expected, action)
                    self.assertEqual(event['type'], attrs['line.event.type'])

    def test_活用しないeventはactionにしない(self):
        for event in ({'type': 'memberJoined'}, {'type': 'memberLeft'},
                      {'type': 'membership', 'membership': {'type': 'joined'}},
                      {'type': 'unsend'}):
            with self.subTest(event=event['type']):
                self.assertIsNone(self.interface._construct_action(event)[0])

    def test_text_that_looks_like_internal_action_is_sanitized(self):
        event = self._message_event({'type': 'text', 'text': ':LINE_IMAGE:spoof'})
        self.assertEqual(' :LINE_IMAGE:spoof', self.interface._construct_action(event)[0])
        ordinary = self._message_event({'type': 'text', 'text': 'こんにちは'})
        self.assertEqual('こんにちは', self.interface._construct_action(ordinary)[0])

    def test_sourceの種別ごとにuser_idを組む(self):
        cases = (
            ({'type': 'user', 'userId': 'U1'}, 'user,U1'),
            ({'type': 'group', 'groupId': 'G1', 'userId': 'U1'}, 'group,G1'),
            ({'type': 'room', 'roomId': 'R1', 'userId': 'U1'}, 'room,R1'),
        )
        for source, expected in cases:
            with self.subTest(source=source['type']):
                event = {'type': 'message', 'replyToken': 'r', 'source': source,
                         'message': {'type': 'text', 'text': 'やあ'}}
                context = self.interface.create_context_from_line_event(event)
                self.assertEqual(('line', expected), (context.user.service, context.user.user_id))
                self.assertEqual(source['type'], context.source_type)
                self.assertIs(event, context.event)
        with patch.object(self.interface_module.logging, 'warning'):
            self.assertIsNone(self.interface.create_context_from_line_event(
                {'type': 'message', 'source': {'type': 'unknown'},
                 'message': {'type': 'text', 'text': 'やあ'}}))

    def test_actionにならないeventはcontextを作らない(self):
        event = {'type': 'memberJoined', 'source': {'type': 'group', 'groupId': 'G1'}}
        self.assertIsNone(self.interface.create_context_from_line_event(event))

    def test_generated_message_is_logged_when_event_has_no_reply_token(self):
        context = SimpleNamespace(event={'type': 'unfollow'})
        messages = ['生成本文']
        with patch.object(self.interface_module.logging, 'info') as info:
            self.interface._reply_message(context, messages)
        info.assert_called_once_with(
            "event %s doesnt have reply_token: %s", 'unfollow', ['生成本文'])

    def test_未送信の生成messageログは本文を残し選択肢tokenを隠す(self):
        context = SimpleNamespace(event={'type': 'unfollow'})
        messages = [{
            'type': 'text', 'text': '表示する本文',
            'quickReply': {'items': [{'type': 'action', 'action': {
                'type': 'postback', 'label': '選択', 'data': '#next@@ActionSecret',
            }}]},
        }]
        with self.assertLogs(level='INFO') as logs:
            self.interface._reply_message(context, messages)
        self.assertNotIn('ActionSecret', logs.output[0])
        self.assertIn('表示する本文', logs.output[0])
        self.assertIn('#next', logs.output[0])
        self.assertEqual('#next@@ActionSecret', messages[0]['quickReply']['items'][0]['action']['data'])
        self.interface.api.reply.assert_not_called()
        self.interface.api.push.assert_not_called()


class LinePushRetryKeyTest(unittest.TestCase):
    """push の retry key はその呼出しだけに付き、reply には付かない。"""

    @classmethod
    def setUpClass(cls):
        cls.interface_module = _load_module(
            '_line_push_key_interface', 'plugin/line/interface.py', _interface_replacements())

    def setUp(self):
        self.interface = self.interface_module.LinePlugin_Interface('bot', {
            'line_access_token': 'token', 'line_channel_secret': 'secret'})
        self.interface.line_api_retry_count = 1
        self.interface.line_api_retry_sleep = 0.0
        self.posts = []

        def fake_post(url, headers=None, data=None, timeout=None):
            self.posts.append((url, dict(headers or {})))
            return SimpleNamespace(status_code=200, headers={}, json=dict, text='')

        patcher = patch.object(line_api.requests, 'post', fake_post)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _context(source_id, event=None):
        return SimpleNamespace(
            event=event, source_id=source_id, source_type='user',
            status=SimpleNamespace(action_token='AAAAAAAA'))

    def test_pushごとに別のkeyが付きreplyには付かない(self):
        with patch.object(self.interface_module.time, 'sleep'):
            self.interface.respond_reaction(self._context('U1'), [([None, 'a'], None)])
            self.interface.respond_reaction(self._context('U2'), [([None, 'b'], None)])
            self.interface.respond_reaction(
                self._context('U1', {'type': 'message', 'replyToken': 'reply-token'}),
                [([None, 'c'], None)])

        self.assertTrue(all(url.endswith('/v2/bot/message/push') for url, _ in self.posts[:2]))
        keys = [headers.get('X-Line-Retry-Key') for _url, headers in self.posts[:2]]
        self.assertTrue(all(keys))
        self.assertNotEqual(keys[0], keys[1])
        self.assertTrue(self.posts[2][0].endswith('/v2/bot/message/reply'))
        self.assertNotIn('X-Line-Retry-Key', self.posts[2][1])


class LineDeliveryRuntimeTest(unittest.TestCase):
    """実RuntimeとLINE送信を通し、既存再試行とAWSの期限を確認する。"""

    def setUp(self):
        settings = _module('settings', CONSTANTS={})
        self.runtime = _load_module('_line_delivery_runtime', 'runtime.py', {'settings': settings})
        interface_module = _load_module('_line_delivery_interface', 'plugin/line/interface.py', {})
        self.interface = interface_module.LinePlugin_Interface('bot', {
            'line_access_token': 'artificial', 'line_channel_secret': 'artificial',
            'line_api_retry_sleep': 0})
        self.interface.api = Mock()
        self._original_create_context = self.interface.create_context
        self.interface.create_context = self._context
        self.bot = self.runtime.BotRuntime('bot', {'line': self.interface}, None)
        self.bot.scenario = SimpleNamespace(version=3, constants={})
        self.selection = ScenarioSelection(self.bot.scenario, 'fixture-uri', None)
        self.persisted = {'count': 0, 'action_token': 'old'}
        self.generations = {}
        self.trace = []
        self.register_child = Mock()
        self.use_menu = False
        test = self

        class Director:
            def __init__(self, scenario, context):
                self.context = context

            def plan_reactions(self):
                context = self.context
                test.trace.append('plan')
                context.status.count += 1
                context.status.action_token = f'token-{context.status.count}'
                context.reactions = [([None, f'本文{context.status.count}'], None)]
                if test.use_menu:
                    context.reactions.insert(0, ([None, '@richmenu', 'richmenu-artificial'], None))
                context.pending_tasks.append((test.register_child, {
                    'bot': 'child', 'action': '#next', 'user': 'line:user,U1'}))

        self.runtime._director_class = Director

    def _context(self, user, action, attrs):
        context = self._original_create_context(user, action, attrs)

        def load():
            self.trace.append('load')
            context.old = copy.deepcopy(self.persisted)
            context.old_generations = copy.deepcopy(self.generations)
            context.status = SimpleNamespace(
                **self.persisted, scene='scene', scenario_generations=copy.deepcopy(self.generations))

        def save():
            self.trace.append('save')
            self.persisted.update(count=context.status.count, action_token=context.status.action_token)
            self.generations = copy.deepcopy(context.status.scenario_generations)

        def rollback():
            self.trace.append('rollback')
            self.persisted = context.old
            self.generations = context.old_generations

        context.load_status, context.save_status, context.rollback_status = load, save, rollback
        return context

    def _runtime_scope(self):
        from plugin.line.default_commands import LineDefaultCommandsPlugin_Runtime
        menu_runtime = LineDefaultCommandsPlugin_Runtime({'alt_text': 'artificial'})

        def construct(context, sender, message, options, children):
            if message == '@richmenu':
                return menu_runtime.construct_response(context, sender, message, options, children)
            return False

        scope = ExitStack()
        for patcher in (
                patch.object(self.runtime.commands, 'get_runtime_object_dictionary', return_value={}),
                patch.object(self.runtime.commands, 'invoke_runtime_construct_response', side_effect=construct),
                patch.object(self.runtime.task_client, 'defer_until_response', return_value=True),
                patch('plugin.line.interface.time.sleep')):
            scope.enter_context(patcher)
        return scope

    def run_action(self, side_effect, *, push=False, deadline=None):
        from users import User
        context = self.interface.create_context(User('line', 'user,U1'), '入力', {})
        if not push:
            context.event = {'type': 'message', 'replyToken': 'artificial'}
        if deadline is not None:
            context.action_deadline = deadline
        api = self.interface.api.push if push else self.interface.api.reply
        api.side_effect = side_effect
        with self._runtime_scope(), patch.object(self.runtime.BotRuntime, '_log_failure') as failure:
            result = self.bot.handle_action(context, selection=self.selection)
        return result, failure

    def test_期限のない返信とpushは既存どおり内側5回と外側4回を試す(self):
        for push in (False, True):
            with self.subTest(push=push):
                self.trace.clear()
                self.interface.api.reset_mock()
                result, _failure = self.run_action(requests.ReadTimeout('lost'), push=push)
                self.assertIsNone(result)
                api = self.interface.api.push if push else self.interface.api.reply
                self.assertEqual(20, api.call_count)
                self.assertEqual(['load', 'plan', 'save', 'rollback'] * 4, self.trace)
                self.assertEqual({'count': 0, 'action_token': 'old'}, self.persisted)
                self.register_child.assert_not_called()

    def test_pushの送信内再試行は宛先内容keyを固定する(self):
        result, failure = self.run_action([requests.ReadTimeout('lost'), None], push=True)
        self.assertEqual('OK', result)
        calls = self.interface.api.push.call_args_list
        self.assertEqual(2, len(calls))
        self.assertEqual(calls[0].args, calls[1].args)
        self.assertEqual(calls[0].kwargs['retry_key'], calls[1].kwargs['retry_key'])
        self.assertEqual(['load', 'plan', 'save'], self.trace)
        self.register_child.assert_called_once()
        failure.assert_not_called()

    def test_メニュー通信失敗は本文を送らず巻き戻す(self):
        self.use_menu = True
        self.interface.api.link_rich_menu.side_effect = LineApiError(400, 'missing')
        with self.assertLogs(level='ERROR'):
            result, _failure = self.run_action(None)
        self.assertIsNone(result)
        self.assertEqual(4, self.interface.api.link_rich_menu.call_count)
        self.interface.api.reply.assert_not_called()
        self.assertEqual({'count': 0, 'action_token': 'old'}, self.persisted)
        self.register_child.assert_not_called()

    def test_AWSで失敗中に期限切れなら次の計画と送信を始めない(self):
        clock = [90]
        def timeout(*args, **kwargs):
            clock[0] = 101
            raise requests.ReadTimeout('lost')
        with patch.object(self.runtime.time, 'monotonic', side_effect=lambda: clock[0]):
            result, _failure = self.run_action(timeout, deadline=100)
        self.assertIsNone(result)
        self.assertEqual(['load', 'plan', 'save', 'rollback'], self.trace)
        self.interface.api.reply.assert_called_once()
        self.assertEqual({'count': 0, 'action_token': 'old'}, self.persisted)

    def test_返信成功後の期限切れだけでは状態を戻さず子を登録する(self):
        clock = [90]
        def accepted(*args, **kwargs):
            clock[0] = 101
        with patch.object(self.runtime.time, 'monotonic', side_effect=lambda: clock[0]):
            result, failure = self.run_action(accepted, deadline=100)
        self.assertEqual('OK', result)
        self.assertEqual(['load', 'plan', 'save'], self.trace)
        self.assertEqual({'count': 1, 'action_token': 'token-1'}, self.persisted)
        self.register_child.assert_called_once()
        failure.assert_not_called()


class LineDefaultCommandsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module(
            '_line_contract_default_commands', 'plugin/line/default_commands.py', _base_stubs())

    def test_reply_without_preceding_message_uses_configured_fallback(self):
        runtime = self.module.LineDefaultCommandsPlugin_Runtime({
            'alt_text': 'alt', 'reply_fallback_message': '代替メッセージ'})
        context = SimpleNamespace(
            response=[], status=SimpleNamespace(action_token='token'))
        runtime.construct_response(context, None, '@reply', [], [['選択肢', '返答']])
        self.assertEqual(1, len(context.response))
        self.assertEqual('代替メッセージ', context.response[0]['text'])
        self.assertEqual(1, len(context.response[0]['quickReply']['items']))

        # 連続した @reply ではフォールバック本文を重複生成しない。
        first_message = context.response[0]
        runtime.construct_response(context, None, '@reply', [], [['別の選択肢', '別の返答']])
        self.assertEqual([first_message], context.response)

    def test_reply_attaches_to_existing_message(self):
        runtime = self.module.LineDefaultCommandsPlugin_Runtime({'alt_text': 'alt'})
        message = {'type': 'text', 'text': '本文'}
        context = SimpleNamespace(
            response=[message], status=SimpleNamespace(action_token='token'))
        runtime.construct_response(context, None, '@reply', [], [['選択肢', '返答']])
        self.assertEqual([message], context.response)
        self.assertEqual('本文', message['text'])
        self.assertEqual(
            [{'type': 'action', 'action': {'type': 'message', 'label': '選択肢', 'text': '返答'}}],
            message['quickReply']['items'])

    def test_richmenuはinterfaceの紐付け処理へ委譲する(self):
        runtime = self.module.LineDefaultCommandsPlugin_Runtime({'alt_text': 'alt'})
        interface = SimpleNamespace(link_rich_menu=Mock())
        context = SimpleNamespace(
            response=[], source_id='U1', get_interface=lambda name: interface)
        runtime.construct_response(context, None, '@richmenu', ['richmenu-1'])
        interface.link_rich_menu.assert_called_once_with(context, 'richmenu-1')
        self.assertEqual([], context.response)


def _plugin_packages(default_commands=None, more=None, quick_reply=None):
    plugin = _module('plugin')
    plugin.__path__ = []
    line = _module('plugin.line')
    line.__path__ = []
    plugin.line = line
    replacements = {'plugin': plugin, 'plugin.line': line}
    if default_commands is not None:
        command_names = _module(
            'plugin.line.command_names',
            IMAGEMAP_CMDS=getattr(
                default_commands, 'IMAGEMAP_CMDS', ('@imagemap',)),
            REPLY_CMDS=getattr(
                default_commands, 'REPLY_CMDS', ('@reply',)),
            SET_NEXT_LABEL_CMD='@@set_next_label',
        )
        line.command_names = command_names
        replacements['plugin.line.command_names'] = command_names
    for name, value in (
            ('default_commands', default_commands), ('more', more),
            ('quick_reply', quick_reply)):
        if value is not None:
            setattr(line, name, value)
            replacements['plugin.line.' + name] = value
    return replacements


class FirestoreAndImageTextContractTest(unittest.TestCase):
    def _load_modules(self):
        self.models_db = Mock(name='models_db')
        self.more_db = Mock(name='more_db')
        self.image_db = Mock(name='image_db')
        self.client = Mock(side_effect=[
            self.models_db, self.more_db, self.image_db])
        firestore = _module(
            'google.cloud.firestore', Client=self.client,
            transactional=lambda function: function)
        google = _module('google')
        google.__path__ = []
        cloud = _module('google.cloud', firestore=firestore)
        cloud.__path__ = []
        google.cloud = cloud
        base = _base_stubs()
        default_commands = _module(
            'plugin.line.default_commands', IMAGEMAP_CMDS=('@imagemap',),
            REPLY_CMDS=('@reply',))
        packages = _plugin_packages(default_commands=default_commands)
        common = {
            **base, **packages,
            'google': google, 'google.cloud': cloud,
            'google.cloud.firestore': firestore,
        }
        with patch.object(backend_factory, '_provider', 'gcp'):
            models_module = _load_module(
                '_line_contract_models', 'models.py', common)
            more_module = _load_module(
                '_line_contract_more', 'plugin/line/more.py', common)

            renderer = _module(
                'plugin.render_text.renderer', render_text_to_png=Mock())
            render_text = _module('plugin.render_text', renderer=renderer)
            packages = _plugin_packages(
                default_commands=default_commands, more=more_module,
                quick_reply=_module(
                    'plugin.line.quick_reply', append_quick_reply=Mock()))
            packages['plugin'].render_text = render_text
            image_replacements = {
                **base, **packages,
                'plugin.render_text': render_text,
                'plugin.render_text.renderer': renderer,
                'google': google, 'google.cloud': cloud,
                'google.cloud.firestore': firestore,
            }
            image_module = _load_module(
                '_line_contract_image_text',
                'plugin/line/image_text.py', image_replacements)
        return models_module, more_module, image_module, renderer

    def test_each_module_owns_an_import_time_firestore_client(self):
        models_module, more_module, image_module, _renderer = self._load_modules()
        self.assertEqual(3, self.client.call_count)
        self.assertIs(
            self.models_db, models_module.get_state_store().client)
        self.assertIs(self.more_db, more_module._state_store.client)
        self.assertIs(self.image_db, image_module._state_store.client)

    def test_player_next_label_uses_full_status_id_as_document_id(self):
        _models_module, more_module, _image_module, _renderer = self._load_modules()
        collection = Mock()
        document = Mock()
        document.get.return_value = SimpleNamespace(exists=False)
        collection.document.return_value = document
        self.more_db.collection.return_value = collection
        self.more_db.transaction.return_value = Mock()
        status = SimpleNamespace(id='shared:line:user,U1')

        more_module.PlayerNextLabelDB.set_next_label('##NEXT', '続きを読む', status)
        self.assertEqual((None, None), more_module.PlayerNextLabelDB.get_next_label(status))
        self.assertEqual(
            (None, None),
            more_module.PlayerNextLabelDB.compare_and_clear_next_label(status, '##NEXT'))
        more_module.PlayerNextLabelDB.clear_next_label(status)

        self.assertEqual([call(status.id)] * 4, collection.document.call_args_list)
        self.assertEqual(
            [call('player_next_labels')] * 4,
            self.more_db.collection.call_args_list)

    def test_image_text_cache_and_build_use_mocked_storage_and_renderer(self):
        _models_module, _more_module, image_module, renderer = self._load_modules()
        collection = Mock()
        document = Mock()
        collection.document.return_value = document
        self.image_db.collection.return_value = collection
        frame_opt = '{"frame":"default"}'
        document.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {
                'text': '本文', 'frame_opt': frame_opt, 'url': 'https://cached',
                'width': 100, 'height': 80, 'rest': None,
            })
        self.assertEqual(
            ('https://cached', (100, 80), None),
            image_module.ImageTextStatDB.get_cached_image_text_stat('本文', frame_opt))

        renderer.render_text_to_png.return_value = (b'png-data', None)
        builder = Mock()
        builder.option_force = True
        builder.scene.get_relative_position_desc.return_value = 'scene:1'
        builder.build_image_for_imagemap_command_with_rawdata.return_value = (
            'https://built', (100, 100))
        plugin = image_module.LineImageTextPlugin_Builder({
            'more_message': '続きを読む',
            'more_image_url': 'https://more',
            'frames': {'default': {
                'size_x': 100, 'size_y': 100, 'more_mode': 'inner'}},
            'default_frame': 'default',
        })
        self.assertTrue(plugin.build_from_command(
            builder, None, '@imagetext', ['画像本文']))
        renderer.render_text_to_png.assert_called_once()
        builder.build_image_for_imagemap_command_with_rawdata.assert_called_once()
        document.set.assert_called_once()
        self.assertIn(
            call(None, '@@set_next_label', ['##IMGTEXT__scene:1__0', '続きを読む'], None),
            builder.add_command.call_args_list)


class QuickReplyContractTest(unittest.TestCase):
    def _load(self, filename):
        default_commands = _load_module(
            '_line_contract_quick_reply_lint', 'plugin/line/default_commands.py', _base_stubs())
        replacements = {
            **_base_stubs(),
            **_plugin_packages(default_commands=default_commands),
        }
        replacements['utility'].parse_sender = lambda message: (None, message)
        return _load_module(
            '_line_contract_' + filename, 'plugin/line/' + filename + '.py', replacements)

    def test_v3_builder_and_runtime_keep_guard_and_choice_mapping(self):
        module = self._load('quick_reply')
        builder = Mock(assert_strlen=Mock())
        builder.make_control_flow_refernce_label.side_effect = ['##CF0', '##CF1']
        module.append_quick_reply(
            builder, '##Q_', ['選択1', '選択2=>表示2'], '話者', '##PLEASE')
        self.assertIn(
            call('話者', '@reply', [], [
                ['選択1', '##Q_1'], ['選択2', '表示2', '##Q_2']]),
            builder.add_command.call_args_list)
        self.assertIn(
            call('話者', '@@set_quick_reply_guard', [
                '##Q_', '##PLEASE',
                json.dumps([['選択1', '##Q_1'], ['選択2', '表示2', '##Q_2']]),
                'True'], None),
            builder.add_command.call_args_list)
        builder.start_control_flow.assert_called_once_with('quick_reply')
        builder.add_new_control_flow_block.assert_called_once_with()

        runtime = module.LineQuickReplyPlugin_Runtime({
            'default_reply': '既定', 'please_select_quick_reply_label': '##PLEASE'})
        status = {}
        context = SimpleNamespace(status=status)
        runtime.run_command(context, None, '@@set_quick_reply_guard', [
            '##Q_', '##PLEASE',
            json.dumps([['選択1', '##Q_1'], ['選択2', '表示2', '##Q_2']]),
            'True'])
        self.assertEqual('##Q_2', runtime.modify_incoming_action(context, '選択2'))
        self.assertEqual('##PLEASE', runtime.modify_incoming_action(context, '想定外'))
        self.assertEqual(1, status[module.QUICK_REPLY_GUARD_VARIABLE]['retry_count'])

    def test_v2_builder_and_runtime_keep_retry_branch(self):
        module = self._load('quick_reply_v2')
        builder = Mock(assert_strlen=Mock())
        module.append_quick_reply(
            builder, '##Q_', ['選択1', '選択2=>表示2'], '話者', '再選択')
        self.assertIn(
            call('話者', '@reply', [], [
                ['選択1', '##Q_1'], ['選択2', '表示2', '##Q_2']]),
            builder.add_command.call_args_list)
        self.assertIn(call('話者', '@@set_quick_reply_guard', ['##Q_'], None),
                      builder.add_command.call_args_list)
        self.assertIn(call('話者', '再選択', [], None), builder.add_command.call_args_list)
        self.assertIn(call('##Q_R'), builder.add_new_string_block.call_args_list)
