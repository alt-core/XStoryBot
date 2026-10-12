import datetime
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cloud_backend import factory as backend_factory
from cloud_backend.contracts import RollbackConflictError, RollbackError, StateStoreError
from cloud_backend.local.state_store import LocalStateStore
from scenario_selection import stamp_scenario_generation


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_common_commands():
    commands = types.ModuleType('commands')
    commands.Default_Builder = object
    commands.CommandEntry = Mock()
    commands.register_commands = Mock()

    expression = types.ModuleType('expression')
    expression.Expression = object

    utility = types.ModuleType('utility')
    utility.timezone = lambda name: datetime.timezone.utc

    stub_modules = {
        'requests': types.ModuleType('requests'),
        'task_client': types.ModuleType('task_client'),
        'main': types.ModuleType('main'),
        'auth': types.ModuleType('auth'),
        'hub': types.ModuleType('hub'),
        'commands': commands,
        'utility': utility,
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
    utility.timezone = lambda name: datetime.timezone.utc

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
        self.models.GlobalBotVariablesDB.save.return_value = 1
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
            'settings': types.SimpleNamespace(CONSTANTS={}),
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
        document.update.return_value = SimpleNamespace(
            update_time=datetime.datetime(2026, 10, 12, tzinfo=datetime.timezone.utc))
        fake_db = Mock()
        fake_db.collection.return_value.document.return_value = document
        module = load_models(fake_db)
        status = module.PlayerStatusDB('bot', 'line:user-1')

        status.entry['scene_history'].append('scene-2')
        status.scene = 'scene-2'
        status.save()
        document.update.reset_mock()
        status.rollback()

        self.assertEqual(status.entry['scene_history'], ['scene-0'])
        status.entry['scene_history'].append('scene-3')
        status.rollback()

        self.assertEqual(status.entry['scene_history'], ['scene-0'])
        self.assertEqual(document.update.call_count, 1)
        document.set.assert_not_called()

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


class RollbackConcurrencyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = LocalStateStore(directory.name)
        spec = importlib.util.spec_from_file_location(
            'models_for_concurrent_rollback_test', PROJECT_ROOT / 'models.py')
        self.models = importlib.util.module_from_spec(spec)
        with patch('cloud_backend.create_state_store', return_value=self.store):
            spec.loader.exec_module(self.models)

    def _player(self):
        return self.models.PlayerStatusDB('shared', 'line:user,U1')

    def _seed(self):
        status = self._player()
        status.scene = 'before'
        status['$count'] = 1
        status.save()

    def test_公開保存の戻り値を呼出し元へ返す(self):
        with patch.object(self.store, 'save_global_bot_variables', return_value=7) as publish:
            self.assertEqual(7, self.models.GlobalBotVariablesDB.save('bot', 'scenario-reference'))
        publish.assert_called_once_with('bot', 'scenario-reference')

    def test_同じ公開世代の記録だけなら保存versionとDB書込みを増やさない(self):
        seeded = self._player()
        stamp_scenario_generation(seeded, 'bot_a', 3)
        seeded.save()
        before = self.store.load_player_status(seeded.id)
        unchanged = self._player()
        with patch.object(self.store, 'update_player_status', wraps=self.store.update_player_status) as update:
            stamp_scenario_generation(unchanged, 'bot_a', 3)
            unchanged.save()
        update.assert_not_called()
        self.assertEqual(before, self.store.load_player_status(seeded.id))

    def test_新しい公開世代の記録は保存し巻戻しで前の世代へ戻す(self):
        seeded = self._player()
        seeded.scenario_generations = {'bot_a': 3, 'bot_b': 7}
        seeded.save()
        before = self.store.load_player_status(seeded.id)
        changed = self._player()
        stamp_scenario_generation(changed, 'bot_a', 4)
        changed.save()
        after = self.store.load_player_status(changed.id)
        self.assertNotEqual(before.version, after.version)
        self.assertEqual({'bot_a': 4, 'bot_b': 7}, after.data['scenario_generations'])
        changed.rollback()
        self.assertEqual(before.data, self.store.load_player_status(changed.id).data)

    def test_世代metadataはflagsと分離し共有namespaceのBot別情報を保存する(self):
        status = self._player()
        self.assertEqual({}, status.scenario_generations)
        generations = {'bot_a': 4, 'bot_b': 7, 'fixed': None}
        status.scenario_generations = generations
        generations['bot_a'] = 99
        self.assertEqual({'bot_a': 4, 'bot_b': 7, 'fixed': None}, status.scenario_generations)
        self.assertNotIn('scenario_generations', status.keys())
        status.save()
        stored = self.store.load_player_status(status.id)
        self.assertEqual('{}', stored.data['value'])
        self.assertEqual(status.scenario_generations, self._player().scenario_generations)

    def test_世代だけの変更も保存し古い保存の欠落を0に変換しない(self):
        self.store.force_put_player_status('shared:line:user,U1', {
            'scene': 'old', 'action_token': 'old-action', 'value': '{"$flag": 1}',
        })
        status = self._player()
        self.assertIsNone(status.scenario_generations)
        status['$flag'] = 2
        status.save()
        self.assertIsNone(self._player().scenario_generations)
        previous_version = status.last_update_time
        status.scenario_generations = {'bot_a': None}
        status.save()
        self.assertNotEqual(previous_version, status.last_update_time)
        self.assertEqual({'bot_a': None}, self._player().scenario_generations)
        self.assertEqual(2, self._player()['$flag'])

    def test_保存済みmetadataの巻戻しと明示resetを維持する(self):
        status = self._player()
        status.scenario_generations = {'bot_a': 1}
        status.save()
        changed = self._player()
        changed.scenario_generations = {'bot_a': 2, 'bot_b': 3}
        changed.save()
        changed.rollback()
        self.assertEqual({'bot_a': 1}, changed.scenario_generations)
        self.assertEqual({'bot_a': 1}, self._player().scenario_generations)
        changed.reset()
        self.assertEqual({}, changed.scenario_generations)
        changed.save()
        self.assertEqual({}, self._player().scenario_generations)

    def test_後続Botが保存した世代情報も古い巻戻しから保護する(self):
        first = self._player()
        first.scenario_generations = {'bot_a': 1}
        first.save()
        second = self._player()
        second.scenario_generations = {'bot_a': 1, 'bot_b': 2}
        second.save()
        with self.assertRaises(RollbackConflictError):
            first.rollback()
        self.assertEqual({'bot_a': 1, 'bot_b': 2}, self._player().scenario_generations)

    def test_後続保存を保護し巻戻し競合を呼出し元へ伝える(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.store.delete_player_status('shared:line:user,U1')
                if existing:
                    self._seed()
                first = self._player()
                first.scene = 'first'
                first.save()
                second = self._player()
                second.scene = 'second'
                second['$count'] = 2
                second.save()
                winner = self.store.load_player_status(second.id)
                with self.assertRaises(RollbackConflictError):
                    first.rollback()
                self.assertEqual(winner, self.store.load_player_status(second.id))

    def test_自分の保存だけを戻し二度目の巻戻しはDBを変更しない(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.store.delete_player_status('shared:line:user,U1')
                if existing:
                    self._seed()
                before = self.store.load_player_status('shared:line:user,U1')
                status = self._player()
                status.scene = 'first'
                status['$count'] = 2
                status.save()
                with patch.object(self.store, 'update_player_status', wraps=self.store.update_player_status) as update, \
                        patch.object(self.store, 'delete_player_status', wraps=self.store.delete_player_status) as delete:
                    status.rollback()
                    status.rollback()
                self.assertEqual((1, 0) if existing else (0, 1), (update.call_count, delete.call_count))
                restored = self.store.load_player_status(status.id)
                self.assertEqual(before.data if before else None, restored.data if restored else None)

    def test_保存なしの巻戻しはDBを書き換えず未保存の値だけ戻す(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.store.delete_player_status('shared:line:user,U1')
                if existing:
                    self._seed()
                status = self._player()
                status['$count'] = 2
                other = self._player()
                other['$count'] = 3
                other.save()
                winner = self.store.load_player_status(status.id)
                with patch.object(self.store, 'update_player_status') as update, \
                        patch.object(self.store, 'delete_player_status') as delete:
                    status.rollback()
                update.assert_not_called()
                delete.assert_not_called()
                self.assertEqual(1 if existing else None, status.get('$count'))
                self.assertEqual(winner, self.store.load_player_status(status.id))

    def test_巻戻しの一般DB障害は隠さない(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.store.delete_player_status('shared:line:user,U1')
                if existing:
                    self._seed()
                status = self._player()
                status.scene = 'first'
                status.save()
                operation = 'update_player_status' if existing else 'delete_player_status'
                with patch.object(self.store, operation, side_effect=StateStoreError('保存先の障害')):
                    with self.assertRaisesRegex(RollbackError, '保存先の障害'):
                        status.rollback()

    def test_明示した強制保存は競合を上書きできる(self):
        self._seed()
        first, second = self._player(), self._player()
        second.scene = 'second'
        second.save()
        first.scene = 'forced'
        first.save(force=True)
        self.assertEqual('forced', self.store.load_player_status(first.id).data['scene'])


if __name__ == '__main__':
    unittest.main()
