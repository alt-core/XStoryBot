import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cloud_backend import factory as backend_factory


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_common_commands():
    commands = types.ModuleType('commands')
    commands.Default_Builder = object
    commands.CommandEntry = Mock()
    commands.register_commands = Mock()

    expression = types.ModuleType('expression')
    expression.Expression = object

    pytz = types.ModuleType('pytz')
    pytz.timezone = lambda value: value

    stub_modules = {
        'requests': types.ModuleType('requests'),
        'pytz': pytz,
        'task_client': types.ModuleType('task_client'),
        'main': types.ModuleType('main'),
        'auth': types.ModuleType('auth'),
        'hub': types.ModuleType('hub'),
        'commands': commands,
        'utility': types.ModuleType('utility'),
        'users': types.ModuleType('users'),
        'expression': expression,
    }

    module_name = 'common_commands_for_isolation_test'
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / 'common_commands.py'
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stub_modules):
        spec.loader.exec_module(module)
    return module


def load_models(fake_db):
    google = types.ModuleType('google')
    cloud = types.ModuleType('google.cloud')
    firestore = types.ModuleType('google.cloud.firestore')
    firestore.Client = Mock(return_value=fake_db)
    firestore.transactional = lambda function: function
    cloud.firestore = firestore
    google.cloud = cloud

    utility = types.ModuleType('utility')
    utility.deep_dump = Mock()

    module_name = 'models_for_rollback_test'
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / 'models.py'
    )
    module = importlib.util.module_from_spec(spec)
    with (
        patch.object(backend_factory, '_provider', 'gcp'),
        patch.dict(sys.modules, {
            'google': google,
            'google.cloud': cloud,
            'google.cloud.firestore': firestore,
            'utility': utility,
        }),
    ):
        spec.loader.exec_module(module)
    return module


class RequestRuntimeIsolationTest(unittest.TestCase):
    def test_runtime_object_is_created_for_each_context(self):
        module = load_common_commands()
        runtime = module.CommonCommands_Runtime({
            'reset_keyword': 'リセット',
            'timezone': 'Asia/Tokyo',
        })
        first_context = SimpleNamespace(
            user='line:first-user',
            status=SimpleNamespace(scene='first-scene'),
        )
        second_context = SimpleNamespace(
            user='line:second-user',
            status=SimpleNamespace(scene='second-scene'),
        )

        first = runtime.get_runtime_object('common', first_context)
        second = runtime.get_runtime_object('common', second_context)

        self.assertIsNot(first, second)
        self.assertIs(first.context, first_context)
        self.assertIs(second.context, second_context)
        self.assertEqual(first.uid, 'line:first-user')
        self.assertEqual(first.scene, 'first-scene')
        self.assertEqual(second.uid, 'line:second-user')
        self.assertEqual(second.scene, 'second-scene')


class BuildResultIsolationTest(unittest.TestCase):
    def setUp(self):
        self.results = {}
        self.cache = types.ModuleType('build_cache')
        self.cache.clear = Mock(side_effect=self.results.clear)
        self.cache.set_cache = Mock(side_effect=(
            lambda key, value, sec=None: self.results.__setitem__(
                key, json.loads(value))))
        self.models = types.ModuleType('models')
        self.models.GlobalBotVariablesDB = Mock()
        self.scenario = types.ModuleType('scenario')
        self.scenario.ScenarioSyntaxError = type(
            'ScenarioSyntaxError', (Exception,), {})
        self.scenario.ScenarioBuilder = Mock()
        self.built = self.scenario.ScenarioBuilder.build_from_tables.return_value
        self.built.save_to_storage.return_value = 'scenario-reference'

        modules = patch.dict(sys.modules, {
            'build_cache': self.cache,
            'models': self.models,
            'scenario': self.scenario,
            'settings': types.ModuleType('settings'),
            'commands': types.ModuleType('commands'),
        })
        modules.start()
        self.addCleanup(modules.stop)
        spec = importlib.util.spec_from_file_location(
            'runtime_for_build_result_test', PROJECT_ROOT / 'runtime.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.loader = Mock()
        self.loader.load_scenario.return_value = ([], {})
        self.bot_a = self.module.BotRuntime('bot-a', {}, self.loader)
        self.bot_b = self.module.BotRuntime('bot-b', {}, self.loader)

    def test_別Botの成功結果を残して同じBotだけを上書きする(self):
        for bot, task_id in (
            (self.bot_a, 'build-a-1'),
            (self.bot_b, 'build-b-1'),
            (self.bot_a, 'build-a-2'),
        ):
            self.assertEqual(bot.build_scenario(task_id), (True, None))

        self.assertEqual(set(self.results), {
            'last_build_result:bot-a', 'last_build_result:bot-b'})
        self.assertEqual(self.results['last_build_result:bot-a']['task_id'], 'build-a-2')
        self.assertEqual(self.results['last_build_result:bot-b']['task_id'], 'build-b-1')
        self.assertTrue(all(value['status'] == 'Success' for value in self.results.values()))
        self.cache.clear.assert_not_called()
        self.assertEqual(self.built.save_to_storage.call_count, 3)
        self.assertEqual(self.models.GlobalBotVariablesDB.save.call_count, 3)
        self.models.GlobalBotVariablesDB.save.assert_called_with(
            'bot-a', 'scenario-reference')
        self.assertTrue(all(
            kwargs['sec'] == 60 * 60 * 24
            for _, kwargs in self.cache.set_cache.call_args_list))

    def test_ビルド失敗は対象Botの結果だけを更新する(self):
        self.bot_a.build_scenario('build-a')
        self.bot_b.build_scenario('build-b')
        other_result = dict(self.results['last_build_result:bot-b'])
        for error in (ValueError('構文エラー'), RuntimeError('取得失敗')):
            with self.subTest(error_type=type(error).__name__):
                self.loader.load_scenario.side_effect = error
                with self.assertLogs(level='ERROR'):
                    result = self.bot_a.build_scenario('failed-a')

                self.assertEqual(result, (False, str(error)))
                self.assertEqual(self.results['last_build_result:bot-a']['status'], 'Failure')
                self.assertEqual(self.results['last_build_result:bot-a']['task_id'], 'failed-a')
                self.assertEqual(self.results['last_build_result:bot-a']['error'], str(error))
                self.assertEqual(self.results['last_build_result:bot-b'], other_result)

        self.cache.clear.assert_not_called()
        self.assertEqual(self.models.GlobalBotVariablesDB.save.call_count, 2)


class RollbackSnapshotTest(unittest.TestCase):
    def test_nested_scene_history_is_restored(self):
        snapshot = Mock()
        snapshot.exists = True
        snapshot.update_time = object()
        snapshot.to_dict.return_value = {
            'scene': 'scene-1',
            'scene_history': ['scene-0'],
            'action_token': 'token',
            'value': '{}',
        }
        document = Mock()
        document.get.return_value = snapshot
        fake_db = Mock()
        fake_db.collection.return_value.document.return_value = document
        module = load_models(fake_db)
        status = module.PlayerStatusDB('bot', 'line:user-1')
        status.save = Mock()

        status.entry['scene_history'].append('scene-2')
        status.rollback()

        self.assertEqual(status.entry['scene_history'], ['scene-0'])
        status.entry['scene_history'].append('scene-3')
        status.rollback()

        self.assertEqual(status.entry['scene_history'], ['scene-0'])
        self.assertEqual(status.save.call_count, 2)
        status.save.assert_called_with(force=True)

    def test_namespaceを含むPlayerStatus文書IDを使う(self):
        snapshot = Mock()
        snapshot.exists = False
        document = Mock()
        document.get.return_value = snapshot
        collection = Mock()
        collection.document.return_value = document
        fake_db = Mock()
        fake_db.collection.return_value = collection
        module = load_models(fake_db)

        status = module.PlayerStatusDB('shared', 'line:user-1')

        self.assertEqual(status.id, 'shared:line:user-1')
        fake_db.collection.assert_called_once_with('player_status')
        collection.document.assert_called_once_with('shared:line:user-1')


if __name__ == '__main__':
    unittest.main()
