# coding: utf-8
"""LINE プラグインの公開版で維持する局所的な契約を確認する。

送信 JSON の同一性は tests/plugin/test_line_wire.py（golden）で、API 呼出しは
test_line_api.py で、署名検証は test_line_webhook.py で確認する。ここでは event から
action への変換、送信失敗の分類と再試行、Webhook callback の HTTP 契約を扱う。
"""

import base64
import hashlib
import hmac
import importlib.util
import io
import json
import re
import subprocess
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import requests

from cloud_backend import factory as backend_factory
from plugin.line import api as line_api
from plugin.line.api import LineApiError
from plugin.line.webhook import InvalidSignatureError
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


class LineWebhookContractTest(unittest.TestCase):
    def setUp(self):
        self.secret = 'channel-secret'
        self.trace = []
        self.request = SimpleNamespace(headers={}, body=io.BytesIO())
        self.response = SimpleNamespace(content_type=None)

        def parse_webhook(body, signature):
            self.trace.append('parse')
            expected = base64.b64encode(hmac.new(
                self.secret.encode('utf-8'), body.encode('utf-8'), hashlib.sha256
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
        )
        replacements = {
            'bottle': bottle,
            'auth': _module('auth'),
            'utility': utility,
            'main': _module('main', get_bot=Mock(return_value=self.bot)),
            'users': _module('users'),
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

    def test_valid_signature_logs_body_only_after_parse_then_reloads(self):
        body = '{"events":[]}'
        signature = base64.b64encode(hmac.new(
            self.secret.encode('utf-8'), body.encode('utf-8'), hashlib.sha256
        ).digest()).decode('ascii')
        self._set_request(body, signature)
        with patch.object(
                self.webapi.logging, 'info',
                side_effect=lambda *_args, **_kwargs: self.trace.append('log')) as info:
            result = self.webapi.callback('bot')
        self.assertEqual(['parse', 'log', 'reload'], self.trace)
        info.assert_called_once_with('Request body: {}'.format(body))
        self.assertEqual({'message': 'OK'}, json.loads(result))
        self.interface.create_context_from_line_event.assert_called_once_with({'type': 'message'})


class LineInterfaceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.interface_module = _load_module(
            '_line_contract_interface', 'plugin/line/interface.py', _interface_replacements())

    def setUp(self):
        self.interface = object.__new__(self.interface_module.LinePlugin_Interface)
        self.interface.bot_name = 'bot'
        self.interface.allow_special_action_text_for_debug = False
        self.interface.api = Mock()
        self.interface.sender_icon_urls = {}

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
            status=SimpleNamespace(action_token='AAAAAAAA'),
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

    def test_LINEの5xxと429は送信側で短く再試行する(self):
        result = self._send([self._api_error(500), self._api_error(429), None])
        self.assertEqual('OK', result)
        self.assertEqual(3, self.interface.api.reply.call_count)

    def test_LINEの409は送信済みとして扱う(self):
        result = self._send([self._api_error(409)])
        self.assertEqual('OK', result)
        self.assertEqual(1, self.interface.api.reply.call_count)

    def test_LINEの400は再送せず即座に上へ返す(self):
        with self.assertRaises(LineApiError):
            self._send([self._api_error(400)])
        self.assertEqual(1, self.interface.api.reply.call_count)

    def test_再試行を尽くした通信例外は上へ返す(self):
        with self.assertRaises(requests.ConnectionError):
            self._send(requests.ConnectionError('down'))
        self.assertEqual(3, self.interface.api.reply.call_count)

    def test_通信例外でもLINEのエラーでもない例外は再試行しない(self):
        class Unexpected(Exception):
            pass

        with self.assertRaises(Unexpected):
            self._send(Unexpected('bug'))
        self.assertEqual(1, self.interface.api.reply.call_count)

    def test_6件以上のmessageは内部エラー1件に置き換える(self):
        context = self._sending_context()
        self.interface.respond_reaction(
            context, [([None, f'本文{index}'], None) for index in range(6)])
        sent = self.interface.api.reply.call_args.args[1]
        self.assertEqual([{'type': 'text', 'text': '内部エラー: 送信するメッセージが多すぎます'}], sent)

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
        with self.assertRaises(NotImplementedError):
            self.interface.create_context_from_line_event(
                {'type': 'message', 'source': {'type': 'unknown'},
                 'message': {'type': 'text', 'text': 'やあ'}})

    def test_actionにならないeventはcontextを作らない(self):
        event = {'type': 'memberJoined', 'source': {'type': 'group', 'groupId': 'G1'}}
        self.assertIsNone(self.interface.create_context_from_line_event(event))

    def test_generated_message_is_logged_when_event_has_no_reply_token(self):
        context = SimpleNamespace(event={'type': 'unfollow'})
        messages = ['生成本文']
        with patch.object(self.interface_module.logging, 'info') as info:
            self.interface._reply_message(context, messages)
        info.assert_called_once_with(
            "event unfollow doesnt have reply_token: ['生成本文']")
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

    def test_richmenuはinterfaceのapiで紐付ける(self):
        runtime = self.module.LineDefaultCommandsPlugin_Runtime({'alt_text': 'alt'})
        interface = SimpleNamespace(api=Mock())
        context = SimpleNamespace(
            response=[], source_id='U1', get_interface=lambda name: interface)
        runtime.construct_response(context, None, '@richmenu', ['richmenu-1'])
        interface.api.link_rich_menu.assert_called_once_with('U1', 'richmenu-1')
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
        default_commands = _module(
            'plugin.line.default_commands', REPLY_CMDS=('@reply',))
        replacements = {
            **_base_stubs(),
            **_plugin_packages(default_commands=default_commands),
        }
        replacements['utility'].parse_sender = lambda message: (None, message)
        return _load_module(
            '_line_contract_' + filename, 'plugin/line/' + filename + '.py', replacements)

    def test_v3_builder_and_runtime_keep_guard_and_choice_mapping(self):
        module = self._load('quick_reply')
        builder = Mock()
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
        builder = Mock()
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
