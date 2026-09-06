import datetime
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, call, patch

import cloud_backend
from cloud_backend.contracts import ObjectNotFoundError, ObjectStoreError


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
    """cloud境界とmodelsをスタブ化し、DB層の純粋な処理を読み込む。"""
    state_store = Mock()
    object_store = Mock()
    object_store.store_private.return_value = 'opaque-object-reference'

    models_module = types.ModuleType('models')
    models_module.GroupMembersDB = Mock()
    models_module.get_state_store = Mock(return_value=state_store)

    module_name = 'group_message_task_db_for_batching_test'
    spec = importlib.util.spec_from_file_location(
        module_name,
        PROJECT_ROOT / 'group_message_task_db.py',
    )
    module = importlib.util.module_from_spec(spec)
    with (
        patch.object(
            cloud_backend, 'create_object_store',
            return_value=object_store,
        ),
        patch.dict(sys.modules, {'models': models_module}),
    ):
        spec.loader.exec_module(module)
    module._test_state_store = state_store
    module._test_object_store = object_store
    module._test_group_members_db = models_module.GroupMembersDB
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


class GroupMemberDeliveryTest(GroupBatchTestBase):
    def test_handle_actionのNoneは失敗として数える(self):
        # runtime.handle_action は送信の再試行を尽くすと None を返す
        self.manager.bot.handle_action.return_value = None

        self.assertEqual(
            (False, 'handle_action failed'),
            self.manager._process_member('mock-line:user-1', self.task))

    def test_handle_actionの応答があれば成功に数える(self):
        self.manager.bot.handle_action.return_value = 'OK'

        self.assertEqual(
            (True, None),
            self.manager._process_member('mock-line:user-1', self.task))

    def test_通常taskはグループメンバーを1回だけ取得する(self):
        self._set_members(3)
        self.db.process_members_in_parallel.return_value = (3, 0, [], [])

        self.manager.process_batch('message-1', 0, 2000, max_workers=1, max_rate=100)

        self.module.users.get_group_members.assert_called_once_with('test-group')


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
        self.assertEqual(self.task['error_messages'], [
            'mock-line:user-498: failure-2\nmock-line:user-499: failure-3',
            'mock-line:user-249: failure-1',
        ])
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
        self.db.initialize(
            {'storage_bucket': 'test-bucket'},
            {},
        )
        self.state_store = self.module._test_state_store
        self.object_store = self.module._test_object_store
        self.group_members_db = self.module._test_group_members_db

    def test_旧gcp_settings_keywordでも初期化できる(self):
        self.db.initialize(
            gcp_settings={'storage_bucket': 'test-bucket'},
            options={},
        )

        self.assertEqual(self.db._batch_size, 2000)

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
        self.assertEqual(self.state_store.mock_calls, [])
        self.assertEqual(result, (0, 0, [], []))

    def test_NoneだけがGCSへfallbackする(self):
        processed = []
        with (
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
        self.assertEqual(self.state_store.mock_calls, [])
        self.assertEqual(processed, ['mock-line:user-1'])
        self.assertEqual(result[:3], (1, 0, ['mock-line:user-1']))

    def test_並列処理はtaskDBを使わずbatchの成功者とエラーログを保存する(self):
        processed = []

        def process_member(member_id):
            processed.append(member_id)
            if member_id == 'exception':
                raise ValueError('送信例外')
            if member_id == 'failure':
                return False, '送信失敗'
            return True, None

        with patch.object(
            self.db, 'create_rate_limiter',
            return_value=lambda function: function,
        ):
            result = self.db.process_members_in_parallel(
                'message-1_batch_0', process_member,
                max_workers=1, max_rate=100,
                member_ids=['success', 'failure', 'exception'],
            )

        self.assertCountEqual(processed, ['success', 'failure', 'exception'])
        self.assertEqual(self.state_store.mock_calls, [])
        self.assertEqual(result[:3], (1, 2, ['success']))
        self.assertCountEqual(
            [(member, error) for member, error, _ in result[3]],
            [('failure', '送信失敗'), ('exception', '送信例外')],
        )
        saved = {
            args[0]: json.loads(args[1])
            for args, _ in self.object_store.store_private.call_args_list
        }
        self.assertEqual(saved, {
            'group_tasks/message-1_batch_0/successful_members.json': ['success'],
            'group_tasks/message-1_batch_0/error_logs.json': [
                list(error) for error in result[3]
            ],
        })

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
        with (
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
                return_value='opaque-retry-reference',
            ) as store_members,
        ):
            retry_task_id = self.db.retry_failed_members(
                'message-1', created_by='test-user'
            )

        self.assertIsNotNone(retry_task_id)
        get_failed.assert_called_once_with('message-1')
        get_remaining.assert_not_called()
        store_members.assert_called_once_with(retry_task_id, [failed_member])
        self.state_store.create_group_message_task.assert_called_once()
        stored_task_id, retry_data = (
            self.state_store.create_group_message_task.call_args.args)
        self.assertEqual(stored_task_id, retry_task_id)
        self.assertEqual(retry_data['total_members'], 1)
        self.assertEqual(retry_data['total_batches'], 1)
        self.assertEqual(retry_data['original_task_id'], 'message-1')
        self.assertIs(retry_data['is_retry'], True)

    def test_createはmember_JSON保存後にtaskをStateStoreへ保存する(self):
        self.group_members_db.get_members.return_value = [
            'mock-line:user-1', 'mock-line:user-2']
        self.object_store.store_private.return_value = (
            'opaque-member-list-reference')
        calls = []
        self.object_store.store_private.side_effect = (
            lambda *args, **kwargs: (
                calls.append(('object', args, kwargs)) or
                'opaque-member-list-reference'))
        self.state_store.create_group_message_task.side_effect = (
            lambda *args, **kwargs: calls.append(('state', args, kwargs)))

        with (
            patch.object(self.module.time, 'time', return_value=123),
            patch.object(
                self.module.uuid, 'uuid4',
                return_value=types.SimpleNamespace(hex='abcdef0123456789')),
        ):
            task_id = self.db.create_task(
                'test-bot', 'test-group', 'notice', {'key': 'value'},
                'admin@example.invalid')

        self.assertEqual(task_id, '123-abcdef01')
        self.assertEqual([entry[0] for entry in calls], ['object', 'state'])
        key, content = calls[0][1]
        self.assertEqual(key, 'group_tasks/123-abcdef01/members.json')
        self.assertEqual(
            json.loads(content),
            ['mock-line:user-1', 'mock-line:user-2'])
        stored_task_id, task_data = calls[1][1]
        self.assertEqual(stored_task_id, task_id)
        self.assertNotIn('created_at', task_data)
        self.assertNotIn('updated_at', task_data)
        self.assertEqual(task_data['total_members'], 2)
        self.assertEqual(
            task_data['member_list_url'],
            'opaque-member-list-reference')

    def test_空groupはObjectStoreとStateStoreを呼ばない(self):
        self.group_members_db.get_members.return_value = []

        with self.assertRaises(ValueError):
            self.db.create_task(
                'test-bot', 'empty-group', 'notice', {},
                'admin@example.invalid')

        self.object_store.store_private.assert_not_called()
        self.state_store.create_group_message_task.assert_not_called()

    def test_update_builderはtransaction内のlist規則とGCS追記を維持する(self):
        old_errors = [f'error-{index}' for index in range(10)]
        old_failed = [f'user-{index}' for index in range(100)]
        self.object_store.load_private.return_value = json.dumps(
            ['existing-user']).encode('utf-8')

        def update_task(_task_id, builder):
            self.updated_data = builder({
                'error_messages': old_errors,
                'failed_member_ids': old_failed,
            })
            return True

        self.state_store.update_group_message_task.side_effect = update_task

        result = self.db.update_task_status(
            'task-1', self.db.STATUS_FAILED,
            processed=101, successful=100, failed=1,
            error='new-error', failed_member_id='new-user')

        self.assertIs(result, True)
        self.assertEqual(
            self.updated_data['error_messages'],
            ['new-error'] + old_errors[:9])
        self.assertEqual(
            self.updated_data['failed_member_ids'],
            old_failed[1:] + ['new-user'])
        self.object_store.load_private.assert_called_once_with(
            'group_tasks/task-1/failed_members.json')
        self.object_store.store_private.assert_called_once_with(
            'group_tasks/task-1/failed_members.json',
            json.dumps(['existing-user', 'new-user']))

    def test_概要は新規と保存済みの両方を最大10件各2000文字に制限する(self):
        old_errors = ['古い概要' * 1000] + [f'error-{index}' for index in range(12)]
        for error in (None, '単独の長いエラー' * 1000, '境' * 2000):
            with self.subTest(error_length=None if error is None else len(error)):
                updates = []
                self.state_store.update_group_message_task.side_effect = (
                    lambda _task_id, builder: updates.append(
                        builder({'error_messages': old_errors})) or True)

                self.db.update_task_status('task-1', self.db.STATUS_RUNNING, error=error)

                messages = updates[0]['error_messages']
                self.assertEqual(len(messages), 10)
                self.assertTrue(all(len(message) <= 2000 for message in messages))
                old_index = 0 if error is None else 1
                self.assertEqual(len(messages[old_index]), 2000)
                self.assertTrue(messages[old_index].endswith('…（省略）'))
                if error is not None:
                    if len(error) == 2000:
                        self.assertEqual(messages[0], error)
                    else:
                        self.assertEqual(len(messages[0]), 2000)
                        self.assertTrue(messages[0].endswith('…（省略）'))
                self.assertEqual(old_errors[0], '古い概要' * 1000)
                self.assertEqual(messages[-1], f'error-{8 - old_index}')

    def test_2000人ずつ3batchの大量失敗でも全件記録と集計を残して完了する(self):
        manager_module = load_group_message_task_manager()
        manager_module.GroupMessageTaskDB = self.db
        manager = manager_module.GroupMessageTaskManager('test-bot')
        task = {
            'bot_name': 'test-bot', 'group_id': 'test-group',
            'action': 'notice', 'attrs': {}, 'status': self.db.STATUS_PENDING,
            'total_members': 6000, 'successful_members': 0, 'failed_members': 0,
            'error_messages': [],
        }
        saved = {}

        def update_task(_task_id, builder):
            updated = {**task, **builder(task)}
            # 大きな概要を持つ更新がDB上限で拒否される状況を再現する。
            self.assertLess(len(json.dumps(updated).encode('utf-8')), 400 * 1024)
            task.update(updated)
            return True

        def load_object(key):
            if key not in saved:
                raise ObjectNotFoundError('missing')
            return saved[key].encode('utf-8')

        self.state_store.update_group_message_task.side_effect = update_task
        self.object_store.load_private.side_effect = load_object
        self.object_store.store_private.side_effect = (
            lambda key, content, *args: saved.__setitem__(key, content))
        expected_failed = []
        long_error = '長い単独エラー' * 1000
        with patch.object(
            self.db, 'create_rate_limiter', return_value=lambda function: function,
        ):
            for batch in range(3):
                members = [f'line:user,U{batch * 2000 + index:032x}' for index in range(2000)]
                errors = {
                    member: long_error if index == 0 else 'handle_action failed'
                    for index, member in enumerate(members[:-1])
                }
                expected_failed.extend(members[:-1])
                result = self.db.process_members_in_parallel(
                    f'task-1_batch_{batch}',
                    lambda member: (False, errors[member]) if member in errors else (True, None),
                    max_workers=1, max_rate=100, member_ids=members,
                )
                response, status = manager._handle_batch_completion(
                    'task-1', dict(task), batch, 3, result[0], result[1], result[3],
                    2000, 6000,
                )
                self.assertEqual(status, 200)
                stored_errors = json.loads(saved[f'group_tasks/task-1_batch_{batch}/error_logs.json'])
                self.assertEqual(len(stored_errors), 1999)
                self.assertEqual(dict((item[0], item[1]) for item in stored_errors), errors)
                self.assertEqual(
                    json.loads(saved[f'group_tasks/task-1_batch_{batch}/successful_members.json']),
                    members[-1:],
                )

        self.assertEqual(task['status'], self.db.STATUS_COMPLETED)
        self.assertEqual(task['processed_members'], 6000)
        self.assertEqual((task['successful_members'], task['failed_members']), (3, 5997))
        self.assertEqual((response['success_count'], response['error_count']), (3, 5997))
        self.assertEqual(task['current_batch'], 3)
        self.assertEqual(len(task['error_messages']), 3)
        self.assertTrue(all(len(message) <= 2000 for message in task['error_messages']))
        self.assertCountEqual(
            json.loads(saved['group_tasks/task-1/failed_members.json']), expected_failed)
        self.assertEqual(manager_module.task_client.create_task.call_count, 2)

    def test_task不存在ならupdate_builderとGCS追記を実行しない(self):
        self.state_store.update_group_message_task.return_value = False

        result = self.db.update_task_status(
            'missing-task', self.db.STATUS_FAILED,
            failed_member_id='new-user')

        self.assertIs(result, False)
        builder = self.state_store.update_group_message_task.call_args.args[1]
        self.assertTrue(callable(builder))
        self.object_store.load_private.assert_not_called()
        self.object_store.store_private.assert_not_called()

    def test_member_JSONのNotFoundとStoreErrorは致命的なまま(self):
        for error in (
                ObjectNotFoundError('missing'),
                ObjectStoreError('failed')):
            with self.subTest(error=type(error).__name__):
                self.object_store.load_private.reset_mock()
                self.object_store.load_private.side_effect = error
                with self.assertRaises(type(error)):
                    self.db.get_members_from_storage('task-1')

    def test_failed_JSON読取失敗は空listから上書きする既存挙動を維持する(self):
        self.object_store.load_private.side_effect = ValueError('broken JSON')

        self.db._append_failed_member_list('task-1', ['new-user'])

        self.object_store.store_private.assert_called_once_with(
            'group_tasks/task-1/failed_members.json',
            json.dumps(['new-user']))

    def test_result_JSON取得失敗は空listを返す(self):
        getters = (
            self.db._get_failed_members_from_storage,
            self.db.get_successful_members,
            self.db.get_error_logs,
        )
        for getter in getters:
            for error in (
                    ObjectNotFoundError('missing'),
                    ObjectStoreError('failed'),
                    ValueError('broken JSON')):
                with self.subTest(getter=getter.__name__, error=type(error).__name__):
                    self.object_store.load_private.reset_mock()
                    self.object_store.load_private.side_effect = error
                    self.assertEqual(getter('task-1'), [])

    def test_facadeはGoogle_SDKを直接importしない(self):
        source = (PROJECT_ROOT / 'group_message_task_db.py').read_text()
        self.assertNotIn('from google.', source)
        self.assertNotIn('import google.', source)


if __name__ == '__main__':
    unittest.main()
