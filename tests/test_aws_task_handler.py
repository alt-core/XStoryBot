import json
import types
import unittest
from unittest.mock import Mock, call, patch
import uuid

from cloud_backend.aws import task_handler
from cloud_backend.aws.state_store import (
    TASK_EXECUTION_BUSY,
    TASK_EXECUTION_CLAIMED,
    TASK_EXECUTION_COMPLETED,
)


ACTION_ARN = 'arn:aws:sqs:test-region-1:000000000000:action'
GROUP_ARN = 'arn:aws:sqs:test-region-1:000000000000:group'


class FakeUser:
    def __init__(self, service_name, user_id):
        self.service_name = service_name
        self.user_id = user_id

    @classmethod
    def deserialize(cls, value):
        if not isinstance(value, str) or ':' not in value:
            return None
        service_name, user_id = value.split(':', 1)
        return cls(service_name, user_id)


class FakeInterface:
    def create_context(self, user, action, attrs):
        return (user, action, attrs)


class FakeBot:
    def __init__(self):
        self.check_reload = Mock()
        self.handled = []

    def get_interface(self, service_name):
        return FakeInterface() if service_name == 'plaintext' else None

    def handle_action(self, context):
        self.handled.append(context)
        return 'ok'


def make_envelope(kind='action', queue_name='action-queue', **params):
    task_id = str(uuid.uuid4())
    default_params = {
        'user': 'plaintext:user-1',
        'action': 'hello',
        'task_id': task_id,
    }
    default_params.update(params)
    return {
        'version': 1,
        'task_id': task_id,
        'queue_name': queue_name,
        'kind': kind,
        'bot_name': 'bot',
        'params': default_params,
    }


def make_record(message_id, envelope, arn=ACTION_ARN):
    return {
        'messageId': message_id,
        'eventSource': 'aws:sqs',
        'eventSourceARN': arn,
        'body': json.dumps(envelope, ensure_ascii=False),
    }


class AwsTaskHandlerTest(unittest.TestCase):
    def setUp(self):
        self.bot = FakeBot()
        self.manager = Mock()
        self.manager.handle_batch_process_request.return_value = (
            {'message': '処理完了'}, 200)
        self.execution_store = Mock()
        self.execution_store.try_claim_task_execution.return_value = (
            TASK_EXECUTION_CLAIMED)
        self.dependencies = {
            'backend_settings': {
                'task_queue': {
                    'queues': {
                        'action-queue': {'arn': ACTION_ARN},
                        'group-message-queue': {'arn': GROUP_ARN},
                    },
                },
            },
            'get_bot': Mock(return_value=self.bot),
            'user_class': FakeUser,
            'get_group_members': Mock(return_value=[]),
            'options': {},
            'manager_class': Mock(return_value=self.manager),
            'execution_store': self.execution_store,
        }

    def invoke(self, records):
        with patch.object(
                task_handler, '_load_dependencies',
                return_value=self.dependencies):
            return task_handler.lambda_handler(
                {'Records': records},
                types.SimpleNamespace(aws_request_id='request-1'),
            )

    def test_失敗recordだけを返して後続処理を続ける(self):
        first = make_envelope(action='first')
        broken = make_envelope(action='broken')
        broken['params']['user'] = 'invalid'
        last = make_envelope(action='last')

        response = self.invoke([
            make_record('first', first),
            make_record('broken', broken),
            make_record('last', last),
        ])

        self.assertEqual(
            response, {'batchItemFailures': [{'itemIdentifier': 'broken'}]})
        self.assertEqual(
            [context[1] for context in self.bot.handled],
            ['first', 'last'],
        )

    def test_actionとgroupは同じ共通processorへ渡す(self):
        action = make_envelope(action='hello@@action-token')
        group = make_envelope(
            kind='group_batch',
            queue_name='group-message-queue',
            message_task_id='message-1',
            batch_index='2',
        )

        response = self.invoke([
            make_record('action', action),
            make_record('group', group, GROUP_ARN),
        ])

        self.assertEqual(response, {'batchItemFailures': []})
        self.assertEqual(self.bot.handled[0][1], 'hello')
        self.assertEqual(
            self.bot.handled[0][2], {'action_token': 'action-token'})
        self.dependencies['manager_class'].assert_called_once_with(
            'bot', bot_instance=self.bot)
        self.manager.handle_batch_process_request.assert_called_once_with(
            'message-1', 2)
        self.assertEqual(
            self.execution_store.try_claim_task_execution.call_args_list,
            [
                call(
                    f'action:bot:{action["task_id"]}',
                    'request-1:action',
                    90,
                ),
                call(
                    f'group:bot:{group["task_id"]}',
                    'request-1:group',
                    960,
                ),
            ],
        )
        self.assertEqual(
            self.execution_store.complete_task_execution.call_args_list,
            [
                call(
                    f'action:bot:{action["task_id"]}',
                    'request-1:action',
                ),
                call(
                    f'group:bot:{group["task_id"]}',
                    'request-1:group',
                ),
            ],
        )

    def test_group再キューは新task_id単位で別実行として扱う(self):
        first = make_envelope(
            kind='group_batch',
            queue_name='group-message-queue',
            message_task_id='scheduled-message',
            batch_index=0,
        )
        requeued = make_envelope(
            kind='group_batch',
            queue_name='group-message-queue',
            message_task_id='scheduled-message',
            batch_index=0,
        )

        response = self.invoke([
            make_record('early', first, GROUP_ARN),
            make_record('scheduled', requeued, GROUP_ARN),
        ])

        self.assertEqual(response, {'batchItemFailures': []})
        self.assertEqual(
            self.manager.handle_batch_process_request.call_args_list,
            [
                call('scheduled-message', 0),
                call('scheduled-message', 0),
            ],
        )
        self.assertEqual(
            self.execution_store.try_claim_task_execution.call_args_list,
            [
                call(
                    f'group:bot:{first["task_id"]}',
                    'request-1:early',
                    960,
                ),
                call(
                    f'group:bot:{requeued["task_id"]}',
                    'request-1:scheduled',
                    960,
                ),
            ],
        )

    def test_groupの同じtask_idの再配送は完了済みなら処理しない(self):
        envelope = make_envelope(
            kind='group_batch',
            queue_name='group-message-queue',
            message_task_id='message-1',
            batch_index=0,
        )
        self.execution_store.try_claim_task_execution.side_effect = [
            TASK_EXECUTION_CLAIMED,
            TASK_EXECUTION_COMPLETED,
        ]

        response = self.invoke([
            make_record('first', envelope, GROUP_ARN),
            make_record('redelivery', envelope, GROUP_ARN),
        ])

        self.assertEqual(response, {'batchItemFailures': []})
        self.manager.handle_batch_process_request.assert_called_once_with(
            'message-1', 0)
        key = f'group:bot:{envelope["task_id"]}'
        self.assertEqual(
            self.execution_store.try_claim_task_execution.call_args_list,
            [
                call(key, 'request-1:first', 960),
                call(key, 'request-1:redelivery', 960),
            ],
        )
        self.execution_store.complete_task_execution.assert_called_once_with(
            key, 'request-1:first')

    def test_実行中は再試行し完了済みはackする(self):
        busy = make_envelope(action='busy')
        completed = make_envelope(action='completed')
        self.execution_store.try_claim_task_execution.side_effect = [
            TASK_EXECUTION_BUSY,
            TASK_EXECUTION_COMPLETED,
        ]

        response = self.invoke([
            make_record('busy', busy),
            make_record('completed', completed),
        ])

        self.assertEqual(
            response,
            {'batchItemFailures': [{'itemIdentifier': 'busy'}]},
        )
        self.assertEqual(self.bot.handled, [])
        self.execution_store.complete_task_execution.assert_not_called()

    def test_業務処理失敗時は完了記録を付けない(self):
        envelope = make_envelope(action='failure')
        self.bot.handle_action = Mock(side_effect=RuntimeError('failure'))

        response = self.invoke([make_record('message-1', envelope)])

        self.assertEqual(
            response,
            {'batchItemFailures': [{'itemIdentifier': 'message-1'}]},
        )
        self.execution_store.complete_task_execution.assert_not_called()

    def test_envelopeと送信元を厳密に検証する(self):
        invalid_records = []

        malformed = make_record('malformed', make_envelope())
        malformed['body'] = '{'
        invalid_records.append(malformed)

        invalid_version = make_envelope()
        invalid_version['version'] = True
        invalid_records.append(make_record('version', invalid_version))

        invalid_uuid = make_envelope()
        invalid_uuid['task_id'] = 'invalid'
        invalid_records.append(make_record('uuid', invalid_uuid))

        mismatched_id = make_envelope()
        mismatched_id['params']['task_id'] = str(uuid.uuid4())
        invalid_records.append(make_record('id-mismatch', mismatched_id))

        unknown_kind = make_envelope(kind='unknown')
        invalid_records.append(make_record('kind', unknown_kind))

        invalid_bot = make_envelope()
        invalid_bot['bot_name'] = 'bot/name'
        invalid_records.append(make_record('bot', invalid_bot))

        invalid_records.append(make_record(
            'source', make_envelope(), arn=GROUP_ARN))

        invalid_batch_index = make_envelope(
            kind='group_batch',
            queue_name='group-message-queue',
            message_task_id='message-1',
            batch_index=1.9,
        )
        invalid_records.append(make_record(
            'batch-index', invalid_batch_index, GROUP_ARN))

        response = self.invoke(invalid_records)

        self.assertEqual(
            response,
            {'batchItemFailures': [
                {'itemIdentifier': message_id}
                for message_id in (
                    'malformed', 'version', 'uuid', 'id-mismatch',
                    'kind', 'bot', 'source', 'batch-index',
                )
            ]},
        )
        self.assertEqual(self.bot.handled, [])
        self.execution_store.try_claim_task_execution.assert_not_called()

    def test_failure_logへ本文や例外本文を出さない(self):
        secret = 'secret-action-value'
        envelope = make_envelope(action=secret)
        self.bot.handle_action = Mock(
            side_effect=RuntimeError(f'failed with {secret}'))

        with patch.object(task_handler.logging, 'error') as error_log:
            response = self.invoke([make_record('message-1', envelope)])

        self.assertEqual(
            response,
            {'batchItemFailures': [{'itemIdentifier': 'message-1'}]},
        )
        messages = ' '.join(
            str(value)
            for log_call in error_log.call_args_list
            for value in log_call.args
        )
        self.assertNotIn(secret, messages)
        self.assertNotIn('failed with', messages)
        self.assertIn('RuntimeError', messages)

        invalid_kind = make_envelope(kind=secret)
        with patch.object(task_handler.logging, 'error') as error_log:
            self.invoke([make_record('message-2', invalid_kind)])
        messages = ' '.join(
            str(value)
            for log_call in error_log.call_args_list
            for value in log_call.args
        )
        self.assertNotIn(secret, messages)

    def test_message_id欠落時は空のpartial_failureを返さない(self):
        record = make_record('', make_envelope())

        with self.assertRaisesRegex(ValueError, 'messageId'):
            self.invoke([record])
