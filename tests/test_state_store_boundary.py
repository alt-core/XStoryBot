import datetime
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from cloud_backend.contracts import (
    StateConflictError,
    StateVersion,
    VersionedState,
)
from cloud_backend.gcp.state_store import GcpStateStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def fake_google_modules():
    """外部接続なしでGCP adapterを検証するための最小module群。"""
    google = types.ModuleType('google')
    google.__path__ = []
    cloud = types.ModuleType('google.cloud')
    cloud.__path__ = []
    firestore = types.ModuleType('google.cloud.firestore')
    firestore.Client = Mock()
    firestore.SERVER_TIMESTAMP = object()
    firestore.transactional = lambda function: function

    api_core = types.ModuleType('google.api_core')
    api_core.__path__ = []
    datetime_helpers = types.ModuleType(
        'google.api_core.datetime_helpers')

    class FakeDatetimeWithNanoseconds:
        @classmethod
        def from_timestamp_pb(cls, timestamp):
            return SimpleNamespace(
                seconds=timestamp.seconds,
                nanos=timestamp.nanos,
            )

    datetime_helpers.DatetimeWithNanoseconds = FakeDatetimeWithNanoseconds
    protobuf = types.ModuleType('google.protobuf')
    protobuf.__path__ = []
    timestamp_pb2 = types.ModuleType('google.protobuf.timestamp_pb2')

    class Timestamp:
        def __init__(self, seconds=0, nanos=0):
            self.seconds = seconds
            self.nanos = nanos

    timestamp_pb2.Timestamp = Timestamp

    modules = {
        'google': google,
        'google.cloud': cloud,
        'google.cloud.firestore': firestore,
        'google.api_core': api_core,
        'google.api_core.datetime_helpers': datetime_helpers,
        'google.protobuf': protobuf,
        'google.protobuf.timestamp_pb2': timestamp_pb2,
    }
    with patch.dict(sys.modules, modules):
        yield firestore


class FakeUpdateTime:
    def __init__(self, seconds, nanos):
        self.seconds = seconds
        self.nanos = nanos

    def timestamp_pb(self):
        return SimpleNamespace(seconds=self.seconds, nanos=self.nanos)


class GcpStateStorePlayerTest(unittest.TestCase):
    def setUp(self):
        self.google_modules = fake_google_modules()
        self.firestore = self.google_modules.__enter__()
        self.client = Mock()
        self.collection = Mock()
        self.document = Mock()
        self.client.collection.return_value = self.collection
        self.collection.document.return_value = self.document
        self.store = GcpStateStore(client=self.client)

    def tearDown(self):
        self.google_modules.__exit__(None, None, None)

    def test_update_timeをlosslessなtokenへ変換してpreconditionへ戻す(self):
        loaded_time = FakeUpdateTime(1_700_000_000, 123_456_789)
        saved_time = FakeUpdateTime(1_700_000_001, 987_654_321)
        self.document.get.return_value = SimpleNamespace(
            exists=True,
            update_time=loaded_time,
            to_dict=lambda: {'scene': 'scene-1'},
        )
        self.document.update.return_value = SimpleNamespace(
            update_time=saved_time)
        write_option = object()
        self.client.write_option.return_value = write_option

        state = self.store.load_player_status('bot:line:user-1')
        next_version = self.store.update_player_status(
            'bot:line:user-1', {'scene': 'scene-2'}, state.version)

        self.assertEqual(
            state.version,
            StateVersion('firestore:v1:1700000000:123456789'),
        )
        self.assertEqual(
            next_version,
            StateVersion('firestore:v1:1700000001:987654321'),
        )
        restored = self.client.write_option.call_args.kwargs[
            'last_update_time']
        self.assertEqual(restored.seconds, 1_700_000_000)
        self.assertEqual(restored.nanos, 123_456_789)
        self.document.update.assert_called_once_with(
            {'scene': 'scene-2'}, option=write_option)

    def test_missingとcreateとforce_putの戻り値を維持する(self):
        self.document.get.return_value = SimpleNamespace(exists=False)
        self.document.create.return_value = SimpleNamespace(
            update_time=FakeUpdateTime(10, 20))
        self.document.set.return_value = SimpleNamespace(
            update_time=FakeUpdateTime(30, 40))

        self.assertIsNone(self.store.load_player_status('missing'))
        created = self.store.create_player_status('new', {'value': '{}'})
        forced = self.store.force_put_player_status(
            'existing', {'value': '{"x": 1}'})

        self.assertEqual(created, StateVersion('firestore:v1:10:20'))
        self.assertEqual(forced, StateVersion('firestore:v1:30:40'))

    def test_Googleの競合例外だけを共通例外へ変換する(self):
        failed_precondition = type(
            'FailedPrecondition',
            (Exception,),
            {'__module__': 'google.api_core.exceptions'},
        )
        self.document.create.side_effect = failed_precondition('conflict')

        with self.assertRaises(StateConflictError):
            self.store.create_player_status('new', {'value': '{}'})

        self.document.create.side_effect = RuntimeError('application error')
        with self.assertRaisesRegex(RuntimeError, 'application error'):
            self.store.create_player_status('new', {'value': '{}'})

        application_conflict = type(
            'Conflict',
            (Exception,),
            {'__module__': 'application.domain'},
        )
        error = application_conflict('application conflict')
        self.document.create.side_effect = error
        with self.assertRaises(application_conflict) as raised:
            self.store.create_player_status('new', {'value': '{}'})

        self.assertIs(raised.exception, error)


class GcpStateStoreAtomicOperationTest(unittest.TestCase):
    def setUp(self):
        self.google_modules = fake_google_modules()
        self.firestore = self.google_modules.__enter__()
        self.client = Mock()
        self.store = GcpStateStore(client=self.client)

    def tearDown(self):
        self.google_modules.__exit__(None, None, None)

    def test_group追加は同じshardのtransactionで重複を避ける(self):
        root_collection = Mock()
        group_document = Mock()
        shard_collection = Mock()
        shard_document = Mock()
        transaction = Mock()
        self.client.collection.return_value = root_collection
        root_collection.document.return_value = group_document
        group_document.collection.return_value = shard_collection
        shard_collection.document.return_value = shard_document
        self.client.transaction.return_value = transaction
        shard_document.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {'members': ['line:user-1']},
        )

        self.store.append_group_member(
            'group-1', 'ab', 'line:user-2')

        shard_document.get.assert_called_once_with(transaction=transaction)
        transaction.set.assert_called_once_with(shard_document, {
            'members': ['line:user-1', 'line:user-2'],
        })

    def test_next_labelの上書き後は現在値だけcompare_and_clearできる(self):
        collection = Mock()
        document = Mock()
        transaction = Mock()
        current = {
            'next_label': '##OLD',
            'trigger_message': '旧入力',
        }
        self.client.collection.return_value = collection
        collection.document.return_value = document
        self.client.transaction.return_value = transaction

        def snapshot(*args, **kwargs):
            del args, kwargs
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: dict(current),
            )

        def update(_document, data):
            current.update(data)

        document.get.side_effect = snapshot
        transaction.update.side_effect = update

        overwritten = self.store.set_next_label(
            'bot:line:user-1', '##NEW', '新入力')
        mismatch = self.store.compare_and_clear_next_label(
            'bot:line:user-1', '##OLD')
        cleared = self.store.compare_and_clear_next_label(
            'bot:line:user-1', '##NEW')

        self.assertEqual(overwritten, ('##OLD', '旧入力'))
        self.assertEqual(mismatch, (None, None))
        self.assertEqual(cleared, ('##NEW', '新入力'))
        self.assertEqual(
            document.get.call_args_list,
            [call(transaction=transaction)] * 3,
        )
        self.assertEqual(transaction.update.call_args_list, [
            call(document, {
                'next_label': '##NEW',
                'trigger_message': '新入力',
            }),
            call(document, {
                'next_label': None,
                'trigger_message': None,
            }),
        ])
        self.assertEqual(current, {
            'next_label': None,
            'trigger_message': None,
        })

    def test_task日時は標準UTC_datetimeへ正規化する(self):
        collection = Mock()
        document = Mock()
        self.client.collection.return_value = collection
        collection.document.return_value = document
        document.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {
                'created_at': datetime.datetime(
                    2026, 8, 12, 3, 0,
                    tzinfo=datetime.timezone.utc),
                'status': 'pending',
            },
        )

        task = self.store.get_group_message_task('task-1')

        self.assertIs(type(task['created_at']), datetime.datetime)
        self.assertEqual(
            task['created_at'].tzinfo, datetime.timezone.utc)

    def test_task更新callbackへ標準datetimeを渡す(self):
        class FirestoreDatetime(datetime.datetime):
            pass

        collection = Mock()
        document = Mock()
        transaction = Mock()
        self.client.collection.return_value = collection
        collection.document.return_value = document
        self.client.transaction.return_value = transaction
        document.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {
                'created_at': FirestoreDatetime(
                    2026, 8, 12, 3, 0,
                    tzinfo=datetime.timezone.utc),
                'status': 'pending',
            },
        )
        received = {}

        def update_builder(current):
            received.update(current)
            return {'status': 'running'}

        updated = self.store.update_group_message_task(
            'task-1', update_builder)

        self.assertTrue(updated)
        self.assertIs(type(received['created_at']), datetime.datetime)
        self.assertEqual(
            received['created_at'].tzinfo, datetime.timezone.utc)
        transaction.update.assert_called_once()

    def test_void相当のwriteはprovider固有結果を返さない(self):
        collection = Mock()
        document = Mock()
        shard_collection = Mock()
        batch = Mock()
        provider_result = object()
        self.client.collection.return_value = collection
        collection.document.return_value = document
        document.collection.return_value = shard_collection
        shard_collection.stream.return_value = []
        self.client.batch.return_value = batch
        document.set.return_value = provider_result
        document.delete.return_value = provider_result
        batch.commit.return_value = [provider_result]

        results = (
            self.store.save_global_bot_variables('bot', 'scenario-uri'),
            self.store.delete_player_status('bot:line:user-1'),
            self.store.clear_group_members('group-1'),
            self.store.put_image_file_stat('image-key', {'value': '1'}),
            self.store.put_media_file_stat('media-key', {'value': '1'}),
            self.store.put_image_text_stat('text-key', {'value': '1'}),
            self.store.clear_next_label('bot:line:user-1'),
            self.store.set_build_cache('cache-key', b'value'),
            self.store.delete_build_cache('cache-key'),
            self.store.create_group_message_task(
                'task-1', {'status': 'pending'}),
        )

        self.assertTrue(all(result is None for result in results))


class FacadeBoundaryTest(unittest.TestCase):
    def test_modelsはprovider非依存facadeのまま新規Playerを保存する(self):
        state_store = Mock()
        state_store.load_player_status.return_value = None
        version = StateVersion('version-1')
        state_store.create_player_status.return_value = version
        cloud_backend = types.ModuleType('cloud_backend')
        cloud_backend.create_state_store = Mock(return_value=state_store)
        utility = types.ModuleType('utility')
        utility.deep_dump = Mock()

        module_name = 'models_for_state_store_boundary_test'
        spec = importlib.util.spec_from_file_location(
            module_name, PROJECT_ROOT / 'models.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            'cloud_backend': cloud_backend,
            'utility': utility,
        }):
            spec.loader.exec_module(module)

        status = module.PlayerStatusDB('shared', 'line:user-1')
        status.save()

        state_store.create_player_status.assert_called_once()
        self.assertEqual(status.id, 'shared:line:user-1')
        self.assertEqual(status.last_update_time, version)
        self.assertFalse(status.is_dirty)
        self.assertIs(module.get_state_store(), state_store)

    def test_build_cacheは初回利用までStateStoreを生成しない(self):
        state_store = Mock()
        state_store.get_build_cache.return_value = b'cached'
        cloud_backend = types.ModuleType('cloud_backend')
        cloud_backend.create_state_store = Mock(return_value=state_store)

        module_name = 'build_cache_for_state_store_boundary_test'
        spec = importlib.util.spec_from_file_location(
            module_name, PROJECT_ROOT / 'build_cache.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'cloud_backend': cloud_backend}):
            spec.loader.exec_module(module)

        cloud_backend.create_state_store.assert_not_called()
        self.assertEqual(module.get_cache('key'), b'cached')
        cloud_backend.create_state_store.assert_called_once_with()

    def test_facadeからGoogle_SDK_importを除外する(self):
        for path in (
                PROJECT_ROOT / 'models.py',
                PROJECT_ROOT / 'build_cache.py',
                PROJECT_ROOT / 'group_message_task_db.py',
                PROJECT_ROOT / 'scenario.py',
                PROJECT_ROOT / 'task_client.py',
                PROJECT_ROOT / 'plugin/line/more.py',
                PROJECT_ROOT / 'plugin/line/image_text.py'):
            with self.subTest(path=path):
                source = path.read_text()
                self.assertNotIn('from google.', source)
                self.assertNotIn('import google.', source)
                self.assertNotIn('settings.GCP_SETTINGS', source)


if __name__ == '__main__':
    unittest.main()
