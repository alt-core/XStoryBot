import copy
import datetime
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, call, patch

import cloud_backend
from cloud_backend.contracts import ObjectNotFoundError, ObjectStoreError, StateStoreError


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_group_message_task_manager():
    """外部サービスを読み込まず、Managerのbatch制御だけを読み込む。"""
    db_module = load_group_message_task_db()
    for method in (
            'get_task', 'get_members_from_storage', 'update_task_status',
            'update_task', 'process_members_in_parallel', '_append_failed_member_list'):
        setattr(db_module.GroupMessageTaskDB, method, Mock())

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
            'current_batch': 0,
            'processed_members': 0,
            'successful_members': 0,
            'failed_members': 0,
            'error_messages': [],
        }
        self.db.get_task.side_effect = lambda task_id: copy.deepcopy(self.task)
        self.db.update_task_status.side_effect = self._update_task
        self.db.update_task.side_effect = self._update_checkpoint

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

    def _update_checkpoint(self, task_id, builder):
        self.task.update(builder(copy.deepcopy(self.task)))
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
        self.task['current_batch'] = 1
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
        self.assertEqual(self.task['current_batch'], 0)
        self.assertEqual(self.task['processed_members'], 0)
        self.db.update_task.assert_not_called()

    def test_早着の登録失敗も取消にせず同じbatchを再予約できる(self):
        self.task['scheduled_at'] = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(minutes=5)
        )
        self.module.task_client.create_task.side_effect = RuntimeError('予約登録失敗')
        with patch.object(self.manager, 'process_batch') as process_batch:
            self.assertEqual(
                self.manager.handle_batch_process_request('message-1', 0)[1], 500)
            self.assertEqual(self.task['status'], self.db.STATUS_FAILED)
            self.module.task_client.create_task.side_effect = None
            self.assertEqual(
                self.manager.handle_batch_process_request('message-1', 0)[1], 200)

        process_batch.assert_not_called()
        self.assertEqual(self.task['status'], self.db.STATUS_PENDING)
        self.assertIn('予約登録失敗', self.task['error_messages'][0])
        self.assertEqual(self.task['current_batch'], 0)
        self.assertEqual(self.task['processed_members'], 0)
        for create_call in self.module.task_client.create_task.call_args_list:
            self.assertEqual(create_call.kwargs['params'], {
                'message_task_id': 'message-1', 'batch_index': 0,
            })
            self.assertGreater(create_call.kwargs['delay_seconds'], 60)

    def test_再予約成功の状態復帰は取消を上書きしない(self):
        self.task.update(
            status=self.db.STATUS_FAILED,
            scheduled_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5),
            error_messages=['予約登録失敗'],
        )
        self.module.task_client.create_task.side_effect = (
            lambda **_kwargs: self.task.update(status=self.db.STATUS_ABORTED))
        result, status = self.manager.handle_batch_process_request('message-1', 0)
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], self.db.STATUS_ABORTED)
        self.assertEqual(self.task['status'], self.db.STATUS_ABORTED)
        self.assertEqual(self.task['current_batch'], 0)
        self.assertEqual(self.task['error_messages'], ['予約登録失敗'])

    def test_再予約成功の状態復帰は先行進捗を巻き戻さない(self):
        self.task.update(
            status=self.db.STATUS_FAILED,
            scheduled_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5),
        )
        self.module.task_client.create_task.side_effect = (
            lambda **_kwargs: self.task.update(
                status=self.db.STATUS_RUNNING, current_batch=1,
                processed_members=2, successful_members=2))
        result, status = self.manager.handle_batch_process_request('message-1', 0)
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], self.db.STATUS_RUNNING)
        self.assertEqual(self.task['current_batch'], 1)
        self.assertEqual(self.task['processed_members'], 2)
        self.assertEqual(self.task['successful_members'], 2)

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


class GroupCheckpointTestBase(unittest.TestCase):
    def setUp(self):
        self.db_module = load_group_message_task_db()
        self.module = load_group_message_task_manager()
        self.module.GroupMessageTaskDB = self.db_module.GroupMessageTaskDB
        self.db = self.db_module.GroupMessageTaskDB
        self.db._state_store = self.db_module._test_state_store
        self.db._object_store = self.db_module._test_object_store
        self.manager = self.module.GroupMessageTaskManager('test-bot')
        self.task = {
            'bot_name': 'test-bot', 'group_id': 'test-group',
            'action': 'notice', 'attrs': {}, 'status': self.db.STATUS_PENDING,
            'total_members': 4, 'total_batches': 2,
            'current_batch': 0, 'processed_members': 0,
            'successful_members': 0, 'failed_members': 0,
            'error_messages': [],
        }
        self.db._state_store.get_group_message_task.side_effect = (
            lambda _task_id: copy.deepcopy(self.task))
        self.db._state_store.update_group_message_task.side_effect = self.update_task
        self.db._object_store.load_private.side_effect = ObjectNotFoundError('missing')
        self.module.users.get_group_members.return_value = [
            SerializedMember(f'mock-line:user-{index}') for index in range(4)]
        self.sent = []
        self.manager._process_batch_members = Mock(side_effect=self.send_members)
        self.updates = []

    def update_task(self, _task_id, builder):
        update = builder(copy.deepcopy(self.task))
        self.updates.append(copy.deepcopy(update))
        self.task.update(update)
        return True

    def send_members(self, _batch_task_id, member_ids, _task, _max_workers, _max_rate):
        self.sent.extend(member_ids)
        return len(member_ids), 0, member_ids, []

    def invoke(self, batch_index=0):
        return self.manager.handle_batch_process_request(
            'message-1', batch_index, batch_size=2, max_workers=1, max_rate=100)


class GroupBatchCheckpointTest(GroupCheckpointTestBase):
    def test_次batchがinvoke中に完了しても親は進捗を巻き戻さない(self):
        def run_next_batch(**kwargs):
            self.assertEqual(self.task['current_batch'], 1)
            self.assertEqual(self.task['processed_members'], 2)
            self.assertEqual(kwargs['params']['batch_index'], 1)
            self.assertEqual(self.invoke(1)[1], 200)

        self.module.task_client.create_task.side_effect = run_next_batch

        response, status = self.invoke()

        self.assertEqual(status, 200)
        self.assertEqual(response['next_batch_index'], 1)
        self.assertEqual(self.task['status'], self.db.STATUS_COMPLETED)
        self.assertEqual(self.task['current_batch'], 2)
        self.assertEqual(self.task['processed_members'], 4)
        self.assertEqual(self.task['successful_members'], 4)
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(self.updates[-1]['status'], self.db.STATUS_COMPLETED)

    def test_invoke失敗後は送信済みメンバーを再送せず登録だけ再開する(self):
        self.module.task_client.create_task.side_effect = RuntimeError('登録失敗')

        self.assertEqual(self.invoke()[1], 500)
        self.assertEqual(self.task['status'], self.db.STATUS_FAILED)
        self.assertEqual(self.task['current_batch'], 1)
        self.assertEqual(self.task['successful_members'], 2)
        self.module.task_client.create_task.side_effect = None
        self.assertEqual(self.invoke()[1], 200)

        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.task['status'], self.db.STATUS_RUNNING)
        self.assertEqual(self.task['successful_members'], 2)
        self.assertEqual(self.module.task_client.create_task.call_count, 2)
        self.assertEqual(self.invoke(1)[1], 200)
        self.assertEqual(self.task['successful_members'], 4)

    def test_invoke応答喪失後の再登録でも親の集計を重複しない(self):
        registered = []

        def lose_ack(**kwargs):
            registered.append(kwargs['params']['batch_index'])
            if len(registered) == 1:
                raise RuntimeError('登録応答を受け取れませんでした')

        self.module.task_client.create_task.side_effect = lose_ack
        self.assertEqual(self.invoke()[1], 500)
        self.assertEqual(self.invoke()[1], 200)

        self.assertEqual(registered, [1, 1])
        self.assertEqual(self.task['successful_members'], 2)
        self.assertEqual(len(self.sent), 2)

    def test_checkpoint保存失敗はfailedとして再試行できる(self):
        def fail_checkpoint(task_id, builder):
            update = builder(copy.deepcopy(self.task))
            if 'current_batch' in update:
                raise StateStoreError('保存失敗')
            return self.update_task(task_id, builder)

        self.db._state_store.update_group_message_task.side_effect = fail_checkpoint
        self.assertEqual(self.invoke()[1], 500)
        self.assertEqual(self.task['status'], self.db.STATUS_FAILED)
        self.assertEqual(self.task['current_batch'], 0)
        self.module.task_client.create_task.assert_not_called()

        self.db._state_store.update_group_message_task.side_effect = self.update_task
        self.assertEqual(self.invoke()[1], 200)
        self.assertEqual(self.task['successful_members'], 2)
        # 送信後・結果保存前の失敗では、送信そのものは重複し得る。
        self.assertEqual(len(self.sent), 4)

    def test_checkpoint保存応答喪失では集計とメンバー送信を重複しない(self):
        lost_ack = [False]

        def save_and_lose_ack(task_id, builder):
            update = builder(copy.deepcopy(self.task))
            self.update_task(task_id, builder)
            if 'current_batch' in update and not lost_ack[0]:
                lost_ack[0] = True
                raise StateStoreError('保存応答を受け取れませんでした')
            return True

        self.db._state_store.update_group_message_task.side_effect = save_and_lose_ack
        self.assertEqual(self.invoke()[1], 500)
        self.assertEqual(self.task['current_batch'], 1)
        self.module.task_client.create_task.assert_not_called()
        self.assertEqual(self.invoke()[1], 200)

        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.task['processed_members'], 2)
        self.assertEqual(self.task['successful_members'], 2)

    def test_処理例外を利用者取消にせずfailedから再試行する(self):
        self.manager._process_batch_members.side_effect = RuntimeError('処理失敗')
        self.assertEqual(self.invoke()[1], 500)
        self.assertEqual(self.task['status'], self.db.STATUS_FAILED)

        self.manager._process_batch_members.side_effect = self.send_members
        self.assertEqual(self.invoke()[1], 200)
        self.assertEqual(self.task['current_batch'], 1)

    def test_利用者取消と完了は送信せず正常skipする(self):
        for terminal_status in (self.db.STATUS_ABORTED, self.db.STATUS_COMPLETED):
            with self.subTest(status=terminal_status):
                self.task['status'] = terminal_status
                response, status = self.invoke()
                self.assertEqual(status, 200)
                self.assertEqual(response['status'], terminal_status)
        self.manager._process_batch_members.assert_not_called()
        self.module.task_client.create_task.assert_not_called()
        self.assertEqual(self.updates, [])

    def test_送信中の取消をcheckpointでrunningやcompletedへ戻さない(self):
        for batch_index in (0, 1):
            with self.subTest(batch=batch_index):
                self.task.update(status=self.db.STATUS_RUNNING, current_batch=batch_index)
                self.task['error_messages'] = ['取消前の概要']
                self.module.task_client.create_task.reset_mock()

                def cancel_during_send(*args):
                    self.task['status'] = self.db.STATUS_ABORTED
                    return self.send_members(*args)

                self.manager._process_batch_members.side_effect = cancel_during_send
                response, status = self.invoke(batch_index)
                self.assertEqual(status, 200)
                self.assertEqual(response['status'], self.db.STATUS_ABORTED)
                self.assertEqual(self.task['status'], self.db.STATUS_ABORTED)
                self.assertEqual(self.task['error_messages'], ['取消前の概要'])
                self.assertEqual(self.task['current_batch'], batch_index)
                self.module.task_client.create_task.assert_not_called()

    def test_失敗記録時にも取消状態と概要を変更しない(self):
        def cancel_and_fail(*_args):
            self.task.update(status=self.db.STATUS_ABORTED, error_messages=['利用者取消'])
            raise RuntimeError('処理失敗')

        self.manager._process_batch_members.side_effect = cancel_and_fail
        self.assertEqual(self.invoke()[1], 500)
        self.assertEqual(self.task['status'], self.db.STATUS_ABORTED)
        self.assertEqual(self.task['error_messages'], ['利用者取消'])

    def test_楽観ロックの再試行は最新カウントへ一度だけ加算する(self):
        def retry_callback(task_id, builder):
            update = builder(copy.deepcopy(self.task))
            if 'current_batch' in update:
                self.task.update(processed_members=5, successful_members=3, failed_members=2)
            return self.update_task(task_id, builder)

        self.db._state_store.update_group_message_task.side_effect = retry_callback
        self.assertEqual(self.invoke()[1], 200)

        self.assertEqual(self.task['processed_members'], 7)
        self.assertEqual(self.task['successful_members'], 5)
        self.assertEqual(self.task['failed_members'], 2)

    def test_checkpointのcallback再試行で先行更新を二重集計しない(self):
        def retry_after_checkpoint(task_id, builder):
            update = builder(copy.deepcopy(self.task))
            if 'current_batch' in update:
                self.task.update(current_batch=1, processed_members=2,
                                 successful_members=2, error_messages=['保存済み概要'])
            return self.update_task(task_id, builder)

        self.db._state_store.update_group_message_task.side_effect = retry_after_checkpoint
        self.assertEqual(self.invoke()[1], 200)
        self.assertEqual(self.task['successful_members'], 2)
        self.assertEqual(self.task['processed_members'], 2)
        self.assertEqual(self.task['error_messages'], ['保存済み概要'])
        self.assertEqual(self.updates[-1], {})
        self.module.task_client.create_task.assert_not_called()

    def test_checkpointのcallback再試行で取消を上書きしない(self):
        def retry_after_cancel(task_id, builder):
            update = builder(copy.deepcopy(self.task))
            if 'current_batch' in update:
                self.task.update(status=self.db.STATUS_ABORTED,
                                 error_messages=['利用者取消'])
            return self.update_task(task_id, builder)

        self.db._state_store.update_group_message_task.side_effect = retry_after_cancel
        response, status = self.invoke()
        self.assertEqual(status, 200)
        self.assertEqual(response['status'], self.db.STATUS_ABORTED)
        self.assertEqual(self.task['error_messages'], ['利用者取消'])
        self.assertEqual(self.task['current_batch'], 0)
        self.assertEqual(self.updates[-1], {})
        self.module.task_client.create_task.assert_not_called()

    def test_実行状態保存と送信の間の取消も正常skipする(self):
        def cancel_before_status_update(task_id, builder):
            self.task.update(status=self.db.STATUS_ABORTED)
            return self.update_task(task_id, builder)

        self.db._state_store.update_group_message_task.side_effect = cancel_before_status_update
        response, status = self.invoke()
        self.assertEqual(status, 200)
        self.assertEqual(response['status'], self.db.STATUS_ABORTED)
        self.manager._process_batch_members.assert_not_called()
        self.module.task_client.create_task.assert_not_called()

    def test_さらに先まで保存済みの古いbatchはskipする(self):
        self.task.update(status=self.db.STATUS_RUNNING, current_batch=2, total_batches=3)
        self.assertEqual(self.invoke()[1], 200)
        self.manager._process_batch_members.assert_not_called()
        self.module.task_client.create_task.assert_not_called()
        self.assertEqual(self.updates, [])

    def test_次batchを先に呼んでも未保存結果を飛ばさない(self):
        self.assertEqual(self.invoke(1)[1], 500)
        self.manager._process_batch_members.assert_not_called()
        self.module.task_client.create_task.assert_not_called()
        self.assertEqual(self.task['current_batch'], 0)

    def test_最終batchは端数も含む集計とcompletedを同じ更新で保存する(self):
        self.module.users.get_group_members.return_value = [
            SerializedMember(f'mock-line:user-{index}') for index in range(3)]
        self.assertEqual(self.invoke()[1], 200)
        self.assertEqual(self.invoke(1)[1], 200)
        final_update = self.updates[-1]
        self.assertEqual(final_update['status'], self.db.STATUS_COMPLETED)
        self.assertEqual(final_update['current_batch'], 2)
        self.assertEqual(final_update['processed_members'], 3)
        self.assertEqual(final_update['successful_members'], 3)
        self.assertEqual(self.module.task_client.create_task.call_count, 1)
        self.assertEqual(self.invoke(1)[1], 200)
        self.assertEqual(len(self.sent), 3)

    def test_最終checkpoint保存応答喪失後もcompletedを失敗へ戻さない(self):
        self.task.update(status=self.db.STATUS_RUNNING, current_batch=1,
                         processed_members=2, successful_members=2)

        def lose_final_ack(task_id, builder):
            update = builder(copy.deepcopy(self.task))
            self.update_task(task_id, builder)
            if update.get('status') == self.db.STATUS_COMPLETED:
                raise StateStoreError('最終保存応答喪失')
            return True

        self.db._state_store.update_group_message_task.side_effect = lose_final_ack
        self.assertEqual(self.invoke(1)[1], 500)
        self.assertEqual(self.task['status'], self.db.STATUS_COMPLETED)
        self.assertEqual(self.invoke(1)[1], 200)
        self.assertEqual(self.task['successful_members'], 4)
        self.assertEqual(len(self.sent), 2)

    def test_失敗者一覧はcallbackの外で保存し概要を丸める(self):
        member_id = 'mock-line:user-0'
        self.manager._process_batch_members.side_effect = (
            lambda *_args: (1, 1, ['mock-line:user-1'], [(member_id, '失敗' * 2000, 0)]))
        events = []
        original_update = self.update_task

        def track_update(task_id, builder):
            events.append('state')
            return original_update(task_id, builder)

        self.db._state_store.update_group_message_task.side_effect = track_update
        self.db._object_store.store_private.side_effect = (
            lambda *_args: events.append('object'))
        self.assertEqual(self.invoke()[1], 200)
        self.assertEqual(events, ['state', 'object', 'state'])
        self.assertEqual(self.task['failed_members'], 1)
        self.assertEqual(len(self.task['error_messages'][0]), 2000)
        self.assertTrue(self.task['error_messages'][0].endswith('…（省略）'))

    def test_失敗者再送は保存済み失敗者だけを処理する(self):
        self.task['is_retry'] = True
        self.db.get_members_from_storage = Mock(return_value=['mock-line:failed-user'])
        self.assertEqual(self.invoke()[1], 200)
        self.assertEqual(self.sent, ['mock-line:failed-user'])
        self.module.users.get_group_members.assert_not_called()
        self.assertEqual(self.task['status'], self.db.STATUS_COMPLETED)

    def test_空groupの完了保存も取消を上書きしない(self):
        self.module.users.get_group_members.return_value = []

        def cancel_empty_task(task_id, builder):
            update = builder(copy.deepcopy(self.task))
            if update.get('status') == self.db.STATUS_COMPLETED:
                self.task['status'] = self.db.STATUS_ABORTED
            return self.update_task(task_id, builder)

        self.db._state_store.update_group_message_task.side_effect = cancel_empty_task
        response, status = self.invoke()
        self.assertEqual(status, 200)
        self.assertEqual(self.task['status'], self.db.STATUS_ABORTED)
        self.assertEqual(response['status'], self.db.STATUS_ABORTED)
        self.assertEqual(self.task['current_batch'], 0)
        self.assertEqual(self.task['error_messages'], [])


class GcpGroupBatchCheckpointIntegrationTest(GroupCheckpointTestBase):
    def setUp(self):
        super().setUp()
        from cloud_backend.gcp.state_store import GcpStateStore
        from tests.test_gcp_state_store_contract import _MemoryFirestoreClient

        self.before_checkpoint_commit = None
        self.transaction_retries = 0
        server_timestamp = object()
        firestore = types.SimpleNamespace(
            SERVER_TIMESTAMP=server_timestamp,
            transactional=self._transactional,
        )
        self.client = _MemoryFirestoreClient(server_timestamp)
        with patch(
                'cloud_backend.gcp.state_store.importlib.import_module',
                return_value=firestore):
            self.store = GcpStateStore(client=self.client)
        self.store.create_group_message_task('message-1', self.task)
        self.db._state_store = self.store

    def _transactional(self, function):
        def transaction_runner(transaction):
            # 最初の更新を未commitのまま破棄し、同じcallbackを最新documentで再実行する。
            pending = []
            transaction.update = lambda reference, data: pending.append((reference, data))
            result = function(transaction)
            if self.before_checkpoint_commit is not None:
                for reference, data in pending:
                    if 'current_batch' in data:
                        self.before_checkpoint_commit(reference)
                        self.before_checkpoint_commit = None
                        self.transaction_retries += 1
                        pending.clear()
                        result = function(transaction)
                        break
            for reference, data in pending:
                reference.update(data)
            return result

        return transaction_runner

    def test_子が先行完了してもFirestoreの結果を親が巻き戻さない(self):
        def run_child(**kwargs):
            saved = self.store.get_group_message_task('message-1')
            self.assertEqual((saved['current_batch'], saved['processed_members']), (1, 2))
            self.assertEqual(self.invoke(kwargs['params']['batch_index'])[1], 200)

        self.module.task_client.create_task.side_effect = run_child
        self.assertEqual(self.invoke()[1], 200)
        saved = self.store.get_group_message_task('message-1')
        self.assertEqual(saved['status'], self.db.STATUS_COMPLETED)
        self.assertEqual((saved['current_batch'], saved['processed_members']), (2, 4))
        self.assertEqual(saved['successful_members'], 4)
        self.assertEqual(len(self.sent), 4)

    def test_同じbatchの重複送信から次batchの二重登録を連鎖させない(self):
        members = [SerializedMember(f'mock-line:user-{index}') for index in range(6)]
        self.module.users.get_group_members.return_value = members
        self.store.update_group_message_task('message-1', lambda _data: {
            'total_members': 6, 'total_batches': 3,
        })
        pending = []
        self.module.task_client.create_task.side_effect = (
            lambda **kwargs: pending.append(kwargs['params']['batch_index']))
        overlapped = False

        def send_overlapping_batch(*args):
            nonlocal overlapped
            result = self.send_members(*args)
            if not overlapped:
                overlapped = True
                self.assertEqual(self.invoke(0)[1], 200)
            return result

        self.manager._process_batch_members.side_effect = send_overlapping_batch
        self.assertEqual(self.invoke(0)[1], 200)

        self.assertEqual(pending, [1])
        saved = self.store.get_group_message_task('message-1')
        self.assertEqual((saved['current_batch'], saved['processed_members']), (1, 2))
        processed_batches = []
        while pending:
            batch_index = pending.pop(0)
            processed_batches.append(batch_index)
            self.assertEqual(self.invoke(batch_index)[1], 200)

        self.assertEqual(processed_batches, [1, 2])
        saved = self.store.get_group_message_task('message-1')
        self.assertEqual(saved['status'], self.db.STATUS_COMPLETED)
        self.assertEqual((saved['current_batch'], saved['processed_members']), (3, 6))
        self.assertEqual(saved['successful_members'], 6)
        for index, member in enumerate(members):
            self.assertEqual(self.sent.count(member.serialize()), 2 if index < 2 else 1)

    def test_CloudTasks登録失敗後は送信せず登録だけ再開する(self):
        self.module.task_client.create_task.side_effect = RuntimeError('Cloud Tasks登録失敗')
        self.assertEqual(self.invoke()[1], 500)
        saved = self.store.get_group_message_task('message-1')
        self.assertEqual(saved['status'], self.db.STATUS_FAILED)
        self.assertEqual(saved['current_batch'], 1)
        self.module.task_client.create_task.side_effect = None
        self.assertEqual(self.invoke()[1], 200)
        saved = self.store.get_group_message_task('message-1')
        self.assertEqual(saved['successful_members'], 2)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.module.task_client.create_task.call_count, 2)

    def test_Firestoreの予約登録失敗も再登録成功時だけPendingへ戻す(self):
        self.store.update_group_message_task('message-1', lambda _data: {
            'scheduled_at': datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5),
        })
        self.module.task_client.create_task.side_effect = RuntimeError('予約登録失敗')
        self.assertEqual(self.invoke()[1], 500)
        saved = self.store.get_group_message_task('message-1')
        self.assertEqual(saved['status'], self.db.STATUS_FAILED)
        self.module.task_client.create_task.side_effect = None
        self.assertEqual(self.invoke()[1], 200)
        saved = self.store.get_group_message_task('message-1')
        self.assertEqual(saved['status'], self.db.STATUS_PENDING)
        self.assertEqual(saved['current_batch'], 0)
        self.assertEqual(saved['processed_members'], 0)
        self.assertIn('予約登録失敗', saved['error_messages'][0])
        self.assertEqual(self.sent, [])

    def test_Firestore_callback再試行は最新カウントへ加算する(self):
        self.before_checkpoint_commit = lambda reference: reference.update({
            'processed_members': 5, 'successful_members': 3, 'failed_members': 2,
        })
        self.assertEqual(self.invoke()[1], 200)
        saved = self.store.get_group_message_task('message-1')
        self.assertEqual(self.transaction_retries, 1)
        self.assertEqual(saved['processed_members'], 7)
        self.assertEqual(saved['successful_members'], 5)
        self.assertEqual(saved['failed_members'], 2)
        self.module.task_client.create_task.assert_called_once()

    def test_Firestore_callback再試行は保存済みbatchを二重集計しない(self):
        self.before_checkpoint_commit = lambda reference: reference.update({
            'current_batch': 1, 'processed_members': 2, 'successful_members': 2,
            'error_messages': ['保存済み概要'],
        })
        self.assertEqual(self.invoke()[1], 200)
        saved = self.store.get_group_message_task('message-1')
        self.assertEqual(self.transaction_retries, 1)
        self.assertEqual(saved['current_batch'], 1)
        self.assertEqual(saved['successful_members'], 2)
        self.assertEqual(saved['processed_members'], 2)
        self.assertEqual(saved['error_messages'], ['保存済み概要'])
        self.module.task_client.create_task.assert_not_called()

    def test_Firestore_callback再試行は取消状態と概要を上書きしない(self):
        self.before_checkpoint_commit = lambda reference: reference.update({
            'status': self.db.STATUS_ABORTED, 'error_messages': ['利用者取消'],
        })
        response, status = self.invoke()
        saved = self.store.get_group_message_task('message-1')
        self.assertEqual(status, 200)
        self.assertEqual(response['status'], self.db.STATUS_ABORTED)
        self.assertEqual(self.transaction_retries, 1)
        self.assertEqual(saved['status'], self.db.STATUS_ABORTED)
        self.assertEqual(saved['current_batch'], 0)
        self.assertEqual(saved['error_messages'], ['利用者取消'])
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
            'status': self.db.STATUS_COMPLETED,
        }
        self.state_store.get_group_message_task.return_value = original_task
        with (
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

        self.state_store.get_group_message_task.side_effect = lambda _task_id: copy.deepcopy(task)
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

    def test_一覧と詳細は同じIDと状態別の操作可否を返し保存値を変えない(self):
        cases = (
            (self.db.STATUS_PENDING, True, False),
            (self.db.STATUS_RUNNING, True, False),
            (self.db.STATUS_FAILED, True, False),
            (self.db.STATUS_COMPLETED, False, True),
            (self.db.STATUS_ABORTED, False, True),
        )
        for status, can_abort, can_retry in cases:
            for failed in (0, 2):
                with self.subTest(status=status, failed=failed):
                    stored = {'status': status, 'failed_members': failed}
                    self.state_store.get_group_message_task.return_value = stored
                    self.state_store.get_recent_group_message_tasks.return_value = [
                        dict(stored, id='task-1')]

                    detail = self.db.get_task('task-1')
                    recent = self.db.get_recent_tasks('test-bot')

                    self.assertEqual(detail, recent[0])
                    self.assertEqual(detail['id'], 'task-1')
                    self.assertIs(detail['can_abort'], can_abort)
                    self.assertIs(detail['can_retry'], can_retry and failed > 0)
                    self.assertEqual(stored, {'status': status, 'failed_members': failed})

        self.state_store.get_group_message_task.return_value = None
        self.assertIsNone(self.db.get_task('missing'))

    def test_取消はtransaction内の最新状態に従う(self):
        for status in (
                self.db.STATUS_PENDING, self.db.STATUS_RUNNING,
                self.db.STATUS_FAILED, self.db.STATUS_COMPLETED,
                self.db.STATUS_ABORTED):
            with self.subTest(status=status):
                task = {'status': status, 'error_messages': ['送信結果']}
                updates = []

                def update_task(_task_id, builder):
                    updates.append(builder(dict(task)))
                    task.update(updates[-1])
                    return True

                self.state_store.update_group_message_task.side_effect = update_task
                result = self.db.abort_task('task-1')

                can_abort = status in (
                    self.db.STATUS_PENDING, self.db.STATUS_RUNNING, self.db.STATUS_FAILED)
                self.assertIs(result, can_abort)
                self.assertEqual(task['status'], self.db.STATUS_ABORTED if can_abort else status)
                self.assertEqual(task['error_messages'], ['送信結果'])
                self.assertEqual(updates, [{'status': self.db.STATUS_ABORTED}] if can_abort else [{}])
        self.state_store.get_group_message_task.assert_not_called()

    def test_取消callback再試行で完了したtaskを上書きせず成功と返さない(self):
        updates = []

        def update_task(_task_id, builder):
            updates.append(builder({'status': self.db.STATUS_RUNNING}))
            updates.append(builder({'status': self.db.STATUS_COMPLETED}))
            return True

        self.state_store.update_group_message_task.side_effect = update_task

        self.assertIs(self.db.abort_task('task-1'), False)
        self.assertEqual(updates, [{'status': self.db.STATUS_ABORTED}, {}])

    def test_不存在のtaskは取消できない(self):
        self.state_store.update_group_message_task.return_value = False

        self.assertIs(self.db.abort_task('missing'), False)

    def test_配信継続中や失敗者ゼロのtaskは再送を作らない(self):
        for status, failed in (
                (self.db.STATUS_PENDING, 2), (self.db.STATUS_RUNNING, 2),
                (self.db.STATUS_FAILED, 2), (self.db.STATUS_COMPLETED, 0),
                (self.db.STATUS_ABORTED, 0)):
            with self.subTest(status=status, failed=failed):
                self.state_store.get_group_message_task.return_value = {
                    'status': status, 'failed_members': failed,
                }

                self.assertIsNone(self.db.retry_failed_members('task-1', 'dashboard'))

        self.object_store.load_private.assert_not_called()
        self.object_store.store_private.assert_not_called()
        self.state_store.create_group_message_task.assert_not_called()

    def test_失敗一覧のNotFoundだけは空と扱い初回の追記を保存できる(self):
        self.object_store.load_private.side_effect = ObjectNotFoundError('missing')

        self.assertEqual(self.db._get_failed_members_from_storage('task-1'), [])
        self.db._append_failed_member_list('task-1', ['new-user'])

        self.object_store.store_private.assert_called_once_with(
            'group_tasks/task-1/failed_members.json', json.dumps(['new-user']))

    def test_失敗一覧の取得や復号やJSON障害は旧一覧を上書きせず再送を作らない(self):
        self.state_store.get_group_message_task.return_value = {
            'status': self.db.STATUS_COMPLETED, 'failed_members': 2,
        }
        cases = (
            (ObjectStoreError('読み取り失敗'), ObjectStoreError),
            (b'\xff', UnicodeDecodeError),
            (b'broken JSON', json.JSONDecodeError),
            (b'{"user": 1}', ValueError),
            (b'["old-user", 1]', ValueError),
        )
        for content, exception_type in cases:
            with self.subTest(content=repr(content)):
                self.object_store.load_private.side_effect = (
                    content if isinstance(content, Exception) else None)
                self.object_store.load_private.return_value = content

                with self.assertRaises(exception_type):
                    self.db._append_failed_member_list('task-1', ['new-user'])
                with self.assertRaises(exception_type):
                    self.db.retry_failed_members('task-1', 'dashboard')

                self.object_store.store_private.assert_not_called()
                self.state_store.create_group_message_task.assert_not_called()

    def test_result_JSON取得失敗は空listを返す(self):
        getters = (
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
