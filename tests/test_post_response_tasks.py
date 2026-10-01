"""返信済みの親を子登録の失敗で再実行しないことを実経路で確認する。"""

import copy
import datetime
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from botocore.exceptions import ClientError

import common_commands
import context as action_context
import task_client
from cloud_backend.aws import task_handler
from cloud_backend.aws.state_store import AwsStateStore
from cloud_backend.aws.task_queue import AwsTaskQueue
from cloud_backend.contracts import ObjectNotFoundError, StateStoreError
from tests.test_aws_state_store import AWS_SETTINGS, _MemoryDynamoClient, _MemoryObjectStore
from tests.test_group_message_batching import (
    load_group_message_task_db,
    load_group_message_task_manager,
)
from tests.test_task_client import FakeCredentialSource, load_task_queue, make_client, make_settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MemoryUser:
    service_name = 'line'

    def __init__(self, user_id='user-1'):
        self.user_id = user_id

    def serialize(self):
        return f'line:{self.user_id}'

    def __str__(self):
        return self.serialize()

    @classmethod
    def deserialize(cls, value):
        if not value.startswith('line:'):
            return None
        return cls(value.split(':', 1)[1])


class MemoryPrivateObjects:
    def __init__(self):
        self.objects = {}

    def store_private(self, key, data, content_type=None):
        self.objects[key] = data.encode() if isinstance(data, str) else bytes(data)
        return f'opaque://{key}'

    def load_private(self, key):
        if key not in self.objects:
            raise ObjectNotFoundError('missing')
        return self.objects[key]


class PostResponseTaskTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.persisted = {}
        self.children = []
        self.fail_first_child = True
        self.failure_phase = None
        self.common = common_commands.CommonCommands_Runtime({'reset_keyword': '!reset'})
        settings = ModuleType('settings')
        settings.CONSTANTS = {}
        spec = importlib.util.spec_from_file_location(
            'tests._post_response_runtime', PROJECT_ROOT / 'runtime.py')
        self.runtime = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'settings': settings}):
            spec.loader.exec_module(self.runtime)
        test = self

        class MemoryStatus(dict):
            @property
            def scene(self):
                return self.get('scene', 'scene-0')

            def __init__(self, namespace, user_id):
                super().__init__(copy.deepcopy(test.persisted.get(user_id, {})))
                self.user_id = user_id
                test.events.append(('load', self.get('counter', 0)))

            def save(self):
                test.events.append(('save', self.get('counter', 0)))
                if test.failure_phase == 'save':
                    raise RuntimeError('save failed')
                test.persisted[self.user_id] = copy.deepcopy(dict(self))

            def rollback(self):
                test.events.append(('rollback',))
                test.persisted[self.user_id] = {'counter': self.get('counter', 0) - 1}

        self.status_class = MemoryStatus

        class Interface:
            def get_retry_count(self):
                return 0

            def create_context(self, user, action, attrs):
                return action_context.ActionContext('bot', 'line', self, user, action, attrs)

            def respond_reaction(self, context, reactions):
                test.events.append(('reply', context.status.get('counter')))
                if test.failure_phase == 'send':
                    raise RuntimeError('send failed')
                return 'OK'

        self.interface = Interface()
        self.bot = self.runtime.BotRuntime('bot', {'line': self.interface}, None)
        self.bot.scenario = SimpleNamespace(version=3, constants={})
        self.bot.check_reload = Mock()
        self.main = ModuleType('main')
        self.main.get_bot = lambda _name: self.bot

        class Director:
            def __init__(self, scenario, context):
                self.context = context

            def plan_reactions(self):
                context = self.context
                counter = context.status.get('counter', 0) + 1
                context.status['counter'] = counter
                context.status['scene'] = f'scene-{counter}'
                test.events.append(('plan', counter))
                if test.failure_phase == 'plan':
                    raise RuntimeError('plan failed')
                for index in range(2):
                    test.common.run_command(
                        context, None, '@forward', ['child-bot', f'#child-{counter}-{index}'])

        self.runtime._director_class = Director
        self.sdk = Mock()
        self.sdk.invoke.side_effect = self.invoke_child
        self.queue = AwsTaskQueue(client_factory=lambda *_args, **_kwargs: self.sdk)
        self.queue.initialize({'task_queue': {'functions': {
            'action-queue': 'test-action', 'group-message-queue': 'test-group',
        }}})
        self.execution_client = _MemoryDynamoClient()
        self.execution_store = AwsStateStore(
            AWS_SETTINGS, client=self.execution_client, object_store=_MemoryObjectStore())
        self.dependencies = {
            'get_bot': lambda _name: self.bot, 'user_class': MemoryUser,
            'get_group_members': lambda _name: [], 'options': {},
            'manager_class': Mock(), 'execution_store': self.execution_store,
            'worker_kind': 'action',
        }
        task_id = str(uuid.uuid4())
        self.envelope = {
            'version': 1, 'task_id': task_id, 'queue_name': 'action-queue',
            'kind': 'action', 'bot_name': 'bot',
            'params': {'task_id': task_id, 'user': 'line:user-1', 'action': 'start'},
        }

    def invoke_child(self, **request):
        envelope = json.loads(request['Payload'])
        self.children.append(envelope)
        self.events.append(('enqueue', envelope['params']['action']))
        if self.fail_first_child and len(self.children) == 1:
            raise ClientError({'Error': {
                'Code': 'ServiceUnavailable', 'Message': 'simulated registration failure',
            }}, 'Invoke')
        return {'StatusCode': 202}

    def run_in_runtime(self, function):
        with patch.object(task_client, '_task_queue', self.queue), \
                patch.object(action_context, '_player_status_class', self.status_class), \
                patch.dict(sys.modules, {'main': self.main}), \
                patch.object(task_handler, '_load_dependencies', return_value=self.dependencies):
            return function()

    def invoke_worker(self):
        return self.run_in_runtime(lambda: task_handler.lambda_handler(self.envelope, None))

    def test_子登録失敗でもworkerは親を完了し同じ封筒を再実行しない(self):
        with self.assertLogs(level='ERROR') as logs:
            self.assertEqual({'status': 'ok'}, self.invoke_worker())
        self.assertEqual({'status': 'ok'}, self.invoke_worker())

        self.assertEqual([('reply', 1)], [event for event in self.events if event[0] == 'reply'])
        self.assertEqual(['#child-1-0', '#child-1-1'], [child['params']['action'] for child in self.children])
        self.assertEqual(1, self.persisted['line:user-1']['counter'])
        record = json.loads(next(log.getMessage() for log in logs.records if log.getMessage().startswith('{')))
        self.assertEqual('enqueue', record['phase'])
        self.assertEqual({
            'bot': 'child-bot', 'action': '#child-1-0', 'interface': 'line',
            'user': 'line:user-1', 'delay_seconds': None,
            'task_id': self.children[0]['task_id'],
        }, record['task'])
        self.assertNotIn('delete_item', [name for name, request in self.execution_client.calls])

    def test_plan保存返信の失敗は引き続き親の再試行対象になる(self):
        for phase in ('plan', 'save', 'send'):
            with self.subTest(phase=phase):
                self.failure_phase = phase
                with self.assertLogs(level='ERROR'), self.assertRaises(task_handler.ActionNotDelivered):
                    self.invoke_worker()
                self.assertEqual([], self.children)
                self.assertEqual({}, self.execution_client.tables['test-cache'])
        self.failure_phase = None

    def test_完了保存の例外は子登録成功後もworkerへ伝播する(self):
        self.fail_first_child = False
        with patch.object(self.execution_store, 'complete_task_execution',
                          side_effect=StateStoreError('completion failed')), \
                self.assertLogs(level='ERROR'), self.assertRaises(StateStoreError):
            self.invoke_worker()
        self.assertEqual([('reply', 1)], [event for event in self.events if event[0] == 'reply'])
        self.assertEqual(2, len(self.children))
        self.assertEqual({}, self.execution_client.tables['test-cache'])

    def test_子登録失敗でも返信済みgroupメンバーの失敗者再送を作らない(self):
        manager_module = load_group_message_task_manager()
        db_module = load_group_message_task_db()
        manager_module.GroupMessageTaskDB = db_module.GroupMessageTaskDB
        manager_module.users.User = MemoryUser
        manager_module.users.get_group_members.return_value = [MemoryUser()]
        db = db_module.GroupMessageTaskDB
        tasks = {'message-1': {
            'bot_name': 'bot', 'group_id': 'group-1', 'action': 'start', 'attrs': {},
            'status': db.STATUS_PENDING, 'current_batch': 0, 'processed_members': 0,
            'successful_members': 0, 'failed_members': 0, 'total_members': 1,
            'total_batches': 1, 'error_messages': [],
        }}
        store = Mock()
        store.get_group_message_task.side_effect = lambda task_id: copy.deepcopy(tasks.get(task_id))

        def update(task_id, callback):
            if task_id not in tasks:
                return False
            tasks[task_id].update(callback(copy.deepcopy(tasks[task_id])))
            return True

        store.update_group_message_task.side_effect = update
        store.create_group_message_task.side_effect = lambda task_id, data: tasks.update({task_id: data})
        db._state_store = store
        db._object_store = MemoryPrivateObjects()
        manager = manager_module.GroupMessageTaskManager('bot', bot_instance=self.bot)

        def sequential_members(batch_id, member_ids, task, workers, rate):
            successful = []
            errors = []
            for member_id in member_ids:
                success, error = manager._process_member(member_id, task)
                (successful if success else errors).append(member_id if success else (member_id, error))
            return len(successful), len(errors), successful, errors

        manager._process_batch_members = sequential_members
        with self.assertLogs(level='ERROR'):
            result, status = self.run_in_runtime(lambda: manager.handle_batch_process_request('message-1', 0, 1, 1, 100))

        self.assertEqual(200, status)
        self.assertEqual(1, result['success_count'])
        self.assertEqual(0, result['error_count'])
        self.assertEqual(0, tasks['message-1']['failed_members'])
        self.assertIsNone(db.retry_failed_members('message-1', 'tester'))
        self.assertEqual([('reply', 1)], [event for event in self.events if event[0] == 'reply'])
        self.assertEqual(2, len(self.children))

    def test_GCPの正delayも親返信成功後の時刻から登録する(self):
        module, dependencies = load_task_queue()
        self.queue = module.GcpTaskQueue(credential_source=FakeCredentialSource())
        self.queue.initialize(make_settings())
        client = make_client()
        self.queue._client = client
        original_datetime = datetime.datetime
        clock = [original_datetime(2026, 10, 1, 1, 2, 3)]

        class FixedDatetime(original_datetime):
            @classmethod
            def utcnow(cls):
                return clock[0]

        original_respond = self.interface.respond_reaction

        def respond(context, reactions):
            result = original_respond(context, reactions)
            clock[0] += datetime.timedelta(seconds=30)
            return result

        self.interface.respond_reaction = respond
        common = self.common

        class Director:
            def __init__(self, scenario, context):
                self.context = context

            def plan_reactions(self):
                common.run_command(self.context, None, '@delay', ['90', 'child-bot', '#next'])

        self.runtime._director_class = Director
        with patch.object(module.datetime, 'datetime', FixedDatetime):
            self.assertEqual('OK', self.run_in_runtime(lambda: self.bot.handle_action(
                self.interface.create_context(MemoryUser(), 'start', {}))))
        self.assertEqual(
            original_datetime(2026, 10, 1, 1, 4, 3),
            client.create_task.call_args.args[0].task.schedule_time.value,
        )
        self.assertFalse(hasattr(client.create_task.call_args.args[0].task, 'name'))


if __name__ == '__main__':
    unittest.main()
