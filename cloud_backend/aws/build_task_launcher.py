"""単発のシナリオビルドをAmazon ECSへ登録する内部実装。"""

import re
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from cloud_backend.contracts import TaskQueueError


class AwsBuildTaskLauncher:
    """公開サブネット上のFargateタスクを一件だけ開始する。"""

    _BOT_NAME_PATTERN = re.compile(r'^[-_a-zA-Z0-9]+$')
    _CONTAINER_NAME_PATTERN = re.compile(r'^[-_a-zA-Z0-9]+$')
    _SUBNET_ID_PATTERN = re.compile(r'^subnet-[0-9a-fA-F]+$')
    _SECURITY_GROUP_ID_PATTERN = re.compile(r'^sg-[0-9a-fA-F]+$')
    _MAX_SUBNETS = 16
    _MAX_SECURITY_GROUPS = 5
    _ERROR_MESSAGE = 'AWSビルドタスクの開始に失敗しました'

    def __init__(
            self, aws_settings=None, client=None, client_factory=None):
        if aws_settings is None:
            import settings
            aws_settings = settings.BACKEND_SETTINGS
        if not isinstance(aws_settings, dict):
            raise ValueError('AWS設定が不正です')

        task_queue_settings = aws_settings.get('task_queue', {})
        if not isinstance(task_queue_settings, dict):
            raise ValueError('AWS TaskQueue設定が不正です')
        build_settings = task_queue_settings.get('build', {})
        if not isinstance(build_settings, dict):
            raise ValueError('AWSビルドタスク設定が不正です')

        self._cluster = self._required_string(
            build_settings, 'cluster', 'AWS ECS cluster')
        self._task_definition = self._required_string(
            build_settings, 'task_definition', 'AWS ECS task definition')
        self._container_name = self._required_string(
            build_settings, 'container_name', 'AWS ECS container name')
        if not self._CONTAINER_NAME_PATTERN.fullmatch(self._container_name):
            raise ValueError('AWS ECS container nameが不正です')
        self._subnet_ids = self._required_id_list(
            build_settings,
            'subnet_ids',
            'AWS ECS subnet IDs',
            self._SUBNET_ID_PATTERN,
            self._MAX_SUBNETS,
        )
        self._security_group_ids = self._required_id_list(
            build_settings,
            'security_group_ids',
            'AWS ECS security group IDs',
            self._SECURITY_GROUP_ID_PATTERN,
            self._MAX_SECURITY_GROUPS,
        )

        region = aws_settings.get('region')
        if region in (None, ''):
            self._region = None
        elif not self._is_plain_string(region):
            raise ValueError('AWS regionが不正です')
        else:
            self._region = region

        self._client_instance = client
        self._client_factory = client_factory or boto3.client

    @staticmethod
    def _is_plain_string(value):
        return (
            isinstance(value, str)
            and bool(value)
            and value == value.strip()
            and not any(character.isspace() for character in value)
        )

    @classmethod
    def _required_string(cls, settings, key, description):
        value = settings.get(key)
        if not cls._is_plain_string(value):
            raise ValueError(f'{description}が設定されていません')
        return value

    @classmethod
    def _required_id_list(
            cls, settings, key, description, pattern, maximum):
        values = settings.get(key)
        if (
                not isinstance(values, list)
                or not values
                or len(values) > maximum
                or any(
                    not isinstance(value, str)
                    or pattern.fullmatch(value) is None
                    for value in values)
                or len(values) != len(set(values))):
            raise ValueError(f'{description}が不正です')
        return list(values)

    @staticmethod
    def _validate_task_id(task_id):
        if not isinstance(task_id, str):
            raise ValueError('task_idが不正です')
        try:
            parsed = uuid.UUID(task_id)
        except (ValueError, AttributeError) as error:
            raise ValueError('task_idが不正です') from error
        if str(parsed) != task_id:
            raise ValueError('task_idが不正です')

    @classmethod
    def _validate_inputs(cls, task_id, bot_name, skip_image, force):
        cls._validate_task_id(task_id)
        if (
                not isinstance(bot_name, str)
                or cls._BOT_NAME_PATTERN.fullmatch(bot_name) is None):
            raise ValueError('Bot名が不正です')
        if type(skip_image) is not bool or type(force) is not bool:
            raise ValueError('ビルドオプションはboolで指定してください')

    @classmethod
    def _raise_launcher_error(cls, error):
        if isinstance(error, (BotoCoreError, ClientError)):
            raise TaskQueueError(cls._ERROR_MESSAGE) from error
        if type(error).__module__.startswith(('boto3.', 'botocore.')):
            raise TaskQueueError(cls._ERROR_MESSAGE) from error
        raise error

    def _client(self):
        if self._client_instance is None:
            try:
                options = {}
                if self._region is not None:
                    options['region_name'] = self._region
                self._client_instance = self._client_factory(
                    'ecs', **options)
            except Exception as error:
                self._raise_launcher_error(error)
        return self._client_instance

    @staticmethod
    def _command(task_id, bot_name, skip_image, force):
        command = [
            'python3',
            '-m',
            'build_job',
            f'--bot-name={bot_name}',
            f'--task-id={task_id}',
        ]
        if skip_image:
            command.append('--skip-image')
        if force:
            command.append('--force')
        return command

    def launch(
            self, task_id, bot_name, skip_image=False, force=False):
        """許可済みの引数だけを渡し、Fargateタスクを一件開始する。"""
        self._validate_inputs(task_id, bot_name, skip_image, force)
        request = {
            'cluster': self._cluster,
            'taskDefinition': self._task_definition,
            'count': 1,
            'launchType': 'FARGATE',
            'clientToken': task_id,
            'startedBy': task_id,
            'networkConfiguration': {
                'awsvpcConfiguration': {
                    'subnets': self._subnet_ids,
                    'securityGroups': self._security_group_ids,
                    'assignPublicIp': 'ENABLED',
                },
            },
            'overrides': {
                'containerOverrides': [{
                    'name': self._container_name,
                    'command': self._command(
                        task_id, bot_name, skip_image, force),
                }],
            },
        }
        try:
            response = self._client().run_task(**request)
        except Exception as error:
            self._raise_launcher_error(error)

        if (
                not isinstance(response, dict)
                or response.get('failures')
                or not isinstance(response.get('tasks'), list)
                or len(response['tasks']) != 1):
            raise TaskQueueError(self._ERROR_MESSAGE)
        return task_id
