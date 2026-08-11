import datetime
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, call, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_group_message_task_manager():
    """外部サービスを読み込まず、Managerのbatch制御だけを読み込む。"""
    db_module = types.ModuleType('group_message_task_db')

    class StubGroupMessageTaskDB:
        STATUS_PENDING = 'pending'
        STATUS_RUNNING = 'running'
        STATUS_COMPLETED = 'completed'
        STATUS_FAILED = 'failed'
        STATUS_ABORTED = 'aborted'

        get_task = Mock()
        get_members_from_storage = Mock()
        update_task_status = Mock()
        process_members_in_parallel = Mock()
        _append_failed_member_list = Mock()

    db_module.GroupMessageTaskDB = StubGroupMessageTaskDB

    users_module = types.ModuleType('users')
    users_module.get_group_members = Mock()
    users_module.User = Mock()

    task_client_module = types.ModuleType('task_client')
    task_client_module.create_task = Mock()

    settings_module = types.ModuleType('settings')
    settings_module.OPTIONS = {}

    models_module = types.ModuleType('models')
    models_module.GroupMembersDB = object

    module_name = 'group_message_task_manager_for_batching_test'
    spec = importlib.util.spec_from_file_location(
        module_name,
        PROJECT_ROOT / 'group_message_task_manager.py',
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        'group_message_task_db': db_module,
        'users': users_module,
        'task_client': task_client_module,
        'settings': settings_module,
        'models': models_module,
    }):
        spec.loader.exec_module(module)
    return module


def load_group_message_task_db():
    """Google Cloudとmodelsをスタブ化し、DB層の純粋な処理を読み込む。"""
    google_module = types.ModuleType('google')
    cloud_module = types.ModuleType('google.cloud')
    firestore_module = types.ModuleType('google.cloud.firestore')
    storage_module = types.ModuleType('google.cloud.storage')
    exceptions_module = types.ModuleType('google.cloud.exceptions')

    class GoogleCloudError(Exception):
        pass

    class NotFound(GoogleCloudError):
        pass

    firestore_module.SERVER_TIMESTAMP = object()
    firestore_module.transactional = lambda function: function
    storage_module.Client = Mock()
    exceptions_module.GoogleCloudError = GoogleCloudError
    exceptions_module.NotFound = NotFound
    cloud_module.firestore = firestore_module
    cloud_module.storage = storage_module
    cloud_module.exceptions = exceptions_module
    google_module.cloud = cloud_module

    models_module = types.ModuleType('models')
    models_module.GroupMembersDB = Mock()
    models_module.db = Mock()

    utility_module = types.ModuleType('utility')
    utility_module.deep_dump = Mock()

    module_name = 'group_message_task_db_for_batching_test'
    spec = importlib.util.spec_from_file_location(
        module_name,
        PROJECT_ROOT / 'group_message_task_db.py',
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        'google': google_module,
        'google.cloud': cloud_module,
        'google.cloud.firestore': firestore_module,
        'google.cloud.storage': storage_module,
        'google.cloud.exceptions': exceptions_module,
        'models': models_module,
        'utility': utility_module,
    }):
        spec.loader.exec_module(module)
    return module


class SerializedMember:
    def __init__(self, member_id):
        self.member_id = member_id

    def serialize(self):
        return self.member_id


class GroupBatchTestBase(unittest.TestCase):
    def setUp(self):
        self.module = load_group_message_task_manager()
        self.db = self.module.GroupMessageTaskDB
        self.manager = self.module.GroupMessageTaskManager(
            'test-bot', bot_instance=Mock()
        )
        self.task = {
            'bot_name': 'test-bot',
            'group_id': 'test-group',
            'action': 'notice',
            'attrs': {'key': 'value'},
            'status': self.db.STATUS_PENDING,
            'successful_members': 0,
            'failed_members': 0,
            'error_messages': [],
        }
        self.db.get_task.side_effect = lambda task_id: self.task
        self.db.update_task_status.side_effect = self._update_task

    def _update_task(self, task_id, status, processed=None, successful=None,
                     failed=None, error=None, current_batch=None, **kwargs):
        self.task['status'] = status
        if processed is not None:
            self.task['processed_members'] = processed
        if successful is not None:
            self.task['successful_members'] = successful
        if failed is not None:
            self.task['failed_members'] = failed
        if error is not None:
            self.task.setdefault('error_messages', []).insert(0, error)
        if current_batch is not None:
            self.task['current_batch'] = current_batch
        return True

    def _set_members(self, count):
        members = [
            SerializedMember(f'mock-line:user-{index}')
            for index in range(count)
        ]
        self.module.users.get_group_members.return_value = members
        return members


class GroupBatchBoundaryTest(GroupBatchTestBase):
    def test_再送taskは保存した失敗者だけを処理する(self):
        failed_member = 'mock-line:user-499'
        self.task['is_retry'] = True
        self._set_members(500)
        self.db.get_members_from_storage.return_value = [failed_member]
        processed = []

        def process_members(**kwargs):
            processed.extend(kwargs['member_ids'])
            return len(kwargs['member_ids']), 0, kwargs['member_ids'], []

        self.db.process_members_in_parallel.side_effect = process_members

        result, status = self.manager.process_batch(
            'message-retry', 0, 2000, max_workers=1, max_rate=100
        )

        self.assertEqual(status, 200)
        self.assertEqual(result['success_count'], 1)
        self.assertEqual(processed, [failed_member])
        self.db.get_members_from_storage.assert_called_once_with(
            'message-retry'
        )
        self.module.users.get_group_members.assert_not_called()

    def test_通常taskは処理対象batchだけをserializeする(self):
        members = []
        for index in range(4):
            member = Mock()
            member.serialize.return_value = f'mock-line:user-{index}'
            members.append(member)
        self.module.users.get_group_members.return_value = members
        self.db.process_members_in_parallel.return_value = (
            2, 0, ['mock-line:user-2', 'mock-line:user-3'], []
        )

        result, status = self.manager.process_batch(
            'message-1', 1, 2, max_workers=1, max_rate=100
        )

        self.assertEqual(status, 200)
        self.assertEqual(result['success_count'], 2)
        members[0].serialize.assert_not_called()
        members[1].serialize.assert_not_called()
        members[2].serialize.assert_called_once_with()
        members[3].serialize.assert_called_once_with()

    def test_500人を250人ずつ2batchで重複なく処理する(self):
        members = self._set_members(500)
        processed = []

        def process_members(**kwargs):
            member_ids = kwargs['member_ids']
            processed.extend(member_ids)
            return len(member_ids), 0, member_ids, []

        self.db.process_members_in_parallel.side_effect = process_members

        first, first_status = self.manager.process_batch(
            'message-1', 0, 250, max_workers=1, max_rate=100
        )
        second, second_status = self.manager.process_batch(
            'message-1', 1, 250, max_workers=1, max_rate=100
        )

        expected = [member.serialize() for member in members]
        self.assertEqual((first_status, second_status), (200, 200))
        self.assertEqual(first['batch_count'], 2)
        self.assertEqual(second['success_count'], 500)
        self.assertEqual(processed, expected)
        self.assertEqual(len(set(processed)), 500)
        self.module.task_client.create_task.assert_called_once_with(
            queue_name='group-message-queue',
            url='/api/v1/bots/test-bot/process_group_batch',
            params={'message_task_id': 'message-1', 'batch_index': 1},
        )

    def test_10000人は2000人ずつ5batchになる(self):
        self._assert_batch_partition(
            total_count=10000,
            expected_sizes=[2000, 2000, 2000, 2000, 2000],
        )

    def test_10001人の最終batchは1人になる(self):
        self._assert_batch_partition(
            total_count=10001,
            expected_sizes=[2000, 2000, 2000, 2000, 2000, 1],
        )

    def _assert_batch_partition(self, total_count, expected_sizes):
        members = self._set_members(total_count)
        batches = []

        def process_members(**kwargs):
            member_ids = kwargs['member_ids']
            batches.append(member_ids)
            return len(member_ids), 0, member_ids, []

        self.db.process_members_in_parallel.side_effect = process_members

        for batch_index in range(len(expected_sizes)):
            result, status = self.manager.process_batch(
                'message-1', batch_index, 2000,
                max_workers=1, max_rate=100,
            )
            self.assertEqual(status, 200)

        flattened = [member_id for batch in batches for member_id in batch]
        expected = [member.serialize() for member in members]
        self.assertEqual([len(batch) for batch in batches], expected_sizes)
        self.assertEqual(flattened, expected)
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertEqual(result['batch_count'], len(expected_sizes))
        self.assertEqual(result['success_count'], total_count)


class GroupBatchAccumulationTest(GroupBatchTestBase):
    def test_複数batchの成功失敗数と失敗者を累積する(self):
        self._set_members(500)
        results = [
            (
                249,
                1,
                [f'mock-line:user-{index}' for index in range(249)],
                [('mock-line:user-249', 'failure-1', 1.0)],
            ),
            (
                248,
                2,
                [f'mock-line:user-{index}' for index in range(250, 498)],
                [
                    ('mock-line:user-498', 'failure-2', 2.0),
                    ('mock-line:user-499', 'failure-3', 3.0),
                ],
            ),
        ]
        self.db.process_members_in_parallel.side_effect = results

        self.manager.process_batch(
            'message-1', 0, 250, max_workers=1, max_rate=100
        )
        result, status = self.manager.process_batch(
            'message-1', 1, 250, max_workers=1, max_rate=100
        )

        self.assertEqual(status, 200)
        self.assertEqual(result['success_count'], 497)
        self.assertEqual(result['error_count'], 3)
        self.assertEqual(self.task['processed_members'], 500)
        self.assertEqual(self.task['successful_members'], 497)
        self.assertEqual(self.task['failed_members'], 3)
        self.assertEqual(
            self.db._append_failed_member_list.call_args_list,
            [
                call('message-1', ['mock-line:user-249']),
                call(
                    'message-1',
                    ['mock-line:user-498', 'mock-line:user-499'],
                ),
            ],
        )


class GroupBatchReservationTest(GroupBatchTestBase):
    def test_予約が60秒より先なら再予約する(self):
        self.task['scheduled_at'] = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=61)
        )

        with patch.object(self.manager, 'process_batch') as process_batch:
            result, status = self.manager.handle_batch_process_request(
                'message-1', batch_index=0
            )

        self.assertEqual(status, 200)
        self.assertEqual(result['status'], self.db.STATUS_PENDING)
        process_batch.assert_not_called()
        create_call = self.module.task_client.create_task.call_args
        self.assertEqual(
            create_call.kwargs['params'],
            {'message_task_id': 'message-1', 'batch_index': 0},
        )
        self.assertGreater(create_call.kwargs['delay_seconds'], 60)

    def test_予約が60秒以内なら即時処理する(self):
        self.task['scheduled_at'] = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=60)
        )

        with patch.object(
            self.manager,
            'process_batch',
            return_value=({'message': 'processed'}, 200),
        ) as process_batch:
            result, status = self.manager.handle_batch_process_request(
                'message-1', batch_index=0
            )

        self.assertEqual(status, 200)
        self.assertEqual(result, {'message': 'processed'})
        process_batch.assert_called_once_with(
            'message-1', 0, 2000, 150, 500
        )
        self.module.task_client.create_task.assert_not_called()


class GroupMessageTaskDBTest(unittest.TestCase):
    def setUp(self):
        self.module = load_group_message_task_db()
        self.db = self.module.GroupMessageTaskDB

    def test_flat設定と既定値を使う(self):
        self.db.initialize(
            {'storage_bucket': 'test-bucket'},
            {'group_message_task': {
                'batch_size': 1,
                'max_workers': 2,
                'max_rate': 3,
            }},
        )

        self.assertEqual(self.db._batch_size, 2000)
        self.assertEqual(self.db._default_max_workers, 150)
        self.assertEqual(self.db._default_max_rate, 500)

        self.db.initialize(
            {'storage_bucket': 'test-bucket'},
            {
                'group_batch_size': 250,
                'group_max_workers': 25,
                'group_max_rate': 75,
            },
        )
        self.assertEqual(self.db._batch_size, 250)
        self.assertEqual(self.db._default_max_workers, 25)
        self.assertEqual(self.db._default_max_rate, 75)

    def test_明示空listはGCSへfallbackしない(self):
        with (
            patch.object(self.db, 'get_task', return_value=None),
            patch.object(
                self.db,
                'get_members_from_storage',
                side_effect=AssertionError('GCSを読み込んではならない'),
            ) as get_members,
            patch.object(
                self.db,
                'create_rate_limiter',
                return_value=lambda function: function,
            ),
            patch.object(self.db, 'update_task_status'),
            patch.object(self.db, '_store_successful_members'),
            patch.object(self.db, '_store_error_logs'),
        ):
            result = self.db.process_members_in_parallel(
                'message-1_batch_0',
                process_function=Mock(),
                max_workers=1,
                max_rate=100,
                member_ids=[],
            )

        get_members.assert_not_called()
        self.assertEqual(result, (0, 0, [], []))

    def test_NoneだけがGCSへfallbackする(self):
        processed = []
        with (
            patch.object(self.db, 'get_task', return_value=None),
            patch.object(
                self.db,
                'get_members_from_storage',
                return_value=['mock-line:user-1'],
            ) as get_members,
            patch.object(
                self.db,
                'create_rate_limiter',
                return_value=lambda function: function,
            ),
            patch.object(self.db, 'update_task_status'),
            patch.object(self.db, '_store_successful_members'),
            patch.object(self.db, '_store_error_logs'),
        ):
            result = self.db.process_members_in_parallel(
                'message-1_batch_0',
                process_function=lambda member_id: (
                    processed.append(member_id) is None,
                    None,
                ),
                max_workers=1,
                max_rate=100,
                member_ids=None,
            )

        get_members.assert_called_once_with('message-1')
        self.assertEqual(processed, ['mock-line:user-1'])
        self.assertEqual(result[:3], (1, 0, ['mock-line:user-1']))

    def test_499人成功1人失敗なら再送対象は失敗者だけになる(self):
        failed_member = 'mock-line:user-499'
        original_task = {
            'bot_name': 'test-bot',
            'group_id': 'test-group',
            'action': 'notice',
            'attrs': {},
            'total_members': 500,
            'successful_members': 499,
            'failed_members': 1,
        }
        document = Mock()
        collection = Mock()
        collection.document.return_value = document
        fake_db = Mock()
        fake_db.collection.return_value = collection

        with (
            patch.object(self.module, 'db', fake_db),
            patch.object(self.db, 'get_task', return_value=original_task),
            patch.object(
                self.db,
                '_get_failed_members_from_storage',
                return_value=[failed_member],
            ) as get_failed,
            patch.object(
                self.db,
                'get_remaining_members',
                side_effect=AssertionError('成功者との差分を再計算してはならない'),
            ) as get_remaining,
            patch.object(
                self.db,
                '_store_member_list',
                return_value='gs://test-bucket/retry/members.json',
            ) as store_members,
        ):
            retry_task_id = self.db.retry_failed_members(
                'message-1', created_by='test-user'
            )

        self.assertIsNotNone(retry_task_id)
        get_failed.assert_called_once_with('message-1')
        get_remaining.assert_not_called()
        store_members.assert_called_once_with(retry_task_id, [failed_member])
        retry_data = document.set.call_args.args[0]
        self.assertEqual(retry_data['total_members'], 1)
        self.assertEqual(retry_data['total_batches'], 1)
        self.assertEqual(retry_data['original_task_id'], 'message-1')
        self.assertIs(retry_data['is_retry'], True)


if __name__ == '__main__':
    unittest.main()
