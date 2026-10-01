"""Lambdaの非同期呼出しを利用するTaskQueue実装。"""

import json
import logging
import re
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from cloud_backend.contracts import TaskQueue, TaskQueueError


class AwsTaskQueue(TaskQueue):
    """actionとグループ配信を専用のLambda workerへ即時登録する。"""

    allows_delayed_scenarios = False
    defer_until_response = True
    _BOT_NAME_PATTERN = re.compile(r'^[-_a-zA-Z0-9]+$')
    _ERROR_MESSAGE = 'AWS非同期タスクの登録に失敗しました'

    def __init__(self, client_factory=None, uuid_factory=None):
        self._client_factory = client_factory or boto3.client
        self._uuid_factory = uuid_factory or uuid.uuid4
        self._lambda_client = None
        self._region = None
        self._functions = {}
        self._initialized = False

    @staticmethod
    def _raise_queue_error(error):
        if isinstance(error, (BotoCoreError, ClientError)):
            raise TaskQueueError(AwsTaskQueue._ERROR_MESSAGE) from error
        if type(error).__module__.startswith(('boto3.', 'botocore.')):
            raise TaskQueueError(AwsTaskQueue._ERROR_MESSAGE) from error
        raise error

    def _call(self, operation):
        try:
            return operation()
        except Exception as error:
            self._raise_queue_error(error)

    def initialize(self, aws_settings):
        """両workerの設定を検査し、SDK clientは初回登録まで生成しない。"""
        task_queue_settings = aws_settings.get('task_queue', {})
        functions = task_queue_settings.get('functions')
        if not isinstance(functions, dict):
            raise ValueError('AWS TaskQueueのfunctionsが設定されていません')
        for queue_name in ('action-queue', 'group-message-queue'):
            function_name = functions.get(queue_name)
            if not isinstance(function_name, str) or not function_name.strip():
                raise ValueError(
                    f'AWS {queue_name}のLambda workerが設定されていません')
        self._region = aws_settings.get('region') or None
        self._functions = dict(functions)
        self._lambda_client = None
        self._initialized = True

    def _require_initialized(self):
        if not self._initialized:
            raise ValueError(
                'Task client not initialized. Call initialize() first.')

    def _get_client(self):
        self._require_initialized()
        if self._lambda_client is None:
            def create_client():
                options = {}
                if self._region is not None:
                    options['region_name'] = self._region
                return self._client_factory('lambda', **options)
            self._lambda_client = self._call(create_client)
        return self._lambda_client

    @staticmethod
    def _parse_destination(queue_name, url):
        routes = {
            'action-queue': ('action', 'action'),
            'group-message-queue': ('process_group_batch', 'group_batch'),
        }
        if queue_name == 'build-queue':
            raise TaskQueueError('AWS build-queueはTaskQueueでは扱いません')
        try:
            route_name, kind = routes[queue_name]
        except KeyError as error:
            raise TaskQueueError(
                f'AWS TaskQueueでは扱えない論理キューです: {queue_name}') from error

        prefix = '/api/v1/bots/'
        suffix = f'/{route_name}'
        if (
                not isinstance(url, str)
                or not url.startswith(prefix)
                or not url.endswith(suffix)):
            raise TaskQueueError('AWS TaskQueueへ渡されたURLが不正です')
        bot_name = url[len(prefix):-len(suffix)]
        if not AwsTaskQueue._BOT_NAME_PATTERN.fullmatch(bot_name):
            # percent decodeの解釈差や別pathへのdispatchを避ける。
            raise TaskQueueError('AWS TaskQueueへ渡されたBot名が不正です')
        return kind, bot_name

    @classmethod
    def _message_body(cls, queue_name, url, params, task_id):
        kind, bot_name = cls._parse_destination(queue_name, url)
        request_params = params.copy()
        request_params['task_id'] = task_id
        return json.dumps({
            'version': 1,
            'task_id': task_id,
            'queue_name': queue_name,
            'kind': kind,
            'bot_name': bot_name,
            'params': request_params,
        }, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    @staticmethod
    def _validate_delay(delay_seconds):
        if delay_seconds is None:
            return
        if (
                isinstance(delay_seconds, bool)
                or not isinstance(delay_seconds, (int, float))
                or delay_seconds != 0):
            raise TaskQueueError('AWSでは0秒以外の遅延タスクを登録できません')

    def create_task(self, queue_name, url, params, delay_seconds=None):
        self._require_initialized()
        self._validate_delay(delay_seconds)
        task_id = str(self._uuid_factory())
        try:
            body = self._message_body(queue_name, url, params, task_id)
            logging.info(
                'Creating AWS task: queue=%s, task_id=%s', queue_name, task_id)
            result = self._call(lambda: self._get_client().invoke(
                FunctionName=self._functions[queue_name],
                InvocationType='Event',
                Payload=body,
            ))
            if not isinstance(result, dict) or result.get('StatusCode') != 202:
                raise TaskQueueError(self._ERROR_MESSAGE)
            logging.info(
                'Created AWS task: queue=%s, task_id=%s', queue_name, task_id)
            return task_id
        except Exception as error:
            error.task_id = task_id
            raise
