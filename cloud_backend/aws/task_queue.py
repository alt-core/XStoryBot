"""Amazon SQSとEventBridge Schedulerを利用するTaskQueue実装。"""

import datetime
import json
import logging
import math
import re
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from cloud_backend.contracts import TaskQueue, TaskQueueError


class AwsTaskQueue(TaskQueue):
    """短い遅延をSQS、15分を超える遅延をSchedulerへ登録する。"""

    _MAX_SQS_DELAY_SECONDS = 900
    _BOT_NAME_PATTERN = re.compile(r'^[-_a-zA-Z0-9]+$')
    _ERROR_MESSAGE = 'AWS非同期タスクの登録に失敗しました'

    def __init__(
            self, client_factory=None, clock=None, uuid_factory=None):
        self._client_factory = client_factory or boto3.client
        self._clock = clock or (
            lambda: datetime.datetime.now(datetime.timezone.utc))
        self._uuid_factory = uuid_factory or uuid.uuid4
        self._sqs_client = None
        self._scheduler_client = None
        self._region = None
        self._queues = {}
        self._scheduler = {}
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
        """AWS設定を保持し、SDK clientは初回登録まで生成しない。"""
        task_queue_settings = aws_settings.get('task_queue', {})
        self._region = aws_settings.get('region') or None
        self._queues = dict(task_queue_settings.get('queues', {}))
        self._scheduler = dict(task_queue_settings.get('scheduler', {}))
        self._sqs_client = None
        self._scheduler_client = None
        self._initialized = True

    def _require_initialized(self):
        if not self._initialized:
            raise ValueError(
                'Task client not initialized. Call initialize() first.')

    def _get_client(self, service_name):
        self._require_initialized()
        attribute_name = f'_{service_name}_client'
        client = getattr(self, attribute_name)
        if client is None:
            def create_client():
                options = {}
                if self._region is not None:
                    options['region_name'] = self._region
                return self._client_factory(service_name, **options)

            client = self._call(create_client)
            setattr(self, attribute_name, client)
        return client

    def _queue_settings(self, queue_name):
        if queue_name == 'build-queue':
            raise TaskQueueError(
                'AWS build-queueはTaskQueueでは扱いません')
        try:
            queue_settings = self._queues[queue_name]
        except KeyError as error:
            raise ValueError(
                f'AWSの論理キューが設定されていません: {queue_name}') from error
        if not isinstance(queue_settings, dict):
            raise ValueError(
                f'AWSの論理キュー設定が不正です: {queue_name}')
        return queue_settings

    @staticmethod
    def _required_setting(settings, key, description):
        value = settings.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f'{description}が設定されていません')
        return value

    @staticmethod
    def _parse_destination(queue_name, url):
        routes = {
            'action-queue': ('action', 'action'),
            'group-message-queue': (
                'process_group_batch', 'group_batch'),
        }
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
        }, ensure_ascii=False, separators=(',', ':'))

    @staticmethod
    def _delay_seconds(delay_seconds):
        if delay_seconds is None or delay_seconds <= 0:
            return 0
        return math.ceil(delay_seconds)

    def _send_to_sqs(self, queue_name, queue_settings, body, delay_seconds):
        queue_url = self._required_setting(
            queue_settings, 'url', f'AWS {queue_name}のSQS URL')
        request = {
            'QueueUrl': queue_url,
            'MessageBody': body,
        }
        if delay_seconds > 0:
            request['DelaySeconds'] = delay_seconds
        self._call(lambda: self._get_client('sqs').send_message(**request))

    def _schedule_to_sqs(
            self, queue_name, queue_settings, body, delay_seconds, task_id):
        queue_arn = self._required_setting(
            queue_settings, 'arn', f'AWS {queue_name}のSQS ARN')
        role_arn = self._required_setting(
            self._scheduler, 'role_arn', 'AWS SchedulerのRole ARN')
        group_name = self._required_setting(
            self._scheduler, 'group_name', 'AWS SchedulerのGroupName')
        delivery_policy = self._scheduler_delivery_policy()

        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=datetime.timezone.utc)
        else:
            now = now.astimezone(datetime.timezone.utc)
        scheduled_at = now + datetime.timedelta(seconds=delay_seconds)
        # Schedulerのat式は秒を指定できても実行精度は分単位なので、
        # 要求時刻より早く起動しないよう次の分境界へ切り上げる。
        if scheduled_at.second or scheduled_at.microsecond:
            scheduled_at = (
                scheduled_at.replace(second=0, microsecond=0)
                + datetime.timedelta(minutes=1))

        schedule_name = f'xsbot-{task_id}'
        request = {
            'Name': schedule_name,
            'GroupName': group_name,
            'ScheduleExpression': scheduled_at.strftime(
                'at(%Y-%m-%dT%H:%M:%S)'),
            'ScheduleExpressionTimezone': 'UTC',
            'FlexibleTimeWindow': {'Mode': 'OFF'},
            'ActionAfterCompletion': 'DELETE',
            'Target': {
                'Arn': queue_arn,
                'RoleArn': role_arn,
                'Input': body,
                **delivery_policy,
            },
            'ClientToken': task_id,
        }
        self._call(
            lambda: self._get_client('scheduler').create_schedule(**request))

    def _scheduler_delivery_policy(self):
        keys = (
            'dead_letter_arn',
            'maximum_event_age_seconds',
            'maximum_retry_attempts',
        )
        present = [
            key for key in keys
            if self._scheduler.get(key) not in (None, '')
        ]
        if not present:
            return {}
        if len(present) != len(keys):
            raise ValueError(
                'AWS SchedulerのDLQと再試行設定は3項目を同時に指定してください')

        dead_letter_arn = self._required_setting(
            self._scheduler,
            'dead_letter_arn',
            'AWS SchedulerのDLQ ARN',
        )
        maximum_event_age_seconds = self._scheduler[
            'maximum_event_age_seconds']
        maximum_retry_attempts = self._scheduler[
            'maximum_retry_attempts']
        if (
                isinstance(maximum_event_age_seconds, bool)
                or not isinstance(maximum_event_age_seconds, int)
                or not 60 <= maximum_event_age_seconds <= 86400):
            raise ValueError(
                'AWS Schedulerのmaximum_event_age_secondsが不正です')
        if (
                isinstance(maximum_retry_attempts, bool)
                or not isinstance(maximum_retry_attempts, int)
                or not 0 <= maximum_retry_attempts <= 185):
            raise ValueError(
                'AWS Schedulerのmaximum_retry_attemptsが不正です')
        return {
            'DeadLetterConfig': {'Arn': dead_letter_arn},
            'RetryPolicy': {
                'MaximumEventAgeInSeconds': maximum_event_age_seconds,
                'MaximumRetryAttempts': maximum_retry_attempts,
            },
        }

    def create_task(self, queue_name, url, params, delay_seconds=None):
        self._require_initialized()
        queue_settings = self._queue_settings(queue_name)
        task_id = str(self._uuid_factory())
        body = self._message_body(queue_name, url, params, task_id)
        normalized_delay = self._delay_seconds(delay_seconds)

        logging.info(
            'Creating AWS task: queue=%s, task_id=%s, delay_seconds=%s',
            queue_name, task_id, normalized_delay)
        if normalized_delay <= self._MAX_SQS_DELAY_SECONDS:
            self._send_to_sqs(
                queue_name, queue_settings, body, normalized_delay)
        else:
            self._schedule_to_sqs(
                queue_name,
                queue_settings,
                body,
                normalized_delay,
                task_id,
            )
        logging.info(
            'Created AWS task: queue=%s, task_id=%s',
            queue_name, task_id)
        return task_id
