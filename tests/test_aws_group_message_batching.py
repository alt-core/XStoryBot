import unittest
from unittest.mock import Mock, patch
import uuid

from async_task_processor import TaskProcessingError
from cloud_backend.aws import task_handler
from cloud_backend.aws.state_store import AwsStateStore
from cloud_backend.contracts import StateStoreError
from tests.test_aws_state_store import AWS_SETTINGS, _MemoryDynamoClient, _MemoryObjectStore
from tests.test_group_message_batching import GroupCheckpointTestBase, SerializedMember


class AwsGroupWorkerIntegrationTest(GroupCheckpointTestBase):
    def setUp(self):
        super().setUp()
        self.execution_store = AwsStateStore(
            AWS_SETTINGS, client=_MemoryDynamoClient(), object_store=_MemoryObjectStore())
        manager = self.manager

        class SmallBatchManager(self.module.GroupMessageTaskManager):
            def __init__(self, bot_name, bot_instance=None):
                super().__init__(bot_name, bot_instance)
                self._process_batch_members = manager._process_batch_members

            def handle_batch_process_request(self, task_id, batch_index=0):
                return super().handle_batch_process_request(task_id, batch_index, batch_size=2)

        self.dependencies = {
            'worker_kind': 'group_batch', 'get_bot': Mock(return_value=Mock()),
            'user_class': Mock(), 'get_group_members': Mock(), 'options': {},
            'manager_class': SmallBatchManager, 'execution_store': self.execution_store,
        }

    def envelope(self, batch_index):
        task_id = str(uuid.uuid4())
        return {
            'version': 1, 'task_id': task_id, 'queue_name': 'group-message-queue',
            'kind': 'group_batch', 'bot_name': 'test-bot',
            'params': {'task_id': task_id, 'message_task_id': 'message-1',
                       'batch_index': batch_index},
        }

    def invoke_worker(self, envelope):
        with patch.object(task_handler, '_load_dependencies', return_value=self.dependencies):
            return task_handler.lambda_handler(envelope, None)

    def test_子先行完了と異なるUUIDでの再配信を論理batch単位で抑止する(self):
        children = []

        def invoke_child(**kwargs):
            envelope = self.envelope(kwargs['params']['batch_index'])
            children.append(envelope)
            self.assertEqual(self.invoke_worker(envelope), {'status': 'ok'})

        self.module.task_client.create_task.side_effect = invoke_child
        parent = self.envelope(0)
        self.assertEqual(self.invoke_worker(parent), {'status': 'ok'})
        self.assertEqual(self.invoke_worker(parent), {'status': 'ok'})
        self.assertEqual(self.invoke_worker(self.envelope(0)), {'status': 'ok'})
        self.assertEqual(self.invoke_worker(self.envelope(1)), {'status': 'ok'})
        self.assertEqual(self.task['status'], self.db.STATUS_COMPLETED)
        self.assertEqual(self.task['successful_members'], 4)
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(len(children), 1)

    def test_Managerとworkerを実StateStoreとMemoryDynamoで完走する(self):
        self.execution_store.create_group_message_task('message-1', self.task)
        self.db._state_store = self.execution_store

        def run_next(**kwargs):
            self.invoke_worker(self.envelope(kwargs['params']['batch_index']))

        self.module.task_client.create_task.side_effect = run_next
        self.assertEqual(self.invoke_worker(self.envelope(0)), {'status': 'ok'})
        self.assertEqual(self.invoke_worker(self.envelope(1)), {'status': 'ok'})
        saved_task = self.execution_store.get_group_message_task('message-1')
        self.assertEqual(saved_task['status'], self.db.STATUS_COMPLETED)
        self.assertEqual(saved_task['current_batch'], 2)
        self.assertEqual(saved_task['processed_members'], 4)
        self.assertEqual(saved_task['successful_members'], 4)
        self.assertEqual(len(self.sent), 4)

    def test_invoke応答喪失後に再投入した子は別UUIDでも再送しない(self):
        children = []

        def lose_invoke_ack(**kwargs):
            envelope = self.envelope(kwargs['params']['batch_index'])
            children.append(envelope)
            if len(children) == 1:
                raise RuntimeError('登録応答喪失')

        self.module.task_client.create_task.side_effect = lose_invoke_ack
        parent = self.envelope(0)
        with self.assertRaises(TaskProcessingError) as raised:
            self.invoke_worker(parent)
        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(self.task['status'], self.db.STATUS_FAILED)
        self.assertEqual(self.invoke_worker(parent), {'status': 'ok'})
        self.assertEqual(len(children), 2)
        for envelope in children:
            self.assertEqual(self.invoke_worker(envelope), {'status': 'ok'})
        self.assertEqual(self.task['status'], self.db.STATUS_COMPLETED)
        self.assertEqual(self.task['successful_members'], 4)
        self.assertEqual(len(self.sent), 4)

    def test_実行完了記録の失敗後もcheckpointから登録だけを再開する(self):
        complete = self.execution_store.complete_task_execution
        attempts = [0]

        def fail_first_complete(*args):
            attempts[0] += 1
            if attempts[0] == 1:
                raise StateStoreError('実行完了記録失敗')
            return complete(*args)

        self.execution_store.complete_task_execution = fail_first_complete
        parent = self.envelope(0)
        with self.assertRaises(StateStoreError):
            self.invoke_worker(parent)
        self.assertEqual(self.invoke_worker(parent), {'status': 'ok'})
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.task['successful_members'], 2)
        self.assertEqual(self.module.task_client.create_task.call_count, 2)

    def test_17batchの連鎖も最終batchで有限に完了する(self):
        self.task.update(total_members=34, total_batches=17)
        self.module.users.get_group_members.return_value = [
            SerializedMember(f'mock-line:user-{index}') for index in range(34)]
        batches = []

        def run_next(**kwargs):
            batch_index = kwargs['params']['batch_index']
            batches.append(batch_index)
            self.invoke_worker(self.envelope(batch_index))

        self.module.task_client.create_task.side_effect = run_next
        self.assertEqual(self.invoke_worker(self.envelope(0)), {'status': 'ok'})
        self.assertEqual(batches, list(range(1, 17)))
        self.assertEqual(self.task['status'], self.db.STATUS_COMPLETED)
        self.assertEqual(self.task['current_batch'], 17)
        self.assertEqual(self.task['processed_members'], 34)
        self.assertEqual(len(self.sent), 34)


if __name__ == '__main__':
    unittest.main()
