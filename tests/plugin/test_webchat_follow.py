"""最新pointerと実Scenarioを使い、要求内の版と署名セーブを確認する。"""

from collections import Counter
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import commands
import hub
from plugin.webchat import webapi
from plugin.webchat.interface import WebchatInterface
from plugin.webchat.errors import InvalidWebchatConfiguration
from tests.plugin import test_webchat_runtime_e2e as fixture
from tests.plugin import test_webchat_liff as liff_fixture


class WebchatFollowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.WebchatRuntimeE2ETest.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixture.WebchatRuntimeE2ETest.tearDownClass()

    def setUp(self):
        self.first_uri = 's3://private/scenario/' + 'a' * 32
        self.next_uri = 's3://private/scenario/' + 'b' * 32
        self.latest = {'bot': {'scenario_uri': self.first_uri, 'generation': 1}}
        self.store = Mock()
        self.store.get_global_bot_variables.side_effect = lambda name: self.latest.get(name)
        factory = patch('cloud_backend.create_state_store', return_value=self.store)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.scenario = fixture.WebchatRuntimeE2ETest.scenario_module
        modules = patch.dict(sys.modules, {'scenario': self.scenario})
        modules.start()
        self.addCleanup(modules.stop)
        self.first = self._build('旧版')
        self.next = self._build('新版')
        self.artifacts = {self.first_uri: self.first, self.next_uri: self.next}
        loader = patch.object(self.scenario.Scenario, 'load_from_uri', side_effect=lambda uri: self.artifacts[uri])
        self.load = loader.start()
        self.addCleanup(loader.stop)
        self.now = 0
        clock = patch.object(fixture.WebchatRuntimeE2ETest.runtime.time, 'monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.interface = self._interface(True)
        self.bot = self._bot(self.interface)
        webapi.configure(lambda name: self.bot if name == 'bot' else None)
        self.addCleanup(webapi.configure, fixture.WebchatRuntimeE2ETest._webapi_callback)

    def _build(self, text):
        return self.scenario.ScenarioBuilder.build_from_table([
            ['##line.follow', '@button', text], ['', '', '次へ', '次へ', '#next'],
            ['#next', text], ['show', text],
        ], version=3)

    def _interface(self, follow):
        return WebchatInterface('bot', {
            **fixture.WebchatRuntimeE2ETest.interface.params,
            'follow_latest_scenario': follow,
            'scenario_uri': '' if follow else self.first_uri,
        })

    def _bot(self, interface):
        bot = fixture.WebchatRuntimeE2ETest.runtime.BotRuntime('bot', {'webchat': interface}, None)
        bot.scenario_check_interval = 10
        return bot

    def _start(self):
        return fixture.WebchatRuntimeE2ETest._start()

    def _turn(self, token, input_data):
        return fixture.WebchatRuntimeE2ETest._turn(input_data, token, expect_errors=True)

    def test_固定方式はDBを初期化せず本体を再利用する(self):
        self.interface = self._interface(False)
        self.bot = self._bot(self.interface)
        started = self._start().json
        result = self._turn(started['state_token'], {'type': 'text', 'text': 'show'})
        self.assertEqual(200, result.status_int)
        self.factory.assert_not_called()
        self.load.assert_called_once_with(self.first_uri)

    def test_追従設定のtypoを固定方式として黙って扱わない(self):
        for value in ('treu', 1, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(InvalidWebchatConfiguration, 'follow_latest_scenario'):
                WebchatInterface('bot', {
                    **self.interface.params, 'scenario_uri': self.first_uri,
                    'follow_latest_scenario': value,
                })

    def test_専用appは固定URI不要の追従設定でもDB未初期化で起動する(self):
        params = dict(self.interface.params)
        settings = SimpleNamespace(
            CLOUD_SETTINGS={'provider': 'aws'}, DEPLOY_ENV='test', BACKEND_SETTINGS={},
            OPTIONS={'reset_keyword': '!reset', 'timezone': 'Asia/Tokyo'}, CONSTANTS={},
            PLUGINS={'webchat': params},
            BOTS={'bot': {'interfaces': [{'type': 'webchat', 'params': {}}]}},
        )
        spec = importlib.util.spec_from_file_location(
            '_webchat_follow_native', Path(__file__).resolve().parents[2] / 'app_webchat.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
                'settings': settings, 'runtime': fixture.WebchatRuntimeE2ETest.runtime,
                'log_config': SimpleNamespace(configure=Mock())}), \
                patch.multiple(commands, catalog=[], catalog_map={}, object_catalog=[], object_catalog_map={}), \
                patch.multiple(hub, builder_list=[], runtime_list=[], method_cache={}), \
                patch.object(webapi, '_get_bot_callback'):
            spec.loader.exec_module(module)
            self.assertTrue(module.get_bot('bot').get_interface('webchat').follow_latest_scenario)
        self.factory.assert_not_called()

    def test_追従は共通10秒cacheでURI変更時だけ本体を読む(self):
        started = self._start().json
        state = started['state_token']
        same = self._turn(state, {'type': 'text', 'text': 'show'}).json
        self.load.assert_called_once_with(self.first_uri)
        self.latest['bot'] = {'scenario_uri': self.next_uri, 'generation': 2}
        self.now = 9
        older = self._turn(same['state_token'], {'type': 'text', 'text': 'show'}).json
        self.assertEqual('旧版', older['messages'][0]['text'])
        self.now = 10
        updated = self._turn(older['state_token'], {'type': 'text', 'text': 'show'}).json
        self.assertEqual('新版', updated['messages'][0]['text'])
        self.assertEqual('b' * 32, self.interface.load_state(updated['state_token'])['scenario_revision'])
        self.assertEqual(2, self.store.get_global_bot_variables.call_count)
        self.assertEqual(2, self.load.call_count)
        self.factory.assert_called_once()

    def test_旧セーブが新版cacheへ集中してもDB再確認に戻らない(self):
        old = self._start().json['state_token']
        self.latest['bot'] = {'scenario_uri': self.next_uri, 'generation': 2}
        self.now = 10
        self._turn(old, {'type': 'text', 'text': 'show'})
        self.store.get_global_bot_variables.reset_mock()
        self.now = 11
        for _ in range(10):
            result = self._turn(old, {'type': 'text', 'text': 'show'}).json
            self.assertEqual('新版', result['messages'][0]['text'])
        self.store.get_global_bot_variables.assert_not_called()

    def test_他processで新版を使ったセーブは旧cacheを期限前に更新する(self):
        self._start()
        self.latest['bot'] = {'scenario_uri': self.next_uri, 'generation': 2}
        other = self._bot(self.interface)
        webapi.configure(lambda name: other if name == 'bot' else None)
        newer = self._start().json['state_token']
        webapi.configure(lambda name: self.bot if name == 'bot' else None)
        self.store.get_global_bot_variables.reset_mock()
        self.now = 1
        result = self._turn(newer, {'type': 'text', 'text': 'show'}).json
        self.assertEqual('新版', result['messages'][0]['text'])
        self.assertEqual({'bot': 2}, self.interface.load_state(result['state_token'])['player']['scenario_generations'])
        self.store.get_global_bot_variables.assert_called_once_with('bot')

    def test_世代のない既存セーブと固定で進んだセーブは追従前にリセットを求める(self):
        state = self._start().json['state_token']
        for generations in ('missing', {'bot': None}):
            payload = self.interface.load_state(state)
            if generations == 'missing':
                del payload['player']['scenario_generations']
            else:
                payload['player']['scenario_generations'] = generations
            unknown = self.interface.codec.dump_state(payload)
            self.store.get_global_bot_variables.reset_mock()
            result = self._turn(unknown, {'type': 'text', 'text': 'show'})
            self.assertEqual(409, result.status_int)
            self.assertNotIn('state_token', result.json)
            self.store.get_global_bot_variables.assert_not_called()

    def test_取得失敗は旧版へfallbackせずセーブを更新しない(self):
        state = self._start().json['state_token']
        original = self.interface.load_state(state)
        for fail_pointer in (True, False):
            with self.subTest(fail_pointer=fail_pointer):
                self.now += 10
                self.store.get_global_bot_variables.side_effect = RuntimeError('資格値を含み得る障害') if fail_pointer else None
                self.store.get_global_bot_variables.return_value = {'scenario_uri': 'missing', 'generation': 2}
                with self.assertLogs(level='ERROR') as logs:
                    result = self._turn(state, {'type': 'text', 'text': 'show'})
                self.assertEqual(503, result.status_int)
                self.assertNotIn('state_token', result.json)
                self.assertEqual(original, self.interface.load_state(state))
                self.assertNotIn('資格値を含み得る障害', '\n'.join(logs.output))

    def test_途中で共有Botの版が変わっても本文tokenログは選択した版を使う(self):
        execute = self.bot.handle_action
        def change(context, **kwargs):
            self.bot.scenario, self.bot.scenario_uri = self.next, self.next_uri
            return execute(context, **kwargs)
        with patch.object(self.bot, 'handle_action', side_effect=change), self.assertLogs(level='INFO') as captured:
            started = self._start().json
        payload = self.interface.load_state(started['state_token'])
        self.assertEqual('a' * 32, payload['scenario_revision'])
        self.assertEqual('旧版', started['messages'][0]['text'])
        choice = started['messages'][0]['actions'][0]['token']
        self.assertEqual('a' * 32, self.interface.load_postback(choice, payload)['scenario_revision'])
        self.assertEqual(1, self.interface.load_postback(choice, payload)['scenario_generation'])
        self.assertEqual({'bot': 1}, payload['player']['scenario_generations'])
        records = [json.loads(record.getMessage()) for record in captured.records if record.getMessage().startswith('{')]
        self.assertTrue(any(record.get('current_scenario_revision') == 'a' * 32 for record in records))
        self.assertTrue(any(record.get('event') == 'turn-completed' and record.get('scenario_revision') == 'a' * 32 for record in records))

    def test_同期forwardは同じBotの要求内の版を再利用する(self):
        case = liff_fixture.WebchatLiffTest()
        case.setUp()
        self.addCleanup(case.doCleanups)
        root_uri = self.first_uri
        peer_uri = 's3://private/scenario/' + 'c' * 32
        self.artifacts[root_uri], self.artifacts[peer_uri] = case.root.scenario, case.menu.scenario
        self.latest.update(bot={'scenario_uri': root_uri, 'generation': 1},
                           menu={'scenario_uri': peer_uri, 'generation': 1})
        for bot in (case.root, case.menu):
            bot.get_interface('webchat').follow_latest_scenario = True
            bot.scenario = bot.scenario_uri = None
        started = case.start()
        self.now = 10
        self.store.get_global_bot_variables.reset_mock()
        execute = case.root.handle_action
        def change(context, **kwargs):
            result = execute(context, **kwargs)
            if context.action == 'chain':
                self.latest['bot'] = {'scenario_uri': self.next_uri, 'generation': 2}
            return result
        with patch.object(case.root, 'handle_action', side_effect=change):
            result = fixture.WebchatRuntimeE2ETest._turn(
                {'type': 'text', 'text': 'chain'}, started['state_token'], expect_errors=True)
        self.assertEqual(200, result.status_int)
        self.assertEqual('同期後:3', result.json['messages'][0]['text'])
        self.assertEqual(Counter(bot=1, menu=1), Counter(call.args[0] for call in self.store.get_global_bot_variables.call_args_list))
        self.assertEqual('a' * 32, case.interface.load_state(result.json['state_token'])['scenario_revision'])

    def test_連携Botはnamespace共有時も自分の保存世代で旧cacheを更新する(self):
        for shared in (False, True):
            with self.subTest(shared=shared):
                case = liff_fixture.WebchatLiffTest()
                case.setUp()
                try:
                    if shared:
                        case.menu.state_namespace = case.root.state_namespace
                    first_peer = 's3://private/scenario/' + 'c' * 32
                    next_peer = 's3://private/scenario/' + 'd' * 32
                    self.artifacts[self.first_uri] = case.root.scenario
                    self.artifacts[first_peer] = case.menu.scenario
                    self.artifacts[next_peer] = self.scenario.ScenarioBuilder.build_from_table([
                        ['##liff.bump', '新しいメニュー'],
                    ], version=3)
                    self.latest.update(bot={'scenario_uri': self.first_uri, 'generation': 1},
                                       menu={'scenario_uri': first_peer, 'generation': 1})
                    for bot in (case.root, case.menu):
                        bot.get_interface('webchat').follow_latest_scenario = True
                        bot.scenario = bot.scenario_uri = None
                    started = case.start()
                    state = case.call(started['state_token']).json['state_token']
                    payload = case.interface.load_state(state)
                    player = payload['player'] if shared else payload['peer_players']['menu']
                    player['scenario_generations']['menu'] = 2
                    self.latest['menu'] = {'scenario_uri': next_peer, 'generation': 2}
                    self.store.get_global_bot_variables.reset_mock()
                    result = case.call(case.interface.codec.dump_state(payload)).json
                    self.assertEqual(['新しいメニュー'], result['liff_result'])
                    self.store.get_global_bot_variables.assert_called_once_with('menu')
                    saved = case.interface.load_state(result['state_token'])
                    player = saved['player'] if shared else saved['peer_players']['menu']
                    self.assertEqual(2, player['scenario_generations']['menu'])
                    self.assertEqual(1, saved['player']['scenario_generations']['bot'])
                finally:
                    case.doCleanups()

    def test_同期forward途中の取得失敗と実行不具合は全Botのセーブを返さない(self):
        for loader_failure in (True, False):
            with self.subTest(loader_failure=loader_failure):
                case = liff_fixture.WebchatLiffTest()
                case.setUp()
                try:
                    peer_uri = 's3://private/scenario/' + 'c' * 32
                    repaired_uri = 's3://private/scenario/' + 'd' * 32
                    working_peer = case.menu.scenario
                    self.artifacts[self.first_uri] = case.root.scenario
                    self.artifacts[repaired_uri] = working_peer
                    self.artifacts.pop(peer_uri, None)
                    if not loader_failure:
                        self.artifacts[peer_uri] = self.scenario.ScenarioBuilder.build_from_table([
                            ['##liff.sync', '@set', '$partial', 'true'],
                            ['', '@raise', '台本の実行不具合'],
                        ], version=3)
                    self.latest.update(bot={'scenario_uri': self.first_uri, 'generation': 1},
                                       menu={'scenario_uri': peer_uri, 'generation': 1})
                    for bot in (case.root, case.menu):
                        bot.get_interface('webchat').follow_latest_scenario = True
                        bot.scenario = bot.scenario_uri = None
                    case.menu.get_interface('webchat').allowed_commands.add('@raise')
                    started = case.start()
                    token = started['state_token']
                    original = case.interface.load_state(token)
                    with self.assertLogs(level='ERROR'):
                        failed = fixture.WebchatRuntimeE2ETest._turn(
                            {'type': 'text', 'text': 'chain'}, token, expect_errors=True)
                    self.assertEqual(503 if loader_failure else 500, failed.status_int)
                    self.assertNotIn('state_token', failed.json)
                    self.assertNotIn('messages', failed.json)
                    self.assertEqual(original, case.interface.load_state(token))
                    self.assertEqual(1, original['player']['flags']['$a'])
                    self.assertEqual({}, original['peer_players'])

                    self.latest['menu'] = {'scenario_uri': repaired_uri, 'generation': 2}
                    self.now += 10
                    repaired = fixture.WebchatRuntimeE2ETest._turn(
                        {'type': 'text', 'text': 'chain'}, token, expect_errors=True)
                    self.assertEqual(200, repaired.status_int)
                    self.assertEqual('同期後:3', repaired.json['messages'][0]['text'])
                    saved = case.interface.load_state(repaired.json['state_token'])
                    self.assertEqual(3, saved['player']['flags']['$a'])
                    self.assertTrue(saved['peer_players']['menu']['flags']['$synced'])
                    self.assertNotIn('$partial', saved['peer_players']['menu']['flags'])
                finally:
                    case.doCleanups()
