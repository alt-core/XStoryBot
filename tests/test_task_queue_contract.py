"""GCPとAWSのTaskQueue共通契約を具象adapterへ適用する。"""

import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

from botocore.exceptions import ClientError

from cloud_backend.aws import task_queue as aws_task_queue
from cloud_backend.aws.task_queue import AwsTaskQueue
from tests.cloud_backend.task_queue_contract import (
    CapturedTask,
    TaskQueueContractMixin,
)
from tests.test_aws_task_queue import AWS_SETTINGS
from tests.test_task_client import (
    FakeCredentialSource,
    load_task_queue,
    make_client,
    make_settings,
)


class GcpTaskQueueContractTest(TaskQueueContractMixin, unittest.TestCase):
    def create_contract_queue(self):
        self.contract_module, self.dependencies = load_task_queue()
        self.queue = self.contract_module.GcpTaskQueue(
            credential_source=FakeCredentialSource())
        self.queue.initialize(make_settings())
        self.client = make_client()

    def capture_task(self, queue_name, url, params, delay_seconds=None):
        with patch.object(
                self.queue, 'get_client', return_value=self.client):
            task_id = self.queue.create_task(
                queue_name, url, params, delay_seconds=delay_seconds)
        request = self.client.create_task.call_args.args[0]
        task = request.task
        raw_body = task.http_request.body.decode('utf-8')
        parsed = parse_qs(raw_body, keep_blank_values=True)
        normalized = {
            key: values[-1]
            for key, values in parsed.items()
        }
        return CapturedTask(
            task_id=task_id,
            params=normalized,
            delayed=hasattr(task, 'schedule_time'),
            raw_body=raw_body,
        )

    def set_transport_error(self, error):
        self.client.create_task.side_effect = error

    def make_sdk_error(self):
        error_class = type(
            'ServiceUnavailable',
            (Exception,),
            {'__module__': 'google.api_core.exceptions'},
        )
        return error_class('unavailable')

    def contract_secret_values(self):
        return ('shared-api-token',)


class AwsTaskQueueContractTest(TaskQueueContractMixin, unittest.TestCase):
    def create_contract_queue(self):
        self.contract_module = aws_task_queue
        self.client = Mock()
        self.queue = AwsTaskQueue(
            client_factory=lambda service_name, **options: self.client)
        self.queue.initialize(AWS_SETTINGS)

    def capture_task(self, queue_name, url, params, delay_seconds=None):
        task_id = self.queue.create_task(
            queue_name, url, params, delay_seconds=delay_seconds)
        request = self.client.send_message.call_args.kwargs
        raw_body = request['MessageBody']
        envelope = json.loads(raw_body)
        return CapturedTask(
            task_id=task_id,
            params=envelope['params'],
            delayed='DelaySeconds' in request,
            raw_body=raw_body,
        )

    def set_transport_error(self, error):
        self.client.send_message.side_effect = error

    def make_sdk_error(self):
        return ClientError(
            {
                'Error': {
                    'Code': 'ServiceUnavailable',
                    'Message': 'unavailable',
                },
            },
            'SendMessage',
        )


if __name__ == '__main__':
    unittest.main()
