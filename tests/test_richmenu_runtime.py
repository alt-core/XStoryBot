"""実シナリオと署名セーブで、メニューの保存・旧画面・定数を確認する。"""
import json
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

from plugin.webchat.interface import WebchatInterface, WebchatInterfaceFactory
from plugin.webchat.state import TokenPlayerStatus
from tests.plugin import test_webchat_liff as fixtures
from tests.test_richmenu_spec import menu_settings


class RichmenuRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.WebchatLiffTest.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixtures.WebchatLiffTest.tearDownClass()

    def setUp(self):
        self.case = fixtures.WebchatLiffTest()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.fixture = fixtures.fixture_module.WebchatRuntimeE2ETest
        self.definitions = menu_settings()
        self.params = {**self.case.interface.params, 'constants': {'menu_url': 'https://pages.example.test/', 'url': 'web'}}
        self.install()
        self.case.root.scenario = self.fixture.scenario_module.ScenarioBuilder.build_from_table([
            ['##line.follow', '@richmenu', 'MAIN'], ['', '{url}'],
            ['#help', 'メニューを押しました'], ['#other', '別の操作'],
            ['reset', '@reset'],
        ], constants={'url': 'sheet'}, version=3)

    def install(self):
        self.interface = WebchatInterface('bot', self.params, bot_settings=self.definitions)
        self.interface._scenario_loaded = True
        self.case.interface = self.interface
        self.case.root.interfaces['webchat'] = self.interface

    def menu_turn(self, response, **changes):
        menu = response['richmenu']
        return self.fixture._turn({'type': 'menu', 'menu': menu['id'], 'revision': menu['revision'], 'area': 0, **changes},
                                  response['state_token'], expect_errors=True)

    def test_保存と定数上書きとメニュー操作(self):
        first = self.case.start()
        self.assertEqual('web', first['messages'][0]['text'])
        self.assertEqual('main', self.interface.load_state(first['state_token'])['player']['richmenu'])
        result = self.menu_turn(first).json
        self.assertEqual('メニューを押しました', result['messages'][0]['text'])
        self.assertEqual('ヘルプ', result['echo_message'])
        reset = self.fixture._turn({'type': 'text', 'text': 'reset'}, result['state_token']).json
        self.assertEqual('main', reset['richmenu']['id'])
        self.assertEqual('main', self.interface.load_state(reset['state_token'])['player']['richmenu'])

    def test_古い領域は進行せず最新メニューを返す(self):
        first = self.case.start()
        self.definitions['richmenus']['main']['areas'][0]['action']['data'] = '#other'
        self.install()
        result = self.menu_turn(first).json
        self.assertTrue(result['menu_updated'])
        self.assertFalse(result['chat_updated'])
        self.assertEqual(first['state_token'], result['state_token'])
        self.assertEqual([], result['messages'])
        self.assertNotEqual(first['richmenu']['revision'], result['richmenu']['revision'])
        next_result = self.menu_turn(result).json
        self.assertEqual('別の操作', next_result['messages'][0]['text'])
        self.assertEqual(400, self.menu_turn(result, area=True).status_int)
        self.assertEqual(400, self.menu_turn(result, area=1).status_int)
        self.assertEqual(400, self.menu_turn(result, area=2).status_int)

    def test_状態のrollbackと旧セーブ(self):
        status = TokenPlayerStatus('bot', 'id', {'richmenu': 'main'})
        status.richmenu = 'other'; status.rollback()
        self.assertEqual('main', status.richmenu)
        status.reset()
        self.assertEqual('main', status.export()['richmenu'])
        self.assertIsNone(TokenPlayerStatus('bot', 'id', {}).richmenu)

    def test_論理名はビルド時に位置付きで検査し動的参照と旧IDを維持する(self):
        from plugin.scenario_table import SourceRow
        builder = self.fixture.scenario_module.ScenarioBuilder
        options = {'richmenu_names': {'main'}, 'richmenu_ids': {}, 'richmenu_require_ids': True}
        for value in ('missing', 'MAIN'):
            with self.subTest(value=value), self.assertRaises(self.fixture.scenario_module.ScenarioSyntaxError) as raised:
                builder.build_from_table([SourceRow(['##line.follow', '@richmenu', value], '人工シート', 6)],
                                         options=options, version=3)
            self.assertIn('人工シート', str(raised.exception))
        for value in ('{menu}', 'richmenu-' + 'a' * 32, 'richmenu-old'):
            builder.build_from_table([['##line.follow', '@richmenu', value]], options=options, version=3)
        options['richmenu_ids'] = {'main': 'richmenu-' + 'a' * 32}
        builder.build_from_table([['##line.follow', '@richmenu', 'MAIN']], options=options, version=3)

    def test_Webchatの未定義の動的名はセーブを更新せず生IDは無視する(self):
        self.case.root.scenario = self.fixture.scenario_module.ScenarioBuilder.build_from_table([
            ['##line.follow', '開始'],
            ['bad', '@set', '$menu', '"missing"'], ['', '@richmenu', '{$menu}'], ['', '進行しました'],
            ['raw', '@richmenu', 'richmenu-old'], ['', '継続しました'],
        ], version=3)
        first = self.case.start()
        failed = self.fixture._turn({'type': 'text', 'text': 'bad'}, first['state_token'], expect_errors=True)
        self.assertEqual(500, failed.status_int)
        self.assertNotIn('state_token', failed.json)
        self.assertNotIn('$menu', self.interface.load_state(first['state_token'])['player']['flags'])
        resumed = self.fixture._turn({'type': 'text', 'text': 'raw'}, first['state_token']).json
        self.assertEqual('継続しました', resumed['messages'][0]['text'])
        self.assertEqual('main', resumed['richmenu']['id'])

    def test_Webchat専用定数のメニューをLINE用の値なしでビルドできる(self):
        bot = self.case.root
        bot.scenario_loader = Mock()
        bot.scenario_loader.load_scenario.return_value = ([('story', [
            ['##line.follow', '@richmenu', 'main'], ['', '開始'],
        ])], {})
        scenario = self.fixture.scenario_module
        models = types.SimpleNamespace(GlobalBotVariablesDB=Mock())
        cache = types.SimpleNamespace(set_cache=Mock())
        self.assertIsNone(bot.get_interface('line'))
        with patch.dict(sys.modules, {'models': models, 'build_cache': cache, 'scenario': scenario}), \
                patch.object(self.fixture.runtime.settings, 'BOTS', {'bot': self.definitions}, create=True), \
                patch.object(self.fixture.runtime.settings, 'CONSTANTS', {}), \
                patch.object(scenario.Scenario, 'save_to_storage', return_value=self.interface.scenario_uri), \
                patch('richmenu_service.create_object_store', side_effect=AssertionError('対応表の読込みは不要')):
            self.assertEqual((True, None), bot.build_scenario(version=3))
            self.assertEqual({}, bot.scenario.richmenu_ids)
            self.assertEqual('開始', self.case.start()['messages'][0]['text'])
            self.definitions.clear()
            success, error = bot.build_scenario(version=3)
            self.assertFalse(success)
            self.assertIn('リッチメニュー main', error)
            self.assertIn('story', error)

        from richmenu_spec import parse_bot_settings
        with self.assertRaisesRegex(ValueError, '定数 menu_url が定義されていません'):
            parse_bot_settings(menu_settings(), {})

    def test_LINEでも動的な未定義名は状態保存とAPI呼出しより前に拒否する(self):
        from plugin.line.interface import LinePlugin_Interface
        from users import User
        interface = LinePlugin_Interface('bot', {
            'line_access_token': 'artificial', 'line_channel_secret': 'artificial',
        })
        interface.api = Mock()
        self.case.root.interfaces['line'] = interface
        self.case.root.scenario = self.fixture.scenario_module.ScenarioBuilder.build_from_table([
            ['失敗', '@set', '$menu', '"missing"'], ['', '@richmenu', '{$menu}'], ['', '進行しました'],
        ], version=3)
        context = interface.create_context(User('line', 'user,artificial'), '失敗', {})
        context.save_status = Mock()
        with patch('context._get_player_status_class', return_value=TokenPlayerStatus), \
                self.assertLogs(level='ERROR') as logged:
            self.assertIsNone(self.case.root.handle_action(context))
        self.assertIn('リッチメニュー missing', '\n'.join(logged.output))
        context.save_status.assert_not_called()
        self.assertEqual([], interface.api.mock_calls)

    def test_LIFF側にも独自の定数上書きが効く(self):
        controller = WebchatInterface('menu', {**self.case.menu.get_interface('webchat').params, 'constants': {'url': 'peer'}})
        controller._scenario_loaded = True
        self.case.menu.interfaces['webchat'] = controller
        self.case.menu.scenario = self.fixture.scenario_module.ScenarioBuilder.build_from_table([
            ['##liff.read', '{url}'],
        ], constants={'url': 'sheet'}, version=3)
        result = self.case.call(self.case.start()['state_token'], 'read').json
        self.assertEqual(['peer'], result['liff_result'])
        self.assertEqual('main', result['richmenu']['id'])

    def test_上書きは層ごとに正規化して合成する(self):
        factory = WebchatInterfaceFactory({**self.params, 'constants': {'ＵＲＬ': 'plugin', 'other': 1}})
        interface = factory.create_interface('bot', {'constants': {'url': 'bot'}})
        self.assertEqual({'url': 'bot', 'other': 1}, interface.constants_override)

    def test_Sheet優先と明示interfaceの契約を維持する(self):
        class Responder:
            __slots__ = ()

            def get_retry_count(self): return 0
            def should_raise_exceptions(self): return True
            def respond_reaction(self, context, reactions):
                return context.env['url'], context.env['fallback']

        self.params['constants'].pop('url')
        self.install()
        original = self.case.root.interfaces.copy()
        context = self.interface.create_start_context('artificial')
        with patch.object(self.fixture.runtime.settings, 'CONSTANTS', {'url': 'settings', 'fallback': 'base'}):
            result = self.case.root.handle_action(context, interface=Responder())
        self.assertEqual(('sheet', 'base'), result)
        self.assertEqual(original, self.case.root.interfaces)


    def test_Webchatの起動はPillowと状態DBを読み込まない(self):
        params = {**self.params, 'liff_apps': {}}
        definitions = {**self.definitions, 'interfaces': [{'type': 'webchat', 'params': params}]}
        payload = json.dumps(definitions)
        script = """
import builtins, json, sys, types
settings = types.ModuleType('settings')
settings.CLOUD_SETTINGS = {'provider': 'aws'}
settings.DEPLOY_ENV = 'test'
settings.OPTIONS = {'reset_keyword': '!reset', 'timezone': 'UTC'}
settings.PLUGINS = {}
settings.BACKEND_SETTINGS = {}
settings.CONSTANTS = {}
settings.BOTS = {'bot': json.loads(sys.argv[1])}
sys.modules['settings'] = settings
original = builtins.__import__
def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and (name == 'models' or name == 'PIL' or name.startswith('PIL.')):
        raise AssertionError(name)
    return original(name, globals, locals, fromlist, level)
builtins.__import__ = guarded
import app_webchat
assert app_webchat.get_bot('bot').get_interface('webchat').richmenu_specs['main']
"""
        result = subprocess.run([sys.executable, '-B', '-c', script, payload],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
