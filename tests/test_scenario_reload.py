import importlib.util
from pathlib import Path
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class ScenarioReloadTest(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.settings = SimpleNamespace(OPTIONS={}, CONSTANTS={}, BOTS={})
        self.store = SimpleNamespace(get_global_bot_variables=Mock(
            return_value={'scenario_uri': 'first', 'generation': 1}))
        self.variables = SimpleNamespace(save=Mock(return_value=2))
        self.loaded = SimpleNamespace(version=3)
        self.scenario = SimpleNamespace(
            Scenario=SimpleNamespace(load_from_uri=Mock(return_value=self.loaded)),
            ScenarioSyntaxError=type('ScenarioSyntaxError', (Exception,), {}),
            ScenarioBuilder=SimpleNamespace(build_from_tables=Mock()))
        self.cache = SimpleNamespace(set_cache=Mock())
        modules = {
            'settings': self.settings, 'commands': SimpleNamespace(),
            'task_client': SimpleNamespace(), 'models': SimpleNamespace(GlobalBotVariablesDB=self.variables),
            'scenario': self.scenario, 'build_cache': self.cache,
        }
        patcher = patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        spec = importlib.util.spec_from_file_location(
            '_runtime_reload', Path(__file__).resolve().parents[1] / 'runtime.py')
        self.runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runtime)
        clock = patch.object(self.runtime.time, 'monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.loader = Mock()
        self.bot = self.runtime.BotRuntime('bot', {}, self.loader)
        self.bot._scenario_state_store = self.store

    def test_既定10秒は旧セーブ集中でもURI確認を繰り返さない(self):
        self.bot.select_scenario()
        self.store.get_global_bot_variables.return_value = {'scenario_uri': 'next', 'generation': 2}
        self.now = 9
        for _ in range(100):
            self.assertEqual(1, self.bot.select_scenario(min_generation=0).generation)
        self.assertEqual(1, self.store.get_global_bot_variables.call_count)
        self.now = 10
        self.assertEqual(2, self.bot.select_scenario().generation)
        for _ in range(100):
            self.assertEqual(2, self.bot.select_scenario(min_generation=1).generation)
        self.assertEqual(2, self.store.get_global_bot_variables.call_count)

    def test_新版保存だけがTTL内でも再確認する(self):
        self.bot.select_scenario()
        self.store.get_global_bot_variables.return_value = {'scenario_uri': 'next', 'generation': 2}
        self.assertEqual(2, self.bot.select_scenario(min_generation=2).generation)
        self.assertEqual(2, self.store.get_global_bot_variables.call_count)
        self.assertEqual(2, self.scenario.Scenario.load_from_uri.call_count)

    def test_forceと同URI再公開はpointerだけ再確認し本体を再利用する(self):
        first = self.bot.select_scenario()
        self.store.get_global_bot_variables.return_value = {'scenario_uri': 'first', 'generation': 2}
        self.bot.load_scenario()
        second = self.bot.select_scenario()
        self.assertIs(first.scenario, second.scenario)
        self.assertEqual((1, 2), (first.generation, second.generation))
        self.assertFalse(hasattr(self.loaded, 'generation'))
        self.assertEqual(2, self.store.get_global_bot_variables.call_count)
        self.scenario.Scenario.load_from_uri.assert_called_once()

    def test_失敗も10秒再利用し本体と世代を成功扱いで更新しない(self):
        first = self.bot.select_scenario()
        self.store.get_global_bot_variables.return_value = {'scenario_uri': 'next', 'generation': 2}
        self.scenario.Scenario.load_from_uri.side_effect = RuntimeError('本体の障害')
        self.now = 10
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, '本体の障害'):
                self.bot.select_scenario(min_generation=2)
        self.assertIs(first, self.bot._latest_selection)
        self.assertEqual(2, self.store.get_global_bot_variables.call_count)
        self.assertIsNone(self.bot._scenario_check_error.__traceback__)
        self.scenario.Scenario.load_from_uri.side_effect = None
        self.now = 20
        self.assertEqual(2, self.bot.select_scenario().generation)

    def test_世代欠損や逆転を旧版の成功へ変えない(self):
        self.bot.select_scenario()
        for record in (None, {'scenario_uri': 'new'},
                       {'scenario_uri': 'new', 'generation': 0},
                       {'scenario_uri': 'new', 'generation': True},
                       {'scenario_uri': 'new', 'generation': 1}):
            with self.subTest(record=record), self.assertRaises(ValueError):
                self.store.get_global_bot_variables.return_value = record
                self.bot.select_scenario(min_generation=2, force=True)

    def test_取得障害後も未確認の新版保存だけ再確認し同世代では繰り返さない(self):
        self.bot.select_scenario()
        self.now = 10
        self.store.get_global_bot_variables.side_effect = ConnectionError('pointerの障害')
        with self.assertRaises(ConnectionError):
            self.bot.select_scenario(min_generation=1)
        for _ in range(10):
            with self.assertRaises(ConnectionError):
                self.bot.select_scenario(min_generation=1)
        self.assertEqual(2, self.store.get_global_bot_variables.call_count)
        with self.assertRaises(ConnectionError):
            self.bot.select_scenario(min_generation=2)
        for _ in range(10):
            with self.assertRaises(ConnectionError):
                self.bot.select_scenario(min_generation=2)
        self.assertEqual(3, self.store.get_global_bot_variables.call_count)
        self.store.get_global_bot_variables.side_effect = None
        self.store.get_global_bot_variables.return_value = {'scenario_uri': 'next', 'generation': 3}
        self.assertEqual(3, self.bot.select_scenario(min_generation=3).generation)
        self.assertEqual(4, self.store.get_global_bot_variables.call_count)

    def test_読めた新版pointerの本体取得失敗は同世代保存で再取得しない(self):
        first = self.bot.select_scenario()
        self.now = 10
        self.store.get_global_bot_variables.return_value = {'scenario_uri': 'next', 'generation': 2}
        self.scenario.Scenario.load_from_uri.side_effect = ConnectionError('本体の障害')
        with self.assertRaises(ConnectionError):
            self.bot.select_scenario(min_generation=1)
        for _ in range(10):
            with self.assertRaises(ConnectionError):
                self.bot.select_scenario(min_generation=2)
        self.assertIs(first, self.bot._latest_selection)
        self.assertEqual(2, self.store.get_global_bot_variables.call_count)
        self.assertEqual(2, self.scenario.Scenario.load_from_uri.call_count)

    def test_固定URIと追従を同じBotで使っても最新cacheを失わない(self):
        latest = self.bot.select_scenario()
        fixed = self.bot.select_scenario('pinned')
        self.assertIsNone(fixed.generation)
        self.assertIs(latest, self.bot.select_scenario())
        self.assertEqual('first', self.bot.scenario_uri)
        self.assertEqual(1, self.store.get_global_bot_variables.call_count)
        self.assertEqual(2, self.scenario.Scenario.load_from_uri.call_count)

    def test_固定URIだけならDB初期化も確認もしない(self):
        bot = self.runtime.BotRuntime('bot', {}, self.loader)
        with patch('cloud_backend.create_state_store') as factory:
            selected = bot.select_scenario('fixed')
        self.assertIsNone(selected.generation)
        factory.assert_not_called()

    def test_同process公開は自分のURIと返された世代を採用し逆転しない(self):
        self.bot.select_scenario()
        built = SimpleNamespace(save_to_storage=Mock(return_value='built'))
        self.scenario.ScenarioBuilder.build_from_tables.return_value = built
        self.loader.load_scenario.return_value = ([], {})
        self.assertEqual((True, None), self.bot.build_scenario())
        selected = self.bot.select_scenario()
        self.assertEqual(('built', 2), (selected.uri, selected.generation))
        self.assertIs(built, selected.scenario)
        self.variables.save.return_value = 1
        self.assertEqual((True, None), self.bot.build_scenario())
        self.assertIs(selected, self.bot.select_scenario())

    def test_0指定は毎回確認し不正な間隔を拒否する(self):
        self.settings.OPTIONS = {'scenario_check_interval': 0}
        bot = self.runtime.BotRuntime('bot', {}, self.loader)
        bot._scenario_state_store = self.store
        bot.check_reload()
        bot.check_reload()
        self.assertEqual(2, self.store.get_global_bot_variables.call_count)
        for value in (-1, float('nan'), float('inf'), None, 'invalid'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.settings.OPTIONS = {'scenario_check_interval': value}
                self.runtime.BotRuntime('bot', {}, self.loader)

    def test_新版保存の並行要求はロック後にも条件を確認する(self):
        self.bot.select_scenario()
        started, release = threading.Event(), threading.Event()

        def read(_):
            started.set()
            if not release.wait(1):
                raise AssertionError('確認の解除が届きません')
            return {'scenario_uri': 'next', 'generation': 2}
        self.store.get_global_bot_variables.side_effect = read
        with ThreadPoolExecutor(max_workers=4) as executor:
            first = executor.submit(self.bot.select_scenario, min_generation=2)
            self.assertTrue(started.wait(1))
            rest = [executor.submit(self.bot.select_scenario, min_generation=2) for _ in range(3)]
            release.set()
            results = [future.result() for future in [first, *rest]]
        self.assertTrue(all(selected.generation == 2 for selected in results))
        self.assertEqual(2, self.store.get_global_bot_variables.call_count)


if __name__ == '__main__':
    unittest.main()
