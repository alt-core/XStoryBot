import datetime
import unittest
from unittest.mock import Mock

from botocore.exceptions import ClientError

from cloud_backend.aws.state_store import (
    TASK_EXECUTION_BUSY,
    TASK_EXECUTION_CLAIMED,
    TASK_EXECUTION_COMPLETED,
    AwsStateStore,
)
from cloud_backend.contracts import StateConflictError, StateStoreError
from tests.test_aws_state_store import (
    AWS_SETTINGS,
    _MemoryDynamoClient,
    _MemoryObjectStore,
)


NOW = datetime.datetime(
    2026, 8, 12, 9, 0, tzinfo=datetime.timezone.utc)
TTL_SECONDS = 15 * 24 * 60 * 60


class AwsTaskExecutionStoreTest(unittest.TestCase):
    def create_store(self, client=None, clock=None):
        return AwsStateStore(
            AWS_SETTINGS,
            client=client or _MemoryDynamoClient(),
            object_store=_MemoryObjectStore(),
            clock=clock or (lambda: NOW),
        )

    @staticmethod
    def execution_items(store):
        return [
            store._decode_item(item)
            for item in store._client().tables['test-cache'].values()
            if store._decode_item(item)['pk'].startswith('TASK_EXECUTION#')
        ]

    def test_未取得taskをclaimし実行キーをhashして15日TTLを付ける(self):
        store = self.create_store()

        result = store.try_claim_task_execution(
            'action:bot:task-1', 'owner-1', 90)

        self.assertEqual(TASK_EXECUTION_CLAIMED, result)
        items = self.execution_items(store)
        self.assertEqual(1, len(items))
        self.assertEqual(TASK_EXECUTION_CLAIMED, items[0]['status'])
        self.assertEqual('owner-1', items[0]['owner'])
        self.assertEqual(int(NOW.timestamp()) + 90, items[0]['lease_until'])
        self.assertEqual(
            int(NOW.timestamp()) + TTL_SECONDS,
            items[0]['expire_at'],
        )
        self.assertNotIn('action:bot:task-1', str(items[0]))

    def test_実行中はbusyで完了後はcompletedを返す(self):
        store = self.create_store()
        key = 'group:bot:message-1:2'
        store.try_claim_task_execution(key, 'owner-1', 360)

        self.assertEqual(
            TASK_EXECUTION_BUSY,
            store.try_claim_task_execution(key, 'owner-2', 360),
        )

        store.complete_task_execution(key, 'owner-1')

        self.assertEqual(
            TASK_EXECUTION_COMPLETED,
            store.try_claim_task_execution(key, 'owner-2', 360),
        )
        item = self.execution_items(store)[0]
        self.assertEqual(TASK_EXECUTION_COMPLETED, item['status'])
        self.assertEqual(
            int(NOW.timestamp()) + TTL_SECONDS,
            item['expire_at'],
        )

    def test_lease切れ後は再取得でき古いownerは完了できない(self):
        current = [NOW]
        store = self.create_store(clock=lambda: current[0])
        key = 'action:bot:task-1'
        store.try_claim_task_execution(key, 'owner-1', 90)
        current[0] += datetime.timedelta(seconds=91)

        self.assertEqual(
            TASK_EXECUTION_CLAIMED,
            store.try_claim_task_execution(key, 'owner-2', 90),
        )
        with self.assertRaises(StateConflictError):
            store.complete_task_execution(key, 'owner-1')

        store.complete_task_execution(key, 'owner-2')

    def test_lease切れ後も再取得がなければ元ownerが完了できる(self):
        current = [NOW]
        store = self.create_store(clock=lambda: current[0])
        key = 'action:bot:task-1'
        store.try_claim_task_execution(key, 'owner-1', 90)
        current[0] += datetime.timedelta(seconds=91)

        store.complete_task_execution(key, 'owner-1')

        self.assertEqual(
            TASK_EXECUTION_COMPLETED,
            self.execution_items(store)[0]['status'],
        )

    def test_条件失敗直後に記録が消えた場合はbusyへ倒す(self):
        client = Mock()
        client.put_item.side_effect = ClientError({
            'Error': {
                'Code': 'ConditionalCheckFailedException',
                'Message': 'condition failed',
            },
        }, 'PutItem')
        client.get_item.return_value = {}
        store = self.create_store(client=client)

        result = store.try_claim_task_execution(
            'action:bot:task-1', 'owner-1', 90)

        self.assertEqual(TASK_EXECUTION_BUSY, result)
        self.assertTrue(client.get_item.call_args.kwargs['ConsistentRead'])

    def test_入力不備とSDK例外を安全な共通例外へ変換する(self):
        store = self.create_store()
        for key, owner, lease_seconds in (
                ('', 'owner', 90),
                ('key', '', 90),
                ('key', 'owner', 0),
                ('key', 'owner', True)):
            with self.subTest(
                    key=key, owner=owner, lease_seconds=lease_seconds):
                with self.assertRaises(ValueError):
                    store.try_claim_task_execution(
                        key, owner, lease_seconds)

        client = Mock()
        client.put_item.side_effect = ClientError({
            'Error': {
                'Code': 'InternalServerError',
                'Message': 'secret SDK detail',
            },
        }, 'PutItem')
        store = self.create_store(client=client)
        with self.assertRaises(StateStoreError) as raised:
            store.try_claim_task_execution('key', 'owner', 90)

        self.assertNotIn('secret SDK detail', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
