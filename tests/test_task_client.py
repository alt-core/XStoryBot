import datetime
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Record:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeTimestamp:
    def __init__(self):
        self.value = None

    def FromDatetime(self, value):
        self.value = value


def load_task_client():
    tasks_v2 = types.ModuleType('google.cloud.tasks_v2')
    tasks_v2.HttpMethod = types.SimpleNamespace(POST='POST')
    tasks_v2.HttpRequest = Record
    tasks_v2.Task = Record
    tasks_v2.CreateTaskRequest = Record
    tasks_v2.CloudTasksClient = Mock(name='CloudTasksClient')

    service_account = types.ModuleType('google.oauth2.service_account')
    credentials_class = type('Credentials', (), {})
    credentials_class.from_service_account_file = Mock()
    service_account.Credentials = credentials_class

    timestamp_pb2 = types.ModuleType('google.protobuf.timestamp_pb2')
    timestamp_pb2.Timestamp = FakeTimestamp

    google = types.ModuleType('google')
    cloud = types.ModuleType('google.cloud')
    oauth2 = types.ModuleType('google.oauth2')
    protobuf = types.ModuleType('google.protobuf')
    google.cloud = cloud
    google.oauth2 = oauth2
    google.protobuf = protobuf
    cloud.tasks_v2 = tasks_v2
    oauth2.service_account = service_account
    protobuf.timestamp_pb2 = timestamp_pb2

    auth = types.ModuleType('auth')
    auth.get_api_token = Mock(return_value='shared-api-token')

    module_name = 'task_client_for_unit_test'
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / 'task_client.py')
    module = importlib.util.module_from_spec(spec)
    modules = {
        'google': google,
        'google.cloud': cloud,
        'google.cloud.tasks_v2': tasks_v2,
        'google.oauth2': oauth2,
        'google.oauth2.service_account': service_account,
        'google.protobuf': protobuf,
        'google.protobuf.timestamp_pb2': timestamp_pb2,
        'auth': auth,
    }
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)

    return module, types.SimpleNamespace(
        tasks_v2=tasks_v2,
        service_account=service_account,
        auth=auth,
    )


def make_settings(credentials_path=''):
    return {
        'project_id': 'test-project',
        'location': 'asia-northeast1',
        'credentials_path': credentials_path,
        'services': {
            'app': {'base_url': 'https://app.example.invalid'},
            'builder': {'base_url': 'https://builder.example.invalid'},
        },
    }


def make_client():
    client = Mock()
    client.queue_path.side_effect = (
        lambda project, location, queue:
        f'projects/{project}/locations/{location}/queues/{queue}')
    client.create_task.return_value = types.SimpleNamespace(
        name='projects/test-project/locations/asia-northeast1/'
             'queues/test-queue/tasks/generated-name')
    return client


class TaskClientInitializationTest(unittest.TestCase):
    def setUp(self):
        self.module, self.dependencies = load_task_client()

    def test_初期化前はclientを作らない(self):
        with self.assertRaisesRegex(ValueError, 'not initialized'):
            self.module.get_client()
        self.dependencies.tasks_v2.CloudTasksClient.assert_not_called()

    def test_鍵file未指定時はADCを使う(self):
        client = make_client()
        self.dependencies.tasks_v2.CloudTasksClient.return_value = client

        self.module.initialize(make_settings())

        self.assertIs(self.module.get_client(), client)
        self.assertIs(self.module.get_client(), client)
        self.dependencies.tasks_v2.CloudTasksClient.assert_called_once_with()
        loader = (
            self.dependencies.service_account.Credentials
            .from_service_account_file)
        loader.assert_not_called()

    def test_明示鍵fileを引き続き使える(self):
        credentials = object()
        loader = (
            self.dependencies.service_account.Credentials
            .from_service_account_file)
        loader.return_value = credentials
        client = make_client()
        self.dependencies.tasks_v2.CloudTasksClient.return_value = client

        self.module.initialize(make_settings('/keys/example.json'))

        self.assertIs(self.module.get_client(), client)
        loader.assert_called_once_with('/keys/example.json')
        self.dependencies.tasks_v2.CloudTasksClient.assert_called_once_with(
            credentials=credentials)

    def test_再初期化時は以前のclientを破棄する(self):
        first_client = make_client()
        second_client = make_client()
        self.dependencies.tasks_v2.CloudTasksClient.side_effect = [
            first_client, second_client]

        self.module.initialize(make_settings())
        self.assertIs(self.module.get_client(), first_client)
        self.module.initialize(make_settings())
        self.assertIs(self.module.get_client(), second_client)

        self.assertEqual(
            self.dependencies.tasks_v2.CloudTasksClient.call_count, 2)


class TaskCreationTest(unittest.TestCase):
    def setUp(self):
        self.module, self.dependencies = load_task_client()
        self.module.initialize(make_settings())
        self.client = make_client()

    def create_task(self, queue_name, url, params, delay_seconds=None):
        with patch.object(
                self.module, 'get_client', return_value=self.client):
            return self.module.create_task(
                queue_name, url, params, delay_seconds=delay_seconds)

    def test_全queueでtokenはheaderだけに入れる(self):
        cases = (
            ('action-queue', '/api/action',
             'https://app.example.invalid/api/action'),
            ('build-queue', '/api/build',
             'https://builder.example.invalid/api/build'),
            ('group-message-queue', '/api/group',
             'https://app.example.invalid/api/group'),
        )

        for queue_name, path, expected_url in cases:
            with self.subTest(queue_name=queue_name):
                params = {'user': 'mock:user-1', 'action': 'notice'}
                task_id = self.create_task(queue_name, path, params)
                request = self.client.create_task.call_args.args[0]
                task = request.task
                body = parse_qs(
                    task.http_request.body.decode('utf-8'),
                    keep_blank_values=True)

                self.assertEqual(task.http_request.url, expected_url)
                self.assertEqual(
                    task.http_request.headers['X-API-Token'],
                    'shared-api-token')
                self.assertNotIn('token', body)
                self.assertEqual(body['user'], ['mock:user-1'])
                self.assertEqual(body['action'], ['notice'])
                self.assertEqual(body['task_id'], [task_id])
                uuid.UUID(task_id)
                self.assertFalse(hasattr(task, 'name'))
                self.assertEqual(
                    params,
                    {'user': 'mock:user-1', 'action': 'notice'})

        self.client.task_path.assert_not_called()
        self.assertEqual(self.dependencies.auth.get_api_token.call_count, 3)

    def test_予約時刻はutcnowを基準にする(self):
        original_datetime = datetime.datetime

        class FixedDatetime(original_datetime):
            @classmethod
            def utcnow(cls):
                return cls(2026, 8, 11, 1, 2, 3)

        with patch.object(
                self.module.datetime, 'datetime', FixedDatetime):
            self.create_task(
                'action-queue', '/api/action', {'value': '1'},
                delay_seconds=90)

        request = self.client.create_task.call_args.args[0]
        self.assertEqual(
            request.task.schedule_time.value,
            original_datetime(2026, 8, 11, 1, 3, 33))

    def test_予約なしではschedule_timeを設定しない(self):
        self.create_task(
            'action-queue', '/api/action', {'value': '1'},
            delay_seconds=0)

        request = self.client.create_task.call_args.args[0]
        self.assertFalse(hasattr(request.task, 'schedule_time'))

    def test_Task登録例外をそのまま伝播する(self):
        error = RuntimeError('task registration failed')
        self.client.create_task.side_effect = error

        with self.assertRaises(RuntimeError) as raised:
            self.create_task(
                'action-queue', '/api/action', {'value': '1'})

        self.assertIs(raised.exception, error)

    def test_parameterとcredentialをlogへ出さない(self):
        loader = (
            self.dependencies.service_account.Credentials
            .from_service_account_file)
        loader.return_value = object()
        self.module.initialize(make_settings('/keys/example.json'))
        self.dependencies.auth.get_api_token.return_value = (
            'secret-header-token')
        with patch.object(self.module.logging, 'info') as log_info:
            self.create_task(
                'action-queue', '/api/action',
                {'action': 'secret-action-value'})

        messages = ' '.join(
            str(arg)
            for call in log_info.call_args_list
            for arg in call.args)
        self.assertNotIn('secret-header-token', messages)
        self.assertNotIn('secret-action-value', messages)
        self.assertNotIn('/keys/example.json', messages)


def load_common_commands(task_client):
    main = types.ModuleType('main')
    hub = types.ModuleType('hub')
    commands = types.ModuleType('commands')
    users = types.ModuleType('users')
    expression = types.ModuleType('expression')
    expression.Expression = type('Expression', (), {})
    requests = types.ModuleType('requests')
    requests.RequestException = type('RequestException', (Exception,), {})
    requests.post = Mock()
    requests.get = Mock()
    pytz = types.ModuleType('pytz')
    pytz.timezone = Mock()

    module_name = 'common_commands_for_task_test'
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / 'common_commands.py')
    module = importlib.util.module_from_spec(spec)
    modules = {
        'task_client': task_client,
        'main': main,
        'hub': hub,
        'commands': commands,
        'users': users,
        'expression': expression,
        'requests': requests,
        'pytz': pytz,
    }
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class CommonCommandTaskTest(unittest.TestCase):
    def test_action本文へtokenを複製しない(self):
        task_client = types.ModuleType('task_client')
        task_client.create_task = Mock(return_value='task-id')
        module = load_common_commands(task_client)
        user = Mock()
        user.serialize.return_value = 'mock:user-1'

        module.send_request('bot', user, 'notice', delay_secs=30)

        task_client.create_task.assert_called_once_with(
            queue_name='action-queue',
            url='/api/v1/bots/bot/action',
            params={
                'user': 'mock:user-1',
                'action': 'notice',
            },
            delay_seconds=30)
        self.assertNotIn('auth', module.__dict__)


if __name__ == '__main__':
    unittest.main()
