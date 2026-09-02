import base64
import json
import sys
import time
import types
import unittest
from unittest.mock import Mock, patch

from plugin.webchat.errors import (
    ActionNotActive,
    IncompatibleState,
    InvalidStateToken,
    InvalidWebchatConfiguration,
    BotNotWebCompatible,
)
from plugin.webchat.state import TokenNextLabelStore, TokenPlayerStatus
from plugin.webchat.token import (
    POSTBACK_TYPE,
    STATE_TYPE,
    TOKEN_VERSION,
    WebchatTokenCodec,
)
from tests.test_api_endpoints import TestApp


SIGNING_KEY = base64.urlsafe_b64encode(b'k' * 32).decode('ascii').rstrip('=')


class WebchatTokenCodecTest(unittest.TestCase):
    def setUp(self):
        self.codec = WebchatTokenCodec(SIGNING_KEY)
        self.state = {
            'v': TOKEN_VERSION,
            'typ': STATE_TYPE,
            'deployment': 'test',
            'bot': 'bot',
            'scenario_compatibility_epoch': 'epoch-1',
            'scenario_revision': 'revision-a',
            'conversation_id': 'conversation',
            'revision': 3,
            'player': {
                'scene': '*scene',
                'scene_history': [],
                'action_generation': 'generation',
                'flags': {'日本語': ['値']},
            },
        }

    def test_stateを無期限でround_tripする(self):
        token = self.codec.dump_state(self.state)
        loaded = self.codec.load_state(
            token, 'test', 'bot', 'epoch-1')
        self.assertEqual(self.state, loaded)
        self.assertEqual(64, len(self.codec.state_id(token)))

    def test_state改ざんを拒否する(self):
        token = self.codec.dump_state(self.state)
        replacement = 'A' if token[-1] != 'A' else 'B'
        with self.assertRaises(InvalidStateToken):
            self.codec.load_state(
                token[:-1] + replacement, 'test', 'bot', 'epoch-1')

    def test_別用途saltのtokenを拒否する(self):
        postback = dict(self.state)
        postback.update({
            'typ': POSTBACK_TYPE,
            'action_generation': 'generation',
            'resolved_action': '#label',
        })
        token = self.codec.dump_postback(postback)
        with self.assertRaises(InvalidStateToken):
            self.codec.load_state(token, 'test', 'bot', 'epoch-1')

    def test_compatibility_epoch不一致を区別する(self):
        token = self.codec.dump_state(self.state)
        with self.assertRaises(IncompatibleState):
            self.codec.load_state(token, 'test', 'bot', 'epoch-2')

    def test_postbackをconversationとaction世代へ束縛する(self):
        postback = {
            'v': TOKEN_VERSION,
            'typ': POSTBACK_TYPE,
            'deployment': 'test',
            'bot': 'bot',
            'scenario_compatibility_epoch': 'epoch-1',
            'scenario_revision': 'revision-a',
            'conversation_id': 'conversation',
            'action_generation': 'generation',
            'resolved_action': '#label',
            'echo_text': '選択',
        }
        token = self.codec.dump_postback(postback)
        loaded = self.codec.load_postback(
            token, self.state, 'test', 'bot', 'epoch-1')
        self.assertEqual('#label', loaded['resolved_action'])

        other = dict(self.state)
        other['conversation_id'] = 'other'
        with self.assertRaises(ActionNotActive):
            self.codec.load_postback(
                token, other, 'test', 'bot', 'epoch-1')

    def test_短い鍵を拒否する(self):
        key = base64.urlsafe_b64encode(b'short').decode('ascii')
        with self.assertRaises(InvalidWebchatConfiguration):
            WebchatTokenCodec(key)

    def test_base64url以外を含む鍵を拒否する(self):
        with self.assertRaises(InvalidWebchatConfiguration):
            WebchatTokenCodec(SIGNING_KEY + '!')


class TokenPlayerStatusTest(unittest.TestCase):
    def test_既存Player意味論と一時変数除外を維持する(self):
        status = TokenPlayerStatus('bot', 'conversation')
        self.assertEqual('*start', status.scene)
        self.assertTrue(status.action_token)
        status['name'] = 'value'
        status['$_temporary'] = 'secret'
        for index in range(7):
            status.push_scene_history(str(index))

        exported = status.export()
        self.assertEqual(['2', '3', '4', '5', '6'], exported['scene_history'])
        self.assertEqual({'name': 'value'}, exported['flags'])

    def test_resetでMore継続もclearする(self):
        status = TokenPlayerStatus('bot', 'conversation')
        status.web_next_label = '##MORE'
        status.web_next_trigger = '続きを読む'
        status.reset()
        self.assertIsNone(status.scene)
        self.assertIsNone(status.web_next_label)
        self.assertIsNone(status.web_next_trigger)

    def test_local_rollbackする(self):
        status = TokenPlayerStatus(
            'bot', 'conversation', {
                'scene': '*one',
                'scene_history': [],
                'action_generation': 'generation',
                'flags': {'value': 1},
            })
        status['value'] = 2
        status.scene = '*two'
        status.rollback()
        self.assertEqual(1, status['value'])
        self.assertEqual('*one', status.scene)

    def test_More_storeをstate内でcompare_and_clearする(self):
        status = TokenPlayerStatus('bot', 'conversation')
        store = TokenNextLabelStore()
        self.assertEqual((None, None), store.set_next_label(
            status, '##MORE', '続きを読む'))
        self.assertEqual(('##MORE', '続きを読む'), store.get_next_label(status))
        self.assertEqual((False, '##MORE'),
                         store.compare_and_clear_next_label(status, 'other'))
        self.assertEqual((True, '##MORE'),
                         store.compare_and_clear_next_label(status, '##MORE'))
        self.assertEqual((None, None), store.get_next_label(status))


class WebchatAudioCommandTest(unittest.TestCase):
    class Builder:
        def __init__(self):
            self.msg_count = 0
            self.command = None

        @staticmethod
        def raise_error(message, *_values):
            raise ValueError(message)

        def add_command(self, sender, message, options, children):
            self.command = (sender, message, options, children)

        @staticmethod
        def parse_imageurl(_value):
            return None

        @staticmethod
        def build_image_for_image_command(value):
            return value, None

        @staticmethod
        def build_video(value):
            return value

    def test_拡張子を自主制限せず正整数durationを受ける(self):
        from common_commands import CommonCommands_Builder
        builder = self.Builder()
        command = CommonCommands_Builder({})
        self.assertTrue(command.build_from_command(
            builder, None, '@audio',
            ['https://media.example.test/signed?id=1', '001']))
        self.assertEqual(
            [
                'https://media.example.test/signed?id=1',
                '1',
                '',
            ],
            builder.command[2],
        )
        self.assertEqual(1, builder.msg_count)

    def test_audioのHTTPと非正整数を拒否する(self):
        from common_commands import CommonCommands_Builder
        command = CommonCommands_Builder({})
        for options in (
                ['http://media.example.test/audio.mp3', '1000'],
                ['https://media.example.test/audio.mp3', '0'],
                ['https://media.example.test/audio.mp3', '1.5']):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    command.build_from_command(
                        self.Builder(), None, '@audio', options)

    def test_audioのUnicode_URLをpercent_encodeする(self):
        from common_commands import CommonCommands_Builder
        builder = self.Builder()
        CommonCommands_Builder({}).build_from_command(
            builder, None, '@audio',
            ['https://media.example.test/音声.m4a', '1000'])
        self.assertEqual(
            'https://media.example.test/%E9%9F%B3%E5%A3%B0.m4a',
            builder.command[2][0],
        )
        self.assertEqual('audio/mp4', builder.command[2][2])

    def test_video完了actionをbuildしLINE完成上限を検査する(self):
        from common_commands import CommonCommands_Builder
        builder = self.Builder()
        CommonCommands_Builder({}).build_from_command(
            builder, None, '@video', [
                'https://media.example.test/poster.png',
                'https://media.example.test/video.mp4',
                '*完了',
            ])
        self.assertEqual('*完了', builder.command[2][2])

        for action in ('通常入力', '*' + ('あ' * 30)):
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    CommonCommands_Builder({}).build_from_command(
                        self.Builder(), None, '@video', [
                            'https://media.example.test/poster.png',
                            'https://media.example.test/video.mp4',
                            action,
                        ])


class WebchatCommandPolicyTest(unittest.TestCase):
    def test_許可commandと同期対象外commandを分ける(self):
        from plugin.webchat.context import WebchatActionContext
        from plugin.webchat.errors import TurnDeadlineExceeded
        interface = types.SimpleNamespace(
            allowed_commands=set(), turn_deadline_seconds=29.0)
        context = WebchatActionContext(
            'bot', interface, 'conversation', '入力', {})
        context.check_command_policy('@log')
        for command in ('@delay', '@forward', '@group_add'):
            with self.subTest(command=command):
                with self.assertRaises(BotNotWebCompatible):
                    context.check_command_policy(command)
        context.deadline = time.monotonic()
        with self.assertRaises(TurnDeadlineExceeded):
            context.check_command_policy('@log')


class WebchatMoreRuntimeTest(unittest.TestCase):
    def test_MoreをDynamoDBなしでstate内だけに保持する(self):
        from plugin.webchat.more import WebchatMoreRuntime
        status = TokenPlayerStatus('bot', 'conversation')
        context = types.SimpleNamespace(
            status=status,
            next_label_store=TokenNextLabelStore(),
        )
        runtime = WebchatMoreRuntime({
            'action_pattern': None,
            'ignore_pattern': None,
            'please_push_more_button_label': '##PLEASE',
        })
        self.assertTrue(runtime.run_command(
            context, None, '@@set_next_label',
            ['##NEXT', '続きを読む']))
        self.assertEqual('##PLEASE', runtime.modify_incoming_action(
            context, '別の入力'))
        self.assertEqual('##NEXT', runtime.modify_incoming_action(
            context, '続きを読む'))
        self.assertEqual((None, None),
                         context.next_label_store.get_next_label(status))


class _FakeWebchatInterface:
    def __init__(self):
        self.actions = []
        self.deadlines = []
        self.turn_deadline_seconds = 29.0
        self.codec = types.SimpleNamespace(
            state_id=lambda _token: 'a' * 64)
        self.scenario_revision = 'scenario-revision'
        self.compatibility_epoch = 'epoch'

    def origin_allowed(self, origin):
        return origin == 'https://app.example.test'

    def create_start_context(self, request_id, deadline_seconds=None):
        self.deadlines.append(deadline_seconds)
        return self._context('##start', request_id)

    def load_state(self, token):
        if token != 'state-token':
            raise InvalidStateToken('invalid')
        return {
            'conversation_id': 'conversation',
            'revision': 0,
            'player': {},
        }

    def load_postback(self, token, _state):
        if token != 'postback-token':
            raise InvalidStateToken('invalid')
        return {'resolved_action': '#next', 'echo_text': '選択'}

    def create_context_from_state(self, _state, action, request_id,
                                  echo_message=None, deadline_seconds=None):
        self.deadlines.append(deadline_seconds)
        return self._context(action, request_id, echo_message)

    def ensure_scenario(self, _bot):
        return None

    def _context(self, action, request_id, echo=None):
        self.actions.append(action)
        return types.SimpleNamespace(
            action=action,
            request_id=request_id,
            echo_message=echo,
            deadline=time.monotonic() + 29,
            user=types.SimpleNamespace(user_id='conversation'),
            original_player={'scene': '*test', 'flags': {}},
            saved_player={'scene': '*test'},
        )


class _FakeWebchatBot:
    def __init__(self, interface):
        self.interface = interface

    def get_interface(self, name):
        return self.interface if name == 'webchat' else None

    def handle_action(self, context):
        return {
            'schema_version': 1,
            'request_id': context.request_id,
            'state': {'id': 'next-state', 'revision': 1},
            'state_token': 'next-token',
            'echo_message': context.echo_message,
            'messages': [],
        }


class WebchatWebApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from plugin.webchat import webapi
        cls.webapi = webapi

    def setUp(self):
        self.interface = _FakeWebchatInterface()
        self.bot = _FakeWebchatBot(self.interface)
        self.webapi.configure(
            lambda name: self.bot if name == 'bot' else None)
        self.app = TestApp(self.webapi.app)

    def test_startとCORSを処理する(self):
        result = self.app.request(
            'POST', '/api/webchat/v1/bots/bot/turn',
            headers={'Origin': 'https://app.example.test'},
            json_body={'input': {'type': 'start'}},
        )
        self.assertEqual(200, result.status_int)
        self.assertEqual('next-token', result.json['state_token'])
        self.assertEqual(
            'https://app.example.test',
            result.headers['Access-Control-Allow-Origin'])
        self.assertEqual('##start', self.interface.actions[-1])

    def test_allowlist外Originを403にする(self):
        result = self.app.request(
            'POST', '/api/webchat/v1/bots/bot/turn',
            headers={'Origin': 'https://evil.example.test'},
            json_body={'input': {'type': 'start'}},
            expect_errors=True,
        )
        self.assertEqual(403, result.status_int)
        self.assertEqual('invalid-origin', result.json['code'])
        self.assertNotIn('Access-Control-Allow-Origin', result.headers)

    def test_textをsanitizeしてstate付きで処理する(self):
        result = self.app.request(
            'POST', '/api/webchat/v1/bots/bot/turn',
            headers={'Origin': 'https://app.example.test'},
            json_body={
                'state_token': 'state-token',
                'input': {'type': 'text', 'text': '#internal'},
            },
        )
        self.assertEqual(200, result.status_int)
        self.assertNotEqual('#internal', self.interface.actions[-1])
        self.assertEqual('#internal', result.json['echo_message'])

    def test_postbackは署名済みresolved_actionだけを実行する(self):
        result = self.app.request(
            'POST', '/api/webchat/v1/bots/bot/turn',
            headers={'Origin': 'https://app.example.test'},
            json_body={
                'state_token': 'state-token',
                'input': {
                    'type': 'postback',
                    'postback_token': 'postback-token',
                },
            },
        )
        self.assertEqual(200, result.status_int)
        self.assertEqual('#next', self.interface.actions[-1])
        self.assertEqual('選択', result.json['echo_message'])

    def test_preflightを許可Originだけへ返す(self):
        result = self.app.request(
            'OPTIONS', '/api/webchat/v1/bots/bot/turn',
            headers={'Origin': 'https://app.example.test'},
        )
        self.assertEqual(204, result.status_int)
        self.assertEqual('POST, OPTIONS',
                         result.headers['Access-Control-Allow-Methods'])

    def test_JSON不正と余分なfieldを400にする(self):
        invalid_json = self.app.request(
            'POST', '/api/webchat/v1/bots/bot/turn',
            headers={'Origin': 'https://app.example.test'},
            raw_body=b'{', raw_content_type='application/json',
            expect_errors=True,
        )
        self.assertEqual(400, invalid_json.status_int)
        self.assertEqual('invalid-request', invalid_json.json['code'])

        extra = self.app.request(
            'POST', '/api/webchat/v1/bots/bot/turn',
            headers={'Origin': 'https://app.example.test'},
            json_body={'input': {'type': 'start'}, 'extra': True},
            expect_errors=True,
        )
        self.assertEqual(400, extra.status_int)
        self.assertEqual('invalid-request', extra.json['code'])

    def test_JSONのcharset_parameterを受理する(self):
        result = self.app.request(
            'POST', '/api/webchat/v1/bots/bot/turn',
            headers={'Origin': 'https://app.example.test'},
            raw_body=json.dumps({'input': {'type': 'start'}}).encode('utf-8'),
            raw_content_type='application/json; charset=UTF-8',
        )
        self.assertEqual(200, result.status_int)

    def test_stateなしtextを400にする(self):
        result = self.app.request(
            'POST', '/api/webchat/v1/bots/bot/turn',
            headers={'Origin': 'https://app.example.test'},
            json_body={'input': {'type': 'text', 'text': '本文'}},
            expect_errors=True,
        )
        self.assertEqual(400, result.status_int)
        self.assertEqual('invalid-request', result.json['code'])

    def test_Lambda由来request_IDと残り時間を使う(self):
        request_id = '6a8d4cc8-9a3e-4db5-bd7a-f8c9763cbaf4'
        lambda_context = json.dumps({
            'request_id': request_id,
            'deadline': int((time.time() + 5) * 1000),
        })
        result = self.app.request(
            'POST', '/api/webchat/v1/bots/bot/turn',
            headers={
                'Origin': 'https://app.example.test',
                'X-Amzn-Lambda-Context': lambda_context,
            },
            json_body={'input': {'type': 'start'}},
        )
        self.assertEqual(request_id, result.json['request_id'])
        self.assertGreater(self.interface.deadlines[-1], 0)
        self.assertLessEqual(self.interface.deadlines[-1], 5)

    def test_例外messageの秘密値をlogへ出さない(self):
        self.bot.handle_action = lambda _context: (
            (_ for _ in ()).throw(RuntimeError('API_TOKEN_SUPER_SECRET')))
        with patch.object(self.webapi.logging, 'error') as error_log:
            result = self.app.request(
                'POST', '/api/webchat/v1/bots/bot/turn',
                headers={'Origin': 'https://app.example.test'},
                json_body={'input': {'type': 'start'}},
                expect_errors=True,
            )
        self.assertEqual(500, result.status_int)
        self.assertNotIn('API_TOKEN_SUPER_SECRET', str(error_log.call_args_list))


class WebchatInterfaceConfigurationTest(unittest.TestCase):
    class Response:
        def __init__(self, status=200, body=b'{}', headers=None):
            self.status_code = status
            self.headers = headers or {}
            self.is_redirect = status in (301, 302, 303, 307, 308)
            self.is_permanent_redirect = status in (308,)
            self.body = body
            self.closed = False

        def iter_content(self, chunk_size=None):
            return iter((self.body,))

        def close(self):
            self.closed = True

    def _params(self, **overrides):
        params = {
            'enabled': True,
            'deployment': 'test',
            'scenario_uri': 's3://private/scenario/' + ('a' * 32),
            'scenario_compatibility_epoch': 'epoch',
            'start_action': '##start',
            'allowed_origins': (
                'https://APP.example.test:443,https://other.example.test:8443'),
            'external_http_origins': '',
            'media_origins': 'https://media.example.test:443',
            'signing_key': SIGNING_KEY,
        }
        params.update(overrides)
        return params

    def test_originを正規化し空の外部allowlistを許可する(self):
        from plugin.webchat.interface import WebchatInterface
        interface = WebchatInterface('bot', self._params())
        self.assertTrue(interface.origin_allowed('https://app.example.test'))
        self.assertTrue(interface.origin_allowed(
            'https://other.example.test:8443'))
        self.assertEqual((), interface.external_http_origins)
        self.assertEqual(
            'https://media.example.test/audio',
            interface.validate_media_url(
                'https://media.example.test/audio'))

    def test_同一originだけなら追加allowlistを要求しない(self):
        from plugin.webchat.interface import WebchatInterface
        interface = WebchatInterface('bot', self._params(
            allowed_origins='',
            self_origin='https://api.example.test:443',
        ))
        self.assertEqual(
            ('https://api.example.test',), interface.allowed_origins)

    def test_mediaのschemeとoriginを拒否する(self):
        from plugin.webchat.interface import WebchatInterface
        interface = WebchatInterface('bot', self._params())
        for url in (
                'http://media.example.test/audio',
                'https://evil.example.test/audio'):
            with self.subTest(url=url):
                with self.assertRaises(BotNotWebCompatible):
                    interface.validate_media_url(url)

    def test_外部HTTPはallowlist内だけを読んで本文をlogしない(self):
        from plugin.webchat.interface import WebchatInterface
        interface = WebchatInterface('bot', self._params(
            external_http_origins='https://hooks.example.test'))
        response = self.Response(body=b'{"secret":"not-logged"}')
        context = types.SimpleNamespace(
            deadline=time.monotonic() + 5,
            request_id='request',
            user=types.SimpleNamespace(user_id='conversation'),
        )
        with (
                patch('plugin.webchat.interface.requests.request',
                      return_value=response) as request_mock,
                patch('plugin.webchat.interface.logging.info') as info):
            result = interface.request_external(
                context, 'GET',
                'https://hooks.example.test/path?token=hidden')
        self.assertEqual(b'{"secret":"not-logged"}', result._content)
        self.assertTrue(response.closed)
        request_mock.assert_called_once()
        self.assertNotIn('not-logged', str(info.call_args_list))
        self.assertNotIn('token=hidden', str(info.call_args_list))

    def test_外部HTTPのallowlist外redirectを追わない(self):
        from plugin.webchat.interface import WebchatInterface
        interface = WebchatInterface('bot', self._params(
            external_http_origins='https://hooks.example.test'))
        response = self.Response(
            status=302,
            headers={'Location': 'https://evil.example.test/path'})
        context = types.SimpleNamespace(
            deadline=time.monotonic() + 5,
            request_id='request',
            user=types.SimpleNamespace(user_id='conversation'),
        )
        with patch(
                'plugin.webchat.interface.requests.request',
                return_value=response) as request_mock:
            with self.assertRaises(BotNotWebCompatible):
                interface.request_external(
                    context, 'GET', 'https://hooks.example.test/start')
        request_mock.assert_called_once()
        self.assertTrue(response.closed)

    def test_redirect先へ最初のquery_paramsを再適用しない(self):
        from plugin.webchat.interface import WebchatInterface
        interface = WebchatInterface('bot', self._params(
            external_http_origins='https://hooks.example.test'))
        redirect = self.Response(
            status=302, headers={'Location': '/next?redirected=1'})
        completed = self.Response(body=b'{}')
        context = types.SimpleNamespace(
            deadline=time.monotonic() + 5,
            request_id='request',
            user=types.SimpleNamespace(user_id='conversation'),
        )
        with patch(
                'plugin.webchat.interface.requests.request',
                side_effect=[redirect, completed]) as request_mock:
            interface.request_external(
                context, 'GET', 'https://hooks.example.test/start',
                params={'first': '1'})
        self.assertEqual(
            {'first': '1'}, request_mock.call_args_list[0].kwargs['params'])
        self.assertEqual(
            'https://hooks.example.test/next?redirected=1',
            request_mock.call_args_list[1].args[1])
        self.assertNotIn('params', request_mock.call_args_list[1].kwargs)

    def test_固定Scenarioをprocess内で一度だけ読む(self):
        from plugin.webchat.interface import WebchatInterface
        interface = WebchatInterface('bot', self._params())
        scenario = object()
        load = Mock(return_value=scenario)
        module = types.SimpleNamespace(
            Scenario=types.SimpleNamespace(load_from_uri=load))
        bot = types.SimpleNamespace(scenario=None, scenario_uri=None)
        with patch.dict(sys.modules, {'scenario': module}):
            interface.ensure_scenario(bot)
            interface.ensure_scenario(bot)
        self.assertIs(scenario, bot.scenario)
        load.assert_called_once_with(interface.scenario_uri)


class _PresenterInterface:
    sender_icon_urls = {'話者': 'https://media.example/icon.png'}
    reply_fallback_message = '選択してください'
    alt_text = '選択可能な画像'

    @staticmethod
    def make_postback_action(_context, label, action, echo):
        return {
            'type': 'postback',
            'label': label,
            'token': f'token:{action}',
            'echo_text': echo,
        }

    @staticmethod
    def preview_image_url(url):
        return url.replace('_1024.', '_240.')

    @staticmethod
    def validate_media_url(url):
        return url


class WebchatPresenterTest(unittest.TestCase):
    def setUp(self):
        from plugin.webchat.presenter import WebchatPresenter
        self.presenter = WebchatPresenter(_PresenterInterface())
        self.context = types.SimpleNamespace(
            response=[], version=3, service_name='webchat')

    def test_buttonをmessage_postback_URIへ変換する(self):
        handled = self.presenter.construct_template(
            self.context, '話者', '@button',
            ['本文', 'タイトル', 'https://media.example/image.png'],
            [
                ['通常'],
                ['内部', '#next'],
                ['表示付き', '表示', '#third'],
                ['外部', 'https://example.test/'],
            ],
        )
        self.assertTrue(handled)
        message = self.context.response[0]
        self.assertEqual('button', message['type'])
        self.assertEqual('話者', message['sender']['name'])
        self.assertEqual(
            ['message', 'postback', 'postback', 'uri'],
            [action['type'] for action in message['actions']],
        )
        self.assertEqual('token:#third', message['actions'][2]['token'])
        self.assertEqual('表示', message['actions'][2]['echo_text'])

    def test_confirmをsimple_buttonへ平坦化する(self):
        self.presenter.construct_template(
            self.context, None, '@confirm', ['確認'],
            [['はい'], ['いいえ']],
        )
        self.assertEqual('button', self.context.response[0]['type'])

    def test_carouselとFlexを固定非対応にする(self):
        from plugin.webchat.errors import BotNotWebCompatible
        with self.assertRaises(BotNotWebCompatible):
            self.presenter.construct_template(
                self.context, None, '@carousel', [], [])
        with self.assertRaises(BotNotWebCompatible):
            self.presenter.construct_template(
                self.context, None, '@flex', ['{}'], [])

    def test_replyを直前messageへ付加する(self):
        self.context.response = [{
            'type': 'text', 'text': '選んでください',
        }]
        self.presenter.construct_template(
            self.context, None, '@reply', [], [['選択', '#one']])
        replies = self.context.response[0]['quick_replies']
        self.assertEqual('postback', replies[0]['type'])

    def test_imagemapへ実画像variantとarea_actionを付ける(self):
        self.presenter.construct_template(
            self.context, None, '@imagemap',
            ['https://media.example/map.png', '1040', '520'],
            [['0,0,520,520', '選択']],
        )
        message = self.context.response[0]
        self.assertEqual(
            'https://media.example/map.png/1040',
            message['image_url'])
        self.assertEqual(
            [460, 1040],
            [source['width'] for source in message['sources']])
        self.assertEqual('message', message['areas'][0]['action']['type'])

    def test_media_reactionをMessageSpecへ変換する(self):
        messages = self.presenter.present(self.context, [
            ([None, '@image', 'https://media.example/a_1024.png'], None),
            ([None, '@rawimage', 'https://media.example/b.png',
              'https://media.example/b-preview.png'], None),
            ([None, '@video', 'https://media.example/poster.png',
              'https://media.example/video.mp4'], None),
            ([None, '@audio', 'https://media.example/audio.mp3',
              1000, 'audio/mpeg'], None),
            ([None, '本文'], None),
        ])
        self.assertEqual(
            ['image', 'image', 'video', 'audio', 'text'],
            [message['type'] for message in messages],
        )
        self.assertNotIn('completion_action', messages[2])

    def test_video完了actionを署名postbackへ変換する(self):
        messages = self.presenter.present(self.context, [
            ([None, '@video', 'https://media.example/poster.png',
              'https://media.example/video.mp4', '*完了'], None),
        ])
        action = messages[0]['completion_action']
        self.assertEqual('postback', action['type'])
        self.assertEqual('', action['label'])
        self.assertEqual('token:*完了', action['token'])
        self.assertIsNone(action['echo_text'])


if __name__ == '__main__':
    unittest.main()
