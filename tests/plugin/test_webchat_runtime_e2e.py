import base64
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import commands
import common_commands
import expression
import hub
import requests
from plugin.line import default_commands, quick_reply
from plugin.webchat import more as webchat_more
from plugin.webchat.interface import WebchatInterfaceFactory
from plugin.webchat import webapi
import runtime
from runtime import BotRuntime
from tests.test_api_endpoints import TestApp


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORIGIN = 'https://app.example.test'
SIGNING_KEY = base64.urlsafe_b64encode(
    b'webchat-runtime-e2e-signing-key!').decode('ascii').rstrip('=')


def _load_scenario_module():
    """ObjectStoreだけを差し替え、実Scenario／Directorを隔離loadする。"""
    object_store = Mock()
    cloud_backend = types.ModuleType('cloud_backend')
    cloud_backend.__path__ = []
    cloud_backend.create_object_store = Mock(return_value=object_store)
    contracts = types.ModuleType('cloud_backend.contracts')

    class InvalidObjectReferenceError(Exception):
        pass

    contracts.InvalidObjectReferenceError = InvalidObjectReferenceError
    module_name = 'tests._webchat_runtime_e2e_scenario'
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / 'scenario.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        'cloud_backend': cloud_backend,
        'cloud_backend.contracts': contracts,
        module_name: module,
    }):
        spec.loader.exec_module(module)
    return module


class WebchatRuntimeE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._commands = (
            list(commands.catalog),
            {key: list(value) for key, value in commands.catalog_map.items()},
            list(commands.object_catalog),
            {
                key: list(value)
                for key, value in commands.object_catalog_map.items()
            },
        )
        cls._hub = (
            list(hub.builder_list),
            list(hub.runtime_list),
            dict(hub.interface_factory_map),
            dict(hub.scenario_loader_factory_map),
            {key: list(value) for key, value in hub.method_cache.items()},
        )
        cls._director_class = runtime._director_class
        cls._webapi_callback = webapi._get_bot_callback
        cls._expression = (
            expression.EXPRESSION_VERSION,
            expression.TRUE_VALUE,
            expression.FALSE_VALUE,
            expression.NONE_VALUE,
        )

        commands.clear()
        hub.clear()
        options = {
            'reset_keyword': '!!reset!!',
            'timezone': 'Asia/Tokyo',
        }
        common_commands.setup(options)
        default_commands.inner_load_plugin({
            **options,
            'alt_text': '代替テキスト',
            'sender_icon_urls': {},
            'reply_fallback_message': '選択してください',
            'disable_response_length_check': False,
        })
        quick_reply.load_plugin({
            **options,
            'command': ['＞'],
            'command_without_guard': [],
            'default_reply': 'A',
            'please_select_quick_reply_label': '##please-select',
            'ignore_pattern': None,
        })

        # build時は既存LINE Moreと同じ内部commandを使い、runtimeだけWeb用にする。
        commands.register_commands([
            commands.CommandEntry(
                names=[webchat_more.SET_NEXT_LABEL_CMD],
                options='label [text]',
                builder=commands.Default_Builder(),
                service='line'),
            commands.CommandEntry(
                names=webchat_more.CLEAR_NEXT_LABEL_CMDS,
                builder=commands.Default_Builder(),
                service='line'),
        ])
        webchat_more.load_plugin({
            **options,
            'action_pattern': None,
            'ignore_pattern': None,
            'please_push_more_button_label': '##please-more',
        })

        factory = WebchatInterfaceFactory({
            **options,
            'enabled': True,
            'deployment': 'test',
            'signing_key': SIGNING_KEY,
            'scenario_uri': (
                's3://private/scenario/' + ('a' * 32)),
            'scenario_compatibility_epoch': 'epoch-1',
            'start_action': '##line.follow',
            'allowed_origins': [ORIGIN],
            'external_http_origins': ['https://hooks.example.test'],
            'media_origins': ['https://media.example.test'],
            'sender_icon_urls': {},
        })
        cls.interface = factory.create_interface('bot', {})

        cls.scenario_module = _load_scenario_module()
        original_imagemap_builder = (
            cls.scenario_module.ScenarioBuilder
            .build_image_for_imagemap_command)
        original_image_builder = (
            cls.scenario_module.ScenarioBuilder
            .build_image_for_image_command)
        original_video_builder = (
            cls.scenario_module.ScenarioBuilder.build_video)
        cls.scenario_module.ScenarioBuilder.build_image_for_imagemap_command = (
            lambda _builder, _url: (
                'https://media.example.test/imagemap/map.png',
                (1040, 520),
            ))
        cls.scenario_module.ScenarioBuilder.build_image_for_image_command = (
            lambda _builder, url: (url, None))
        cls.scenario_module.ScenarioBuilder.build_video = (
            lambda _builder, url: url)
        try:
            cls.scenario = cls.scenario_module.ScenarioBuilder.build_from_table([
                ['##line.follow', '開始しました'],
                ['', '@button', '選択してください'],
                ['', '', '次へ', '次へ', '#next'],
                ['#next', '到着しました'],
                ['flag', '@set', '$value', '1'],
                ['', '保存しました'],
                ['quick', '質問です'],
                ['', '＞', 'A', 'B=>表示B'],
                ['', '/end'],
                ['', '選択後です'],
                ['more', '前半です'],
                ['', '@@set_next_label', '##after', '続きを読む'],
                ['##after', '後半です'],
                ['audio', '@audio',
                 'https://media.example.test/audio.mp3', '1000'],
                ['video', '@video',
                 'https://media.example.test/poster.png',
                 'https://media.example.test/video.mp4', '*video-done'],
                ['map', '@imagemap',
                 '=IMAGE("https://source.example.test/map.png")'],
                ['', '', '0,0,1040,520', '押す'],
                ['post', '@postjson',
                 'https://hooks.example.test/post',
                 '{{"input":1}}', 'value'],
                ['', '結果:{$_value}'],
                ['log', '@log', 'e2e', '到達'],
                ['delay', '@delay', '1', '#later'],
                ['#later', '遅延しました'],
                ['flex', '@flex', '{}'],
                ['*video-done', '動画が完了しました'],
            ], options={'force': True}, version=3)
        finally:
            (cls.scenario_module.ScenarioBuilder
             .build_image_for_imagemap_command) = original_imagemap_builder
            (cls.scenario_module.ScenarioBuilder
             .build_image_for_image_command) = original_image_builder
            (cls.scenario_module.ScenarioBuilder
             .build_video) = original_video_builder

        cls.bot = BotRuntime(
            'bot', {'webchat': cls.interface}, scenario_loader=None)
        cls.bot.scenario = cls.scenario
        cls.bot.scenario_uri = cls.interface.scenario_uri
        cls.interface._scenario_loaded = True
        runtime._director_class = cls.scenario_module.Director
        webapi.configure(lambda name: cls.bot if name == 'bot' else None)
        cls.app = TestApp(webapi.app)

    @classmethod
    def tearDownClass(cls):
        commands.clear()
        commands.catalog.extend(cls._commands[0])
        commands.catalog_map.update(cls._commands[1])
        commands.object_catalog.extend(cls._commands[2])
        commands.object_catalog_map.update(cls._commands[3])

        hub.clear()
        hub.builder_list.extend(cls._hub[0])
        hub.runtime_list.extend(cls._hub[1])
        hub.interface_factory_map.update(cls._hub[2])
        hub.scenario_loader_factory_map.update(cls._hub[3])
        hub.method_cache.update(cls._hub[4])

        runtime._director_class = cls._director_class
        webapi._get_bot_callback = cls._webapi_callback
        (
            expression.EXPRESSION_VERSION,
            expression.TRUE_VALUE,
            expression.FALSE_VALUE,
            expression.NONE_VALUE,
        ) = cls._expression
        sys.modules.pop('tests._webchat_runtime_e2e_scenario', None)

    @classmethod
    def _turn(cls, input_data, state_token=None, expect_errors=False):
        body = {'input': input_data}
        if state_token is not None:
            body['state_token'] = state_token
        return cls.app.request(
            'POST', '/api/webchat/v1/bots/bot/turn',
            headers={'Origin': ORIGIN},
            json_body=body,
            expect_errors=expect_errors,
        )

    @classmethod
    def _start(cls):
        return cls._turn({'type': 'start'})

    def test_start_postback_replayと手入力偽装を実Directorで処理する(self):
        started = self._start()
        self.assertEqual(200, started.status_int)
        self.assertEqual(
            ['text', 'button'],
            [message['type'] for message in started.json['messages']])
        postback = started.json['messages'][1]['actions'][0]['token']

        first = self._turn(
            {'type': 'postback', 'postback_token': postback},
            started.json['state_token'])
        replayed = self._turn(
            {'type': 'postback', 'postback_token': postback},
            started.json['state_token'])
        self.assertEqual(
            [message['text'] for message in first.json['messages']],
            [message['text'] for message in replayed.json['messages']])
        self.assertEqual(
            first.json['state_token'], replayed.json['state_token'])
        self.assertEqual(1, first.json['state']['revision'])
        self.assertEqual('到着しました', first.json['messages'][0]['text'])

        spoofed = self._turn(
            {'type': 'text', 'text': '#next'},
            started.json['state_token'])
        self.assertEqual([], spoofed.json['messages'])

    def test_flag_audio_imagemapを実DirectorからMessageSpecへ変換する(self):
        started = self._start()

        flagged = self._turn(
            {'type': 'text', 'text': 'flag'},
            started.json['state_token'])
        payload = self.interface.load_state(flagged.json['state_token'])
        self.assertEqual(1, payload['player']['flags']['$value'])
        self.assertEqual('保存しました', flagged.json['messages'][0]['text'])

        audio = self._turn(
            {'type': 'text', 'text': 'audio'},
            started.json['state_token'])
        self.assertEqual('audio', audio.json['messages'][0]['type'])
        self.assertEqual(1000, audio.json['messages'][0]['duration_ms'])

        imagemap = self._turn(
            {'type': 'text', 'text': 'map'},
            started.json['state_token'])
        message = imagemap.json['messages'][0]
        self.assertEqual('imagemap', message['type'])
        self.assertEqual(
            'https://media.example.test/imagemap/map.png/1040',
            message['image_url'])
        self.assertEqual([460, 1040], [
            source['width'] for source in message['sources']])

    def test_video完了postbackでscene遷移し旧世代を拒否する(self):
        started = self._start()
        video = self._turn(
            {'type': 'text', 'text': 'video'},
            started.json['state_token'])
        message = video.json['messages'][0]
        self.assertEqual('video', message['type'])
        completion = message['completion_action']
        self.assertEqual('postback', completion['type'])
        self.assertIsNone(completion['echo_text'])

        completed = self._turn(
            {'type': 'postback', 'postback_token': completion['token']},
            video.json['state_token'])
        self.assertEqual(
            '動画が完了しました', completed.json['messages'][0]['text'])
        self.assertIsNone(completed.json['echo_message'])

        stale = self._turn(
            {'type': 'postback', 'postback_token': completion['token']},
            completed.json['state_token'], expect_errors=True)
        self.assertEqual(409, stale.status_int)
        self.assertEqual('action-not-active', stale.json['code'])

    def test_Quick_ReplyとMoreを署名stateだけで進行する(self):
        started = self._start()
        quick = self._turn(
            {'type': 'text', 'text': 'quick'},
            started.json['state_token'])
        quick_replies = quick.json['messages'][-1]['quick_replies']
        self.assertEqual(['A', 'B'], [
            action['label'] for action in quick_replies])
        selected = self._turn(
            {
                'type': 'postback',
                'postback_token': quick_replies[0]['token'],
            },
            quick.json['state_token'])
        self.assertIn(
            '選択後です',
            [message.get('text') for message in selected.json['messages']])

        more = self._turn(
            {'type': 'text', 'text': 'more'},
            started.json['state_token'])
        continued = self._turn(
            {'type': 'text', 'text': '続きを読む'},
            more.json['state_token'])
        self.assertEqual('後半です', continued.json['messages'][0]['text'])
        payload = self.interface.load_state(continued.json['state_token'])
        self.assertIsNone(payload['player']['web_next_label'])

    def test_forbidden_commandと構造的非対応を固定errorにする(self):
        started = self._start()
        delay = self._turn(
            {'type': 'text', 'text': 'delay'},
            started.json['state_token'],
            expect_errors=True)
        self.assertEqual(422, delay.status_int)
        self.assertEqual('bot-not-web-compatible', delay.json['code'])
        self.assertNotIn('state_token', delay.json)

        flex = self._turn(
            {'type': 'text', 'text': 'flex'},
            started.json['state_token'],
            expect_errors=True)
        self.assertEqual(422, flex.status_int)
        self.assertEqual('bot-not-web-compatible', flex.json['code'])

    def test_replayでlogが重複しDynamoDB経路を呼ばない(self):
        started = self._start()
        with (
                patch('cloud_backend.create_state_store') as state_store,
                patch('cloud_backend.create_credential_source') as credentials,
                patch.object(common_commands.logging, 'info') as info):
            first = self._turn(
                {'type': 'text', 'text': 'log'},
                started.json['state_token'])
            second = self._turn(
                {'type': 'text', 'text': 'log'},
                started.json['state_token'])

        self.assertEqual(200, first.status_int)
        self.assertEqual(200, second.status_int)
        self.assertEqual(2, sum(
            'XSBLog' in str(call)
            for call in info.call_args_list))
        state_store.assert_not_called()
        credentials.assert_not_called()

    def test_POSTJSONを自動retryせずreplay時の重複を許容する(self):
        started = self._start()

        def response(*_args, **_kwargs):
            result = requests.Response()
            result.status_code = 200
            result.headers['Content-Type'] = 'application/json'
            result._content = b'{"value":"OK"}'
            result._content_consumed = True
            return result

        with patch(
                'plugin.webchat.interface.requests.request',
                side_effect=response) as request_mock:
            first = self._turn(
                {'type': 'text', 'text': 'post'},
                started.json['state_token'])
            replayed = self._turn(
                {'type': 'text', 'text': 'post'},
                started.json['state_token'])

        self.assertEqual(2, request_mock.call_count)
        self.assertEqual('POST', request_mock.call_args.args[0])
        self.assertEqual(
            ['結果:OK'],
            [message['text'] for message in first.json['messages']])
        self.assertEqual(
            ['結果:OK'],
            [message['text'] for message in replayed.json['messages']])
        payload = self.interface.load_state(first.json['state_token'])
        self.assertNotIn('$_value', payload['player']['flags'])


if __name__ == '__main__':
    unittest.main()
