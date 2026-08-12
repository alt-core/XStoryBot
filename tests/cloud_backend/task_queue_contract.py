"""GCPとAWSのTaskQueueへ同じ意味論を適用する契約テスト。"""

from dataclasses import dataclass
from unittest.mock import patch
import uuid

from cloud_backend.contracts import TaskQueueError


@dataclass(frozen=True)
class CapturedTask:
    """クラウド固有requestから読み取った共通の登録結果。"""

    task_id: str
    params: dict
    delayed: bool
    raw_body: str


class TaskQueueContractMixin:
    """具象adapterの実transportを正規化して共通契約を検証する。"""

    def create_contract_queue(self):
        raise NotImplementedError

    def capture_task(self, queue_name, url, params, delay_seconds=None):
        raise NotImplementedError

    def set_transport_error(self, error):
        raise NotImplementedError

    def make_sdk_error(self):
        raise NotImplementedError

    def contract_secret_values(self):
        return ()

    def setUp(self):
        super().setUp()
        self.create_contract_queue()

    def test_UUIDとparameter上書きと元辞書不変を共通化する(self):
        params = {
            'user': 'mock:user-1',
            'action': '日本語のaction',
            'empty': '',
            'task_id': 'caller-task-id',
        }
        original = dict(params)

        captured = self.capture_task(
            'action-queue', '/api/v1/bots/bot/action', params)

        self.assertEqual(original, params)
        self.assertEqual(str(uuid.UUID(captured.task_id)), captured.task_id)
        self.assertEqual(captured.task_id, captured.params['task_id'])
        self.assertNotEqual('caller-task-id', captured.params['task_id'])
        self.assertEqual('mock:user-1', captured.params['user'])
        self.assertEqual('日本語のaction', captured.params['action'])
        self.assertEqual('', captured.params['empty'])

    def test_actionとgroupの共通parameter契約を維持する(self):
        cases = (
            (
                'action-queue',
                '/api/v1/bots/bot/action',
                {'user': 'mock:user-1', 'action': 'notice'},
            ),
            (
                'group-message-queue',
                '/api/v1/bots/bot/process_group_batch',
                {'message_task_id': 'message-1', 'batch_index': '2'},
            ),
        )

        for queue_name, url, params in cases:
            with self.subTest(queue_name=queue_name):
                captured = self.capture_task(queue_name, url, params)
                for key, value in params.items():
                    self.assertEqual(value, captured.params[key])
                self.assertEqual(captured.task_id, captured.params['task_id'])

    def test_Noneと0は即時で正の値だけを遅延扱いにする(self):
        cases = (
            (None, False),
            (0, False),
            (30, True),
        )

        for delay_seconds, expected_delayed in cases:
            with self.subTest(delay_seconds=delay_seconds):
                captured = self.capture_task(
                    'action-queue',
                    '/api/v1/bots/bot/action',
                    {'user': 'mock:user-1', 'action': 'notice'},
                    delay_seconds=delay_seconds,
                )
                self.assertIs(expected_delayed, captured.delayed)

    def test_SDK例外だけを共通例外へ変換する(self):
        self.set_transport_error(self.make_sdk_error())

        with self.assertRaises(TaskQueueError):
            self.capture_task(
                'action-queue',
                '/api/v1/bots/bot/action',
                {'user': 'mock:user-1', 'action': 'notice'},
            )

    def test_application例外は同じinstanceを伝播する(self):
        error = RuntimeError('application error')
        self.set_transport_error(error)

        with self.assertRaises(RuntimeError) as raised:
            self.capture_task(
                'action-queue',
                '/api/v1/bots/bot/action',
                {'user': 'mock:user-1', 'action': 'notice'},
            )

        self.assertIs(error, raised.exception)

    def test_本文とlogへparameterや認証値を追加しない(self):
        secret_parameter = 'secret-common-parameter'
        with patch.object(self.contract_module.logging, 'info') as log_info:
            captured = self.capture_task(
                'action-queue',
                '/api/v1/bots/bot/action',
                {'user': 'mock:user-1', 'action': secret_parameter},
            )

        body_lower = captured.raw_body.lower()
        self.assertNotIn('x-api-token', body_lower)
        self.assertNotIn('api_token', body_lower)
        messages = ' '.join(
            str(value)
            for log_call in log_info.call_args_list
            for value in log_call.args
        )
        self.assertNotIn(secret_parameter, messages)
        for secret in self.contract_secret_values():
            self.assertNotIn(secret, captured.raw_body)
            self.assertNotIn(secret, messages)
