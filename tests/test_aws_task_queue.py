import copy
import json
import unittest
import uuid
from unittest.mock import Mock, patch

import boto3
from botocore.stub import Stubber

from cloud_backend import aws as aws_backend
from cloud_backend.aws.task_queue import AwsTaskQueue
from cloud_backend.contracts import TaskQueueError


AWS_SETTINGS = {
    'region': 'ap-northeast-1',
    'task_queue': {
        'functions': {
            'action-queue': 'test-action-worker',
            'group-message-queue': 'test-group-worker',
        },
    },
}
TASK_UUID = uuid.UUID('12345678-1234-5678-1234-567812345678')


def make_client():
    return boto3.client(
        'lambda', region_name='ap-northeast-1',
        aws_access_key_id='test-access-key',
        aws_secret_access_key='test-secret-key',
        aws_session_token='test-session-token',
    )


def expected_body(params=None, queue_name='action-queue', bot_name='bot'):
    if params is None:
        params = {'value': '1'}
    body_params = dict(params)
    body_params['task_id'] = str(TASK_UUID)
    return json.dumps({
        'version': 1,
        'task_id': str(TASK_UUID),
        'queue_name': queue_name,
        'kind': 'action' if queue_name == 'action-queue' else 'group_batch',
        'bot_name': bot_name,
        'params': body_params,
    }, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


class AwsTaskQueueTest(unittest.TestCase):
    def make_queue(self, client_factory=None, settings=AWS_SETTINGS):
        queue = AwsTaskQueue(
            client_factory=client_factory,
            uuid_factory=lambda: TASK_UUID,
        )
        queue.initialize(settings)
        return queue

    def test_SDK_clientは初回登録まで生成しない(self):
        client = Mock()
        client.invoke.return_value = {'StatusCode': 202}
        client_factory = Mock(return_value=client)
        queue = self.make_queue(client_factory=client_factory)
        self.assertFalse(queue.allows_delayed_scenarios)
        self.assertTrue(queue.defer_until_response)
        client_factory.assert_not_called()
        for value in ('1', '2'):
            queue.create_task(
                'action-queue', '/api/v1/bots/bot/action', {'value': value})
        client_factory.assert_called_once_with(
            'lambda', region_name='ap-northeast-1')

    def test_即時Taskはparamsを変更せずLambdaへ非同期登録する(self):
        client = make_client()
        stubber = Stubber(client)
        params = {'value': '日本語', 'task_id': 'caller-task-id'}
        body = expected_body(params=params)
        stubber.add_response('invoke', {'StatusCode': 202}, {
            'FunctionName': 'test-action-worker',
            'InvocationType': 'Event',
            'Payload': body,
        })
        queue = self.make_queue(
            client_factory=lambda service_name, **options: client)
        with stubber:
            task_id = queue.create_task(
                'action-queue', '/api/v1/bots/bot/action', params,
                delay_seconds=0)
        self.assertEqual(str(TASK_UUID), task_id)
        self.assertEqual(
            {'value': '日本語', 'task_id': 'caller-task-id'}, params)
        self.assertEqual(str(TASK_UUID), json.loads(body)['params']['task_id'])
        self.assertNotIn(b'token', body.lower())
        self.assertNotIn(b'credential', body.lower())

    def test_groupは専用workerへ同じ封筒で登録する(self):
        client = make_client()
        stubber = Stubber(client)
        params = {'message_task_id': 'message-1', 'batch_index': '2'}
        stubber.add_response('invoke', {'StatusCode': 202}, {
            'FunctionName': 'test-group-worker',
            'InvocationType': 'Event',
            'Payload': expected_body(
                params, 'group-message-queue', 'test-bot'),
        })
        queue = self.make_queue(
            client_factory=lambda service_name, **options: client)
        with stubber:
            queue.create_task(
                'group-message-queue',
                '/api/v1/bots/test-bot/process_group_batch', params)

    def test_Noneと数値ゼロだけを即時実行として許可する(self):
        client = Mock()
        client.invoke.return_value = {'StatusCode': 202}
        queue = self.make_queue(
            client_factory=lambda service_name, **options: client)
        for delay in (None, 0, 0.0):
            with self.subTest(delay=delay):
                queue.create_task(
                    'action-queue', '/api/v1/bots/bot/action', {}, delay)
        self.assertEqual(3, client.invoke.call_count)

    def test_ゼロ以外の遅延はclient生成前に拒否する(self):
        client_factory = Mock()
        queue = self.make_queue(client_factory=client_factory)
        for delay in (-1, -0.1, 0.1, 30, True, False, '0', float('nan')):
            with self.subTest(delay=delay):
                with self.assertRaises(TaskQueueError):
                    queue.create_task(
                        'action-queue', '/api/v1/bots/bot/action', {}, delay)
        client_factory.assert_not_called()

    def test_202以外や応答欠落は登録失敗として扱う(self):
        client = Mock()
        queue = self.make_queue(
            client_factory=lambda service_name, **options: client)
        for response in ({'StatusCode': 200}, {'StatusCode': 500}, {}):
            with self.subTest(response=response):
                client.invoke.return_value = response
                with self.assertRaises(TaskQueueError) as raised:
                    queue.create_task(
                        'action-queue', '/api/v1/bots/bot/action', {})
                self.assertEqual(str(TASK_UUID), raised.exception.task_id)

    def test_初期化時に両worker名を必須にする(self):
        for queue_name in ('action-queue', 'group-message-queue'):
            for value in (None, '', ' ', 1, {}, []):
                with self.subTest(queue_name=queue_name, value=value):
                    settings = copy.deepcopy(AWS_SETTINGS)
                    settings['task_queue']['functions'][queue_name] = value
                    client_factory = Mock()
                    with self.assertRaises(ValueError):
                        self.make_queue(client_factory, settings)
                    client_factory.assert_not_called()
            settings = copy.deepcopy(AWS_SETTINGS)
            del settings['task_queue']['functions'][queue_name]
            with self.assertRaises(ValueError):
                self.make_queue(settings=settings)
        for functions in (None, '', []):
            with self.assertRaises(ValueError):
                self.make_queue(settings={'task_queue': {'functions': functions}})

    def test_build_queueと未知のqueueを拒否する(self):
        client_factory = Mock()
        queue = self.make_queue(client_factory=client_factory)
        for name in ('build-queue', 'missing-queue'):
            with self.subTest(name=name), self.assertRaises(TaskQueueError):
                queue.create_task(name, '/api/v1/bots/bot/action', {})
        client_factory.assert_not_called()

    def test_不正pathやBot名を拒否する(self):
        client_factory = Mock()
        queue = self.make_queue(client_factory=client_factory)
        invalid_destinations = (
            ('action-queue', '/api/v1/bots//action'),
            ('action-queue', '/api/v1/bots/a%2Fb/action'),
            ('action-queue', '/api/v1/bots/a/b/action'),
            ('action-queue', '/api/v1/bots/日本語/action'),
            ('action-queue', '/api/v1/bots/a.b/action'),
            ('action-queue', '/api/v1/bots/bot/process_group_batch'),
            ('group-message-queue', '/api/v1/bots/bot/action'),
        )
        for queue_name, path in invalid_destinations:
            with self.subTest(queue_name=queue_name, path=path):
                with self.assertRaises(TaskQueueError):
                    queue.create_task(queue_name, path, {})
        client_factory.assert_not_called()

    def test_AWS_SDK例外だけを共通例外へ変換する(self):
        client = make_client()
        stubber = Stubber(client)
        stubber.add_client_error(
            'invoke', service_error_code='ServiceUnavailable',
            service_message='unavailable', http_status_code=503,
            expected_params={
                'FunctionName': 'test-action-worker',
                'InvocationType': 'Event', 'Payload': expected_body(),
            },
        )
        queue = self.make_queue(
            client_factory=lambda service_name, **options: client)
        with stubber, self.assertRaises(TaskQueueError) as raised:
            queue.create_task(
                'action-queue', '/api/v1/bots/bot/action', {'value': '1'})
        self.assertEqual(
            'AWS非同期タスクの登録に失敗しました', str(raised.exception))
        self.assertNotIn('unavailable', str(raised.exception))
        self.assertEqual(str(TASK_UUID), raised.exception.task_id)
        application_error = RuntimeError('application error')
        client = Mock()
        client.invoke.side_effect = application_error
        queue = self.make_queue(
            client_factory=lambda service_name, **options: client)
        with self.assertRaises(RuntimeError) as raised:
            queue.create_task(
                'action-queue', '/api/v1/bots/bot/action', {'value': '1'})
        self.assertIs(application_error, raised.exception)
        self.assertEqual(str(TASK_UUID), raised.exception.task_id)

    def test_client生成失敗にも生成済みのtask_idを添える(self):
        from botocore.exceptions import EndpointConnectionError
        client_factory = Mock(side_effect=EndpointConnectionError(
            endpoint_url='https://test.invalid'))
        queue = self.make_queue(client_factory=client_factory)
        with self.assertRaises(TaskQueueError) as raised:
            queue.create_task('action-queue', '/api/v1/bots/bot/action', {})
        self.assertEqual(str(TASK_UUID), raised.exception.task_id)

    def test_task_id生成前の検査失敗にはIDを付けない(self):
        queue = self.make_queue(client_factory=Mock())
        queue._uuid_factory = Mock(return_value=TASK_UUID)
        with self.assertRaises(TaskQueueError) as raised:
            queue.create_task(
                'action-queue', '/api/v1/bots/bot/action', {}, delay_seconds=1)
        self.assertFalse(hasattr(raised.exception, 'task_id'))
        queue._uuid_factory.assert_not_called()

    def test_ログへparameterやAWS設定値を出さない(self):
        client = Mock()
        client.invoke.return_value = {'StatusCode': 202}
        queue = self.make_queue(
            client_factory=lambda service_name, **options: client)
        with patch(
                'cloud_backend.aws.task_queue.logging.info') as log_info:
            queue.create_task(
                'action-queue', '/api/v1/bots/bot/action',
                {'value': 'secret-parameter'})
        messages = ' '.join(
            str(value) for log_call in log_info.call_args_list
            for value in log_call.args)
        self.assertNotIn('secret-parameter', messages)
        self.assertNotIn('/api/v1/bots/bot/action', messages)
        self.assertNotIn('test-action-worker', messages)


class AwsTaskQueueFactoryTest(unittest.TestCase):
    def test_provider内ではTaskQueueを共有する(self):
        original = getattr(aws_backend, '_task_queue', None)
        aws_backend._task_queue = None
        task_queue = Mock()
        try:
            with patch(
                    'cloud_backend.aws.task_queue.AwsTaskQueue',
                    return_value=task_queue) as constructor:
                first = aws_backend.create_task_queue()
                second = aws_backend.create_task_queue()
        finally:
            aws_backend._task_queue = original
        self.assertIs(task_queue, first)
        self.assertIs(task_queue, second)
        constructor.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
