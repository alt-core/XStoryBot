"""実Scenarioと署名tokenで、LIFF状態と同期forwardを検証する。"""

import builtins
import unittest
from unittest.mock import patch

from plugin.webchat import webapi
from plugin.webchat.interface import WebchatInterface
from plugin.webchat.errors import InvalidWebchatConfiguration
from plugin.webchat.session import session_bots
from plugin.liff.interface import LiffPlugin_Interface
from tests.plugin import test_webchat_runtime_e2e as fixture_module


PAGE = 'https://pages.example.test/menu/index.html'


class WebchatLiffTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_module.WebchatRuntimeE2ETest.setUpClass()
        from plugin.liff.richmenu import register_runtime
        register_runtime({})

    @classmethod
    def tearDownClass(cls):
        fixture_module.WebchatRuntimeE2ETest.tearDownClass()

    def setUp(self):
        params = {**fixture_module.WebchatRuntimeE2ETest.interface.params, 'liff_apps': {
            'menu': {'bot': 'menu', 'url': PAGE},
        }}
        self.interface = WebchatInterface('bot', params)
        menu_interface = WebchatInterface('menu', {**params, 'liff_apps': {}})
        self.root = fixture_module.WebchatRuntimeE2ETest.runtime.BotRuntime('bot', {'webchat': self.interface}, None)
        self.menu = fixture_module.WebchatRuntimeE2ETest.runtime.BotRuntime('menu', {
            'webchat': menu_interface,
            'liff': LiffPlugin_Interface('menu', {'allow_origin': '*'}),
        }, None)
        root_rows = [
            ['##line.follow', '@set', '$a', '1'],
            ['', '@button', '選択してください'], ['', '', '次へ', '次へ', '#next'],
            ['#next', '到着'],
            ['chain', '@set', '$a', '2'],
            ['', '@forward', 'menu', '##liff.sync'], ['', '@set', '$a', '3'],
            ['#after', '同期後:{$a}'],
            ['multi', '転送前'], ['', '@forward', 'menu', '##liff.sync'],
            ['loop', '@forward', 'menu', '##liff.loop'],
            ['outside', '@forward', 'other', '#next'],
            ['#record', '@set', '$recorded', 'true'], ['', '記録しました'],
        ]
        menu_rows = [
            ['##liff.bump', '@if', '$count'],
            ['', '@set', '$count', '$count + 1'], ['', '@else'],
            ['', '@set', '$count', '1'], ['', '@end'],
            ['', '{{"count":{$count}}}'],
            ['##liff.sync', '@set', '$synced', 'true'],
            ['', '@forward', 'bot', '#after'],
            ['##liff.loop', '@forward', 'bot', 'loop'],
            ['##liff.delay', '@delay', '1', '#next'],
            ['##liff.record', '@forward', 'bot', '#record', 'liff'],
            ['##liff.line', '@forward', 'bot', '#record', 'line'],
            ['##liff.otherchat', '@forward', 'menu', '#record', 'webchat'],
        ]
        for bot, rows in [(self.root, root_rows), (self.menu, menu_rows)]:
            bot.scenario = fixture_module.WebchatRuntimeE2ETest.scenario_module.ScenarioBuilder.build_from_table(rows, version=3)
            interface = bot.get_interface('webchat')
            interface._scenario_loaded = True
            bot.scenario_uri = interface.scenario_uri
        self.bots = {'bot': self.root, 'menu': self.menu}
        webapi.configure(self.bots.get)
        self.addCleanup(webapi.configure, lambda name: fixture_module.WebchatRuntimeE2ETest.bot if name == 'bot' else None)
        for target in ('context._get_player_status_class', 'task_client.create_task', 'requests.request'):
            patcher = patch(target, side_effect=AssertionError('外部状態やLINE APIは使用しません'))
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self):
        return fixture_module.WebchatRuntimeE2ETest._start().json

    def call(self, token, action='bump', **extra):
        return fixture_module.WebchatRuntimeE2ETest._turn({'type': 'liff', 'app': 'menu', 'app_url': PAGE,
                              'action': action, **extra}, token, expect_errors=True)

    def test_LIFF状態は別に保存され会話のplayerを変更しない(self):
        started = self.start()
        original = self.interface.load_state(started['state_token'])
        first = self.call(started['state_token']).json
        second = self.call(first['state_token']).json
        self.assertEqual(['{"count":1}'], first['liff_result'])
        self.assertEqual(['{"count":2}'], second['liff_result'])
        self.assertFalse(second['chat_updated'])
        self.assertEqual([], second['messages'])
        self.assertIsNone(second['echo_message'])
        payload = self.interface.load_state(second['state_token'])
        self.assertEqual(2, payload['v'])
        self.assertEqual(original['player'], payload['player'])
        self.assertEqual(2, payload['peer_players']['menu']['flags']['$count'])
        self.assertEqual(original['revision'] + 2, payload['revision'])
        self.assertEqual([{'id': 'menu', 'url': PAGE}], second['liff_apps'])
        choice = started['messages'][0]['actions'][0]['token']
        selected = fixture_module.WebchatRuntimeE2ETest._turn({'type': 'postback', 'postback_token': choice}, second['state_token'])
        self.assertEqual(200, selected.status_int)

    def test_forwardは現在Botの処理完了後に実行し他の保存を残す(self):
        first = self.call(self.start()['state_token']).json
        result = fixture_module.WebchatRuntimeE2ETest._turn({'type': 'text', 'text': 'chain'}, first['state_token']).json
        self.assertEqual(['同期後:3'], [message['text'] for message in result['messages']])
        payload = self.interface.load_state(result['state_token'])
        self.assertEqual(3, payload['player']['flags']['$a'])
        self.assertEqual(1, payload['peer_players']['menu']['flags']['$count'])
        self.assertTrue(payload['peer_players']['menu']['flags']['$synced'])
        self.assertEqual(first['state']['revision'] + 1, result['state']['revision'])

    def test_LIFFからAへのforwardは会話応答を返す(self):
        result = self.call(self.start()['state_token'], 'sync').json
        self.assertTrue(result['chat_updated'])
        self.assertEqual([], result['liff_result'])
        self.assertEqual('同期後:1', result['messages'][0]['text'])
        self.assertEqual([message['id'] for message in result['messages']], result['active_message_ids'])
        self.assertNotIn('active_response', result)

    def test_未使用Botのepoch変更と削除はセーブを無効にしない(self):
        started = self.start()
        self.assertEqual({}, self.interface.load_state(started['state_token'])['peer_epochs'])
        self.menu.get_interface('webchat').compatibility_epoch = 'new-epoch'
        result = fixture_module.WebchatRuntimeE2ETest._turn(
            {'type': 'text', 'text': 'hello'}, started['state_token']).json
        self.assertEqual({}, self.interface.load_state(result['state_token'])['peer_epochs'])
        self.interface.liff_apps = {}
        removed = fixture_module.WebchatRuntimeE2ETest._turn(
            {'type': 'text', 'text': 'hello'}, result['state_token']).json
        self.assertEqual(1, self.interface.load_state(removed['state_token'])['player']['flags']['$a'])

    def test_LIFF指定で会話Botの状態を更新し会話へは送らない(self):
        token = self.start()['state_token']
        self.assertEqual(422, self.call(token, 'record').status_int)
        self.root.interfaces['liff'] = LiffPlugin_Interface('bot', {'allow_origin': '*'})
        result = self.call(token, 'record').json
        self.assertEqual([], result['messages'])
        self.assertEqual(['記録しました'], result['liff_result'])
        self.assertTrue(self.interface.load_state(result['state_token'])['player']['flags']['$recorded'])
        for action in ('line', 'otherchat'):
            with self.subTest(action=action):
                self.assertEqual(422, self.call(result['state_token'], action).status_int)

    def test_複数の会話応答では最後の応答IDだけを有効にする(self):
        result = fixture_module.WebchatRuntimeE2ETest._turn(
            {'type': 'text', 'text': 'multi'}, self.start()['state_token']).json
        self.assertEqual(['転送前', '同期後:1'], [message['text'] for message in result['messages']])
        self.assertEqual([result['messages'][1]['id']], result['active_message_ids'])
        self.assertNotEqual(result['messages'][0]['id'], result['messages'][1]['id'])

    def test_新しい連携Botは未開始から追加し既存の進行を保つ(self):
        first = self.call(self.start()['state_token']).json
        runtime = fixture_module.WebchatRuntimeE2ETest.runtime
        controller = WebchatInterface('extra', self.menu.get_interface('webchat').params)
        controller._scenario_loaded = True
        extra = runtime.BotRuntime('extra', {
            'webchat': controller, 'liff': LiffPlugin_Interface('extra', {'allow_origin': '*'}),
        }, None)
        extra.scenario = self.menu.scenario
        extra.scenario_uri = controller.scenario_uri
        self.bots['extra'] = extra
        self.interface.liff_apps['extra'] = {'bot': 'extra', 'url': PAGE + '/extra'}
        added = self.call(first['state_token'], app='extra', app_url=PAGE + '/extra').json
        payload = self.interface.load_state(added['state_token'])
        self.assertEqual(1, payload['player']['flags']['$a'])
        self.assertEqual(1, payload['peer_players']['menu']['flags']['$count'])
        self.assertEqual(1, payload['peer_players']['extra']['flags']['$count'])
        self.assertEqual({'menu', 'extra'}, set(payload['peer_epochs']))

    def test_明示したnamespaceだけを共有する(self):
        self.menu.state_namespace = self.root.state_namespace
        result = self.call(self.start()['state_token']).json
        payload = self.interface.load_state(result['state_token'])
        self.assertEqual(1, payload['player']['flags']['$count'])
        self.assertEqual({}, payload['peer_players'])
        self.assertEqual({'menu': self.menu.get_interface('webchat').compatibility_epoch}, payload['peer_epochs'])
        self.menu.get_interface('webchat').compatibility_epoch = 'changed'
        self.assertEqual(409, self.call(result['state_token']).status_int)

    def test_旧単独Botのstateを拡張し登録外の状態は受け入れない(self):
        started = self.start()
        payload = self.interface.load_state(started['state_token'])
        payload.pop('peer_epochs')
        payload.pop('peer_players')
        payload['v'] = 1
        old = self.interface.codec.dump_state(payload)
        self.assertEqual(200, self.call(old).status_int)
        payload['peer_players'] = {'other': {}}
        malformed = self.interface.codec.dump_state(payload)
        self.assertEqual(401, self.call(malformed).status_int)

    def test_epoch変更と連携削除で古い保存を拒否する(self):
        token = self.call(self.start()['state_token']).json['state_token']
        self.menu.get_interface('webchat').compatibility_epoch = 'new-epoch'
        self.assertEqual(409, self.call(token).status_int)
        self.interface.liff_apps = {}
        result = fixture_module.WebchatRuntimeE2ETest._turn({'type': 'text', 'text': 'chain'}, token, expect_errors=True)
        self.assertEqual(409, result.status_int)

    def test_不正入力と登録外forwardとdelayを拒否する(self):
        token = self.start()['state_token']
        for extra in ({'app': 'other'}, {'app_url': 'https://other.example.test/'}, {'action': 1}):
            with self.subTest(extra=extra):
                self.assertEqual(400, self.call(token, **extra).status_int)
        for input_data in ({'type': 'text', 'text': 'outside'},
                           {'type': 'liff', 'app': 'menu', 'app_url': PAGE, 'action': 'delay'}):
            self.assertEqual(422, fixture_module.WebchatRuntimeE2ETest._turn(input_data, token, expect_errors=True).status_int)
        with self.assertRaises(InvalidWebchatConfiguration):
            session_bots(self.root, lambda _name: None)

    def test_forward循環は有限回で停止し新しいセーブを返さない(self):
        token = self.start()['state_token']
        with self.assertLogs(level='INFO'):
            result = fixture_module.WebchatRuntimeE2ETest._turn({'type': 'text', 'text': 'loop'}, token, expect_errors=True)
        self.assertEqual(422, result.status_int)
        self.assertNotIn('state_token', result.json)
        self.assertEqual(200, self.call(token).status_int)

    def test_追加許可されたdelayと非同期forwardも実APIで拒否する(self):
        original_import = builtins.__import__

        def reject_main(name, *args, **kwargs):
            if name == 'main':
                raise AssertionError('通常のDB／非同期実行環境を読み込んではいけません')
            return original_import(name, *args, **kwargs)

        token = self.start()['state_token']
        self.menu.get_interface('webchat').allowed_commands.update({'@delay', '@遅延'})
        with patch('builtins.__import__', side_effect=reject_main), \
                patch('task_client.create_task') as enqueue:
            for command in ('@delay', '@遅延'):
                self.menu.scenario = fixture_module.WebchatRuntimeE2ETest.scenario_module.ScenarioBuilder.build_from_table([
                    ['##liff.blocked', command, '1', 'menu', '#next', 'liff'],
                ], version=3)
                response = self.call(token, 'blocked')
                self.assertEqual(422, response.status_int)
                self.assertNotIn('state_token', response.json)

            self.interface.liff_apps = {}
            self.interface.allowed_commands.update({'@forward', '@転送'})
            for command in ('@forward', '@転送'):
                self.root.scenario = fixture_module.WebchatRuntimeE2ETest.scenario_module.ScenarioBuilder.build_from_table([
                    ['##line.follow', '開始'],
                    ['禁止', command, 'menu', '#next', 'liff'],
                ], version=3)
                standalone_token = self.start()['state_token']
                response = fixture_module.WebchatRuntimeE2ETest._turn(
                    {'type': 'text', 'text': '禁止'}, standalone_token, expect_errors=True)
                self.assertEqual(422, response.status_int)
                self.assertNotIn('state_token', response.json)
            enqueue.assert_not_called()

    def test_接続設定の不正URLと重複ページを起動前に拒否する(self):
        for url in ('http://pages.example.test/menu', 'javascript:alert(1)',
                    'https://name:secret@pages.example.test/menu', 'https://[broken'):
            with self.subTest(url=url), self.assertRaises(InvalidWebchatConfiguration):
                WebchatInterface('bot', {**self.interface.params, 'liff_apps': {
                    'menu': {'bot': 'menu', 'url': url},
                }})
        with self.assertRaises(InvalidWebchatConfiguration):
            WebchatInterface('bot', {**self.interface.params, 'liff_apps': {
                'first': {'bot': 'menu', 'url': PAGE},
                'second': {'bot': 'menu', 'url': PAGE + '?different=1'},
            }})

    def test_自己Botで状態とメニューを共有し入口だけ任意に無視する(self):
        from tests.test_richmenu_spec import menu_settings
        definitions = menu_settings()
        self.interface = WebchatInterface('bot', {**self.interface.params, 'liff_apps': {
            'menu': {'bot': 'bot', 'url': PAGE, 'match': 'prefix', 'liff_id': '123-example'},
        }, 'constants': {'menu_url': PAGE}}, bot_settings=definitions)
        self.interface._scenario_loaded = True
        self.root.interfaces['webchat'] = self.interface
        liff = LiffPlugin_Interface('bot', {'allow_origin': '*', 'ignore_unhandled_action': True})
        self.root.interfaces['liff'] = liff
        self.root.scenario = fixture_module.WebchatRuntimeE2ETest.scenario_module.ScenarioBuilder.build_from_table([
            ['##line.follow', '@set', '$word', '"月"'], ['', '開始'],
            ['##liff.status', '{$word}:{$$service_name}'],
            ['##liff.menu', '@richmenu', 'main'],
            ['##liff.broken', '#missing'],
            ['##error_invalid_label', '会話用エラー'],
        ], version=3)
        first = self.start()
        status = self.call(first['state_token'], 'status').json
        self.assertEqual(['月:liff'], status['liff_result'])
        self.assertEqual([], status['messages'])
        self.assertEqual([{'id': 'menu', 'url': PAGE, 'match': 'prefix', 'liff_id': '123-example'}], status['liff_apps'])
        menu = self.call(status['state_token'], 'menu').json
        self.assertEqual([], menu['liff_result'])
        self.assertEqual('main', self.interface.load_state(menu['state_token'])['player']['richmenu'])
        self.assertFalse(menu['chat_updated'])
        self.assertEqual([], self.call(menu['state_token'], 'not-defined').json['liff_result'])
        self.assertEqual(['会話用エラー'], self.call(menu['state_token'], 'broken').json['liff_result'])
        liff.ignore_unhandled_action = False
        self.assertEqual(['会話用エラー'], self.call(menu['state_token'], 'not-defined').json['liff_result'])

    def test_LIFF_IDの重複と不正matchを拒否する(self):
        for apps in (
                {'menu': {'bot': 'menu', 'url': PAGE, 'match': 'anything'}},
                {'menu': {'bot': 'menu', 'url': PAGE, 'liff_id': 'id/path'}},
                {'a': {'bot': 'menu', 'url': PAGE, 'liff_id': '123-id'},
                 'b': {'bot': 'menu', 'url': PAGE + '/other', 'liff_id': '123-id'}}):
            with self.subTest(apps=apps), self.assertRaises(InvalidWebchatConfiguration):
                WebchatInterface('bot', {**self.interface.params, 'liff_apps': apps})


if __name__ == '__main__':
    unittest.main()
