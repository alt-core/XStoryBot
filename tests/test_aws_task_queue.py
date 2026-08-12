import copy
import datetime
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
        'queues': {
            'action-queue': {
                'url': (
                    'https://sqs.ap-northeast-1.amazonaws.com/'
                    '000000000000/action'),
                'arn': 'arn:aws:sqs:ap-northeast-1:000000000000:action',
            },
            'group-message-queue': {
                'url': (
                    'https://sqs.ap-northeast-1.amazonaws.com/'
                    '000000000000/group'),
                'arn': 'arn:aws:sqs:ap-northeast-1:000000000000:group',
            },
        },
        'scheduler': {
            'role_arn': (
                'arn:aws:iam::000000000000:role/test-scheduler-role'),
            'group_name': 'test-scheduler-group',
        },
    },
}

TASK_UUID = uuid.UUID('12345678-1234-5678-1234-567812345678')


def make_client(service_name):
    return boto3.client(
        service_name,
        region_name='ap-northeast-1',
        aws_access_key_id='test-access-key',
        aws_secret_access_key='test-secret-key',
        aws_session_token='test-session-token',
    )


def expected_body(params=None):
    if params is None:
        params = {'value': '1'}
    body_params = dict(params)
    body_params['task_id'] = str(TASK_UUID)
    return json.dumps({
        'version': 1,
        'task_id': str(TASK_UUID),
        'queue_name': 'action-queue',
        'kind': 'action',
        'bot_name': 'bot',
        'params': body_params,
    }, ensure_ascii=False, separators=(',', ':'))


class AwsTaskQueueTest(unittest.TestCase):
    def make_queue(
            self, client_factory=None, clock=None, settings=AWS_SETTINGS):
        queue = AwsTaskQueue(
            client_factory=client_factory,
            clock=clock,
            uuid_factory=lambda: TASK_UUID,
        )
        queue.initialize(settings)
        return queue

    def test_SDK_clientは初回登録まで生成しない(self):
        client = Mock()
        client_factory = Mock(return_value=client)
        queue = self.make_queue(client_factory=client_factory)

        client_factory.assert_not_called()
        queue.create_task(
            'action-queue', '/api/v1/bots/bot/action', {'value': '1'})

        client_factory.assert_called_once_with(
            'sqs', region_name='ap-northeast-1')

    def test_即時Taskはparamsを変更せずSQSへ登録する(self):
        sqs = make_client('sqs')
        stubber = Stubber(sqs)
        params = {
            'value': '日本語',
            'task_id': 'caller-task-id',
        }
        body = expected_body(params=params)
        stubber.add_response('send_message', {
            'MessageId': 'message-id',
        }, {
            'QueueUrl': AWS_SETTINGS['task_queue']['queues'][
                'action-queue']['url'],
            'MessageBody': body,
        })
        queue = self.make_queue(
            client_factory=lambda service_name, **options: sqs)

        with stubber:
            task_id = queue.create_task(
                'action-queue', '/api/v1/bots/bot/action', params,
                delay_seconds=0)

        self.assertEqual(str(TASK_UUID), task_id)
        self.assertEqual({
            'value': '日本語',
            'task_id': 'caller-task-id',
        }, params)
        envelope = json.loads(body)
        self.assertEqual(str(TASK_UUID), envelope['params']['task_id'])
        self.assertNotIn('token', body.lower())
        self.assertNotIn('credential', body.lower())

    def test_900秒以下の正の遅延は切り上げてSQSへ渡す(self):
        sqs = make_client('sqs')
        stubber = Stubber(sqs)
        stubber.add_response('send_message', {
            'MessageId': 'message-id',
        }, {
            'QueueUrl': AWS_SETTINGS['task_queue']['queues'][
                'action-queue']['url'],
            'MessageBody': expected_body(),
            'DelaySeconds': 900,
        })
        queue = self.make_queue(
            client_factory=lambda service_name, **options: sqs)

        with stubber:
            queue.create_task(
                'action-queue', '/api/v1/bots/bot/action', {'value': '1'},
                delay_seconds=899.1)

    def test_900秒超はUTCのSchedulerからSQSへ渡す(self):
        scheduler = make_client('scheduler')
        stubber = Stubber(scheduler)
        body = expected_body()
        stubber.add_response('create_schedule', {
            'ScheduleArn': (
                'arn:aws:scheduler:ap-northeast-1:000000000000:'
                'schedule/test-scheduler-group/xsbot-'
                f'{TASK_UUID}'),
        }, {
            'Name': f'xsbot-{TASK_UUID}',
            'GroupName': 'test-scheduler-group',
            'ScheduleExpression': 'at(2026-08-12T00:16:00)',
            'ScheduleExpressionTimezone': 'UTC',
            'FlexibleTimeWindow': {'Mode': 'OFF'},
            'ActionAfterCompletion': 'DELETE',
            'Target': {
                'Arn': AWS_SETTINGS['task_queue']['queues'][
                    'action-queue']['arn'],
                'RoleArn': AWS_SETTINGS['task_queue']['scheduler'][
                    'role_arn'],
                'Input': body,
            },
            'ClientToken': str(TASK_UUID),
        })
        queue = self.make_queue(
            client_factory=lambda service_name, **options: scheduler,
            clock=lambda: datetime.datetime(
                2026, 8, 12, 0, 0, 0,
                tzinfo=datetime.timezone.utc),
        )

        with stubber:
            queue.create_task(
                'action-queue', '/api/v1/bots/bot/action', {'value': '1'},
                delay_seconds=900.1)

    def test_Scheduler配送policyは3項目指定時だけ付与する(self):
        settings = copy.deepcopy(AWS_SETTINGS)
        settings['task_queue']['scheduler'].update({
            'dead_letter_arn': (
                'arn:aws:sqs:ap-northeast-1:000000000000:scheduler-dlq'),
            'maximum_event_age_seconds': 3600,
            'maximum_retry_attempts': 3,
        })
        scheduler = make_client('scheduler')
        stubber = Stubber(scheduler)
        stubber.add_response('create_schedule', {
            'ScheduleArn': (
                'arn:aws:scheduler:ap-northeast-1:000000000000:'
                'schedule/test-scheduler-group/xsbot-'
                f'{TASK_UUID}'),
        }, {
            'Name': f'xsbot-{TASK_UUID}',
            'GroupName': 'test-scheduler-group',
            'ScheduleExpression': 'at(2026-08-12T00:16:00)',
            'ScheduleExpressionTimezone': 'UTC',
            'FlexibleTimeWindow': {'Mode': 'OFF'},
            'ActionAfterCompletion': 'DELETE',
            'Target': {
                'Arn': AWS_SETTINGS['task_queue']['queues'][
                    'action-queue']['arn'],
                'RoleArn': AWS_SETTINGS['task_queue']['scheduler'][
                    'role_arn'],
                'Input': expected_body(),
                'DeadLetterConfig': {
                    'Arn': settings['task_queue']['scheduler'][
                        'dead_letter_arn'],
                },
                'RetryPolicy': {
                    'MaximumEventAgeInSeconds': 3600,
                    'MaximumRetryAttempts': 3,
                },
            },
            'ClientToken': str(TASK_UUID),
        })
        queue = self.make_queue(
            client_factory=lambda service_name, **options: scheduler,
            clock=lambda: datetime.datetime(
                2026, 8, 12, 0, 0, 0,
                tzinfo=datetime.timezone.utc),
            settings=settings,
        )

        with stubber:
            queue.create_task(
                'action-queue', '/api/v1/bots/bot/action', {'value': '1'},
                delay_seconds=900.1)

    def test_Scheduler配送policyの部分指定は拒否する(self):
        settings = copy.deepcopy(AWS_SETTINGS)
        settings['task_queue']['scheduler']['dead_letter_arn'] = (
            'arn:aws:sqs:ap-northeast-1:000000000000:scheduler-dlq')
        client_factory = Mock()
        queue = self.make_queue(
            client_factory=client_factory,
            clock=lambda: datetime.datetime(
                2026, 8, 12, 0, 0, 0,
                tzinfo=datetime.timezone.utc),
            settings=settings,
        )

        with self.assertRaises(ValueError):
            queue.create_task(
                'action-queue', '/api/v1/bots/bot/action', {'value': '1'},
                delay_seconds=901)
        client_factory.assert_not_called()

    def test_build_queueはPhase4用の経路へ誤登録しない(self):
        client_factory = Mock()
        queue = self.make_queue(client_factory=client_factory)

        with self.assertRaises(TaskQueueError):
            queue.create_task(
                'build-queue', '/api/v1/bots/bot/build', {})

        client_factory.assert_not_called()

    def test_設定不備をSDK例外として扱わない(self):
        queue = self.make_queue()

        with self.assertRaises(ValueError):
            queue.create_task(
                'missing-queue', '/api/v1/bots/bot/action', {})

    def test_envelopeは許可したqueueとpathだけを受け付ける(self):
        client = Mock()
        queue = self.make_queue(
            client_factory=lambda service_name, **options: client)

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

        queue.create_task(
            'group-message-queue',
            '/api/v1/bots/test-bot/process_group_batch',
            {'task_id': 'caller'},
        )
        envelope = json.loads(client.send_message.call_args.kwargs[
            'MessageBody'])
        self.assertEqual('group_batch', envelope['kind'])
        self.assertEqual('test-bot', envelope['bot_name'])
        self.assertEqual('group-message-queue', envelope['queue_name'])
        self.assertNotIn('url', envelope)

    def test_AWS_SDK例外だけを共通例外へ変換する(self):
        sqs = make_client('sqs')
        stubber = Stubber(sqs)
        stubber.add_client_error(
            'send_message',
            service_error_code='ServiceUnavailable',
            service_message='unavailable',
            http_status_code=503,
            expected_params={
                'QueueUrl': AWS_SETTINGS['task_queue']['queues'][
                    'action-queue']['url'],
                'MessageBody': expected_body(),
            },
        )
        queue = self.make_queue(
            client_factory=lambda service_name, **options: sqs)

        with stubber, self.assertRaises(TaskQueueError) as raised:
            queue.create_task(
                'action-queue', '/api/v1/bots/bot/action', {'value': '1'})
        self.assertEqual(
            'AWS非同期タスクの登録に失敗しました',
            str(raised.exception),
        )
        self.assertNotIn('ServiceUnavailable', str(raised.exception))
        self.assertNotIn('unavailable', str(raised.exception))

        application_error = RuntimeError('application error')
        client = Mock()
        client.send_message.side_effect = application_error
        queue = self.make_queue(
            client_factory=lambda service_name, **options: client)
        with self.assertRaises(RuntimeError) as raised:
            queue.create_task(
                'action-queue', '/api/v1/bots/bot/action', {'value': '1'})
        self.assertIs(application_error, raised.exception)

    def test_ログへparameterやAWS設定値を出さない(self):
        client = Mock()
        queue = self.make_queue(
            client_factory=lambda service_name, **options: client)

        with patch(
                'cloud_backend.aws.task_queue.logging.info') as log_info:
            queue.create_task(
                'action-queue', '/api/v1/bots/bot/action',
                {'value': 'secret-parameter'})

        messages = ' '.join(
            str(value)
            for call in log_info.call_args_list
            for value in call.args)
        self.assertNotIn('secret-parameter', messages)
        self.assertNotIn('/api/v1/bots/bot/action', messages)
        self.assertNotIn('test-scheduler-role', messages)
        self.assertNotIn('amazonaws.com', messages)


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
