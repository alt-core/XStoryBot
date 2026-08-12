import copy
import unittest
from unittest.mock import Mock

import boto3
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber

from cloud_backend.aws.build_task_launcher import AwsBuildTaskLauncher
from cloud_backend.contracts import TaskQueueError
from build_job import create_parser


TASK_ID = '12345678-1234-4abc-8def-1234567890ab'
TASK_ARN = (
    'arn:aws:ecs:ap-northeast-1:000000000000:'
    'task/test-cluster/0123456789abcdef0')
AWS_SETTINGS = {
    'region': 'ap-northeast-1',
    'task_queue': {
        'build': {
            'cluster': (
                'arn:aws:ecs:ap-northeast-1:000000000000:'
                'cluster/test-cluster'),
            'task_definition': (
                'arn:aws:ecs:ap-northeast-1:000000000000:'
                'task-definition/test-builder:1'),
            'container_name': 'xstorybot',
            'subnet_ids': ['subnet-0123456789abcdef0'],
            'security_group_ids': ['sg-0123456789abcdef0'],
        },
    },
}


def make_client():
    return boto3.client(
        'ecs',
        region_name='ap-northeast-1',
        aws_access_key_id='test-access-key',
        aws_secret_access_key='test-secret-key',
        aws_session_token='test-session-token',
    )


def expected_request(skip_image=True, force=True):
    command = [
        'python3',
        '-m',
        'build_job',
        '--bot-name=test-bot',
        f'--task-id={TASK_ID}',
    ]
    if skip_image:
        command.append('--skip-image')
    if force:
        command.append('--force')
    return {
        'cluster': AWS_SETTINGS['task_queue']['build']['cluster'],
        'taskDefinition': (
            AWS_SETTINGS['task_queue']['build']['task_definition']),
        'count': 1,
        'launchType': 'FARGATE',
        'clientToken': TASK_ID,
        'startedBy': TASK_ID,
        'networkConfiguration': {
            'awsvpcConfiguration': {
                'subnets': ['subnet-0123456789abcdef0'],
                'securityGroups': ['sg-0123456789abcdef0'],
                'assignPublicIp': 'ENABLED',
            },
        },
        'overrides': {
            'containerOverrides': [{
                'name': 'xstorybot',
                'command': command,
            }],
        },
    }


class AwsBuildTaskLauncherTest(unittest.TestCase):
    def test_ECS_clientはlaunchまで生成しない(self):
        client = Mock()
        client.run_task.return_value = {
            'tasks': [{'taskArn': TASK_ARN}], 'failures': []}
        client_factory = Mock(return_value=client)

        launcher = AwsBuildTaskLauncher(
            AWS_SETTINGS, client_factory=client_factory)

        client_factory.assert_not_called()
        launcher.launch(TASK_ID, 'test-bot')
        client_factory.assert_called_once_with(
            'ecs', region_name='ap-northeast-1')

    def test_実botocore_StubberでRunTask形状を固定する(self):
        client = make_client()
        stubber = Stubber(client)
        stubber.add_response('run_task', {
            'tasks': [{
                'taskArn': TASK_ARN,
            }],
            'failures': [],
        }, expected_request())
        launcher = AwsBuildTaskLauncher(AWS_SETTINGS, client=client)

        with stubber:
            result = launcher.launch(
                TASK_ID,
                'test-bot',
                skip_image=True,
                force=True,
            )

        self.assertEqual(TASK_ID, result)

    def test_falseのoptionはcommandへ追加しない(self):
        client = Mock()
        client.run_task.return_value = {
            'tasks': [{'taskArn': TASK_ARN}], 'failures': []}
        launcher = AwsBuildTaskLauncher(AWS_SETTINGS, client=client)

        launcher.launch(TASK_ID, 'test-bot')

        self.assertEqual(
            expected_request(skip_image=False, force=False),
            client.run_task.call_args.kwargs,
        )

    def test_先頭ハイフンのBot名もCLIで正しく解析できる(self):
        client = Mock()
        client.run_task.return_value = {
            'tasks': [{'taskArn': TASK_ARN}], 'failures': []}
        launcher = AwsBuildTaskLauncher(AWS_SETTINGS, client=client)

        launcher.launch(TASK_ID, '-test-bot', force=True)

        command = client.run_task.call_args.kwargs[
            'overrides']['containerOverrides'][0]['command']
        args = create_parser().parse_args(command[3:])
        self.assertEqual('-test-bot', args.bot_name)
        self.assertEqual(TASK_ID, args.task_id)
        self.assertTrue(args.force)

    def test_task_idとBot名はSDK呼出し前に検証する(self):
        client_factory = Mock()
        launcher = AwsBuildTaskLauncher(
            AWS_SETTINGS, client_factory=client_factory)

        invalid_inputs = (
            ('not-a-uuid', 'test-bot'),
            (TASK_ID.upper(), 'test-bot'),
            (None, 'test-bot'),
            (TASK_ID, ''),
            (TASK_ID, '日本語'),
            (TASK_ID, 'a/b'),
            (TASK_ID, 'a.b'),
        )
        for task_id, bot_name in invalid_inputs:
            with self.subTest(task_id=task_id, bot_name=bot_name):
                with self.assertRaises(ValueError):
                    launcher.launch(task_id, bot_name)

        client_factory.assert_not_called()

    def test_optionはboolだけを受け付ける(self):
        client_factory = Mock()
        launcher = AwsBuildTaskLauncher(
            AWS_SETTINGS, client_factory=client_factory)

        invalid_options = (
            {'skip_image': 1},
            {'skip_image': 'true'},
            {'skip_image': None},
            {'force': 0},
            {'force': 'false'},
            {'force': None},
        )
        for options in invalid_options:
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    launcher.launch(TASK_ID, 'test-bot', **options)

        client_factory.assert_not_called()

    def test_必須設定は生成時に厳密に検証する(self):
        build_settings = AWS_SETTINGS['task_queue']['build']
        for key in (
                'cluster', 'task_definition', 'container_name',
                'subnet_ids', 'security_group_ids'):
            settings = copy.deepcopy(AWS_SETTINGS)
            del settings['task_queue']['build'][key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                AwsBuildTaskLauncher(settings)

        invalid_values = {
            'cluster': ' test-cluster',
            'task_definition': 'test task',
            'container_name': 'test.container',
            'subnet_ids': [],
            'security_group_ids': ['group-0123456789abcdef0'],
        }
        for key, value in invalid_values.items():
            settings = copy.deepcopy(AWS_SETTINGS)
            settings['task_queue']['build'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(
                    ValueError):
                AwsBuildTaskLauncher(settings)

        self.assertEqual(
            ['subnet-0123456789abcdef0'], build_settings['subnet_ids'])

    def test_network設定は重複とAWS上限超過を拒否する(self):
        invalid_lists = {
            'subnet_ids': [
                'subnet-0123456789abcdef0',
                'subnet-0123456789abcdef0',
            ],
            'security_group_ids': [
                f'sg-{index:017x}' for index in range(6)
            ],
        }
        for key, value in invalid_lists.items():
            settings = copy.deepcopy(AWS_SETTINGS)
            settings['task_queue']['build'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                AwsBuildTaskLauncher(settings)

    def test_SDK例外は機微値を含まない共通例外へ変換する(self):
        client = make_client()
        stubber = Stubber(client)
        stubber.add_client_error(
            'run_task',
            service_error_code='ServiceUnavailableException',
            service_message='secret failure detail',
            http_status_code=503,
            expected_params=expected_request(
                skip_image=False, force=False),
        )
        launcher = AwsBuildTaskLauncher(AWS_SETTINGS, client=client)

        with stubber, self.assertRaises(TaskQueueError) as raised:
            launcher.launch(TASK_ID, 'test-bot')

        self.assertEqual(
            'AWSビルドタスクの開始に失敗しました', str(raised.exception))
        self.assertNotIn('secret', str(raised.exception))

    def test_client生成時のSDK例外も共通例外へ変換する(self):
        error = EndpointConnectionError(
            endpoint_url='https://secret.example.invalid')
        launcher = AwsBuildTaskLauncher(
            AWS_SETTINGS,
            client_factory=Mock(side_effect=error),
        )

        with self.assertRaises(TaskQueueError) as raised:
            launcher.launch(TASK_ID, 'test-bot')

        self.assertNotIn('secret', str(raised.exception))

    def test_RunTaskのfailure内容を共通例外へ露出しない(self):
        client = Mock()
        client.run_task.return_value = {
            'tasks': [],
            'failures': [{
                'arn': 'arn:aws:ecs:region:account:task/secret-task',
                'reason': 'secret failure reason',
            }],
        }
        launcher = AwsBuildTaskLauncher(AWS_SETTINGS, client=client)

        with self.assertRaises(TaskQueueError) as raised:
            launcher.launch(TASK_ID, 'test-bot')

        self.assertEqual(
            'AWSビルドタスクの開始に失敗しました', str(raised.exception))
        self.assertNotIn('secret', str(raised.exception))

    def test_RunTaskが正常なtask一件以外を返した場合は失敗にする(self):
        for tasks in (
                [], [{}], [{'taskArn': ''}], [{}, {}], None):
            client = Mock()
            client.run_task.return_value = {
                'tasks': tasks,
                'failures': [],
            }
            launcher = AwsBuildTaskLauncher(AWS_SETTINGS, client=client)

            with self.subTest(tasks=tasks), self.assertRaises(TaskQueueError):
                launcher.launch(TASK_ID, 'test-bot')

    def test_application例外は同一instanceのまま返す(self):
        error = RuntimeError('application error')
        client = Mock()
        client.run_task.side_effect = error
        launcher = AwsBuildTaskLauncher(AWS_SETTINGS, client=client)

        with self.assertRaises(RuntimeError) as raised:
            launcher.launch(TASK_ID, 'test-bot')

        self.assertIs(error, raised.exception)


if __name__ == '__main__':
    unittest.main()
