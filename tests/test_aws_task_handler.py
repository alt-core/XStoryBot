import unittest
from unittest.mock import Mock, call, patch
import uuid

from botocore.exceptions import EndpointConnectionError

from cloud_backend.aws import task_handler
from cloud_backend.aws.state_store import (
    TASK_EXECUTION_BUSY,
    TASK_EXECUTION_CLAIMED,
    TASK_EXECUTION_COMPLETED,
    AwsStateStore,
)
from cloud_backend.contracts import StateStoreError
from tests.test_aws_state_store import (
    AWS_SETTINGS,
    _MemoryDynamoClient,
    _MemoryObjectStore,
)


OWNER_UUID = uuid.UUID('aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')


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

    def __str__(self):
        return f'{self.service_name}:{self.user_id}'


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
        'user': 'plaintext:user-1', 'action': 'hello', 'task_id': task_id,
    }
    default_params.update(params)
    return {
        'version': 1, 'task_id': task_id, 'queue_name': queue_name,
        'kind': kind, 'bot_name': 'bot', 'params': default_params,
    }


def make_group_envelope(**params):
    return make_envelope(
        kind='group_batch', queue_name='group-message-queue',
        message_task_id=params.pop('message_task_id', 'message-1'),
        batch_index=params.pop('batch_index', 2), **params)


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
            'get_bot': Mock(return_value=self.bot), 'user_class': FakeUser,
            'get_group_members': Mock(return_value=[]), 'options': {},
            'manager_class': Mock(return_value=self.manager),
            'execution_store': self.execution_store,
            'worker_kind': 'action',
        }

    def invoke(self, envelope, worker_kind='action', owner=OWNER_UUID):
        dependencies = dict(self.dependencies, worker_kind=worker_kind)
        with patch.object(
                task_handler, '_load_dependencies', return_value=dependencies), \
                patch.object(task_handler.uuid, 'uuid4', return_value=owner):
            return task_handler.lambda_handler(envelope, None)

    def test_直接の封筒を共通action処理へ渡す(self):
        envelope = make_envelope(action='hello@@action-token')
        self.assertEqual({'status': 'ok'}, self.invoke(envelope))
        self.assertEqual(self.bot.handled[0][1], 'hello')
        self.assertEqual(self.bot.handled[0][2], {'action_token': 'action-token'})
        key = f'action:bot:{envelope["task_id"]}'
        self.execution_store.try_claim_task_execution.assert_called_once_with(
            key, str(OWNER_UUID), 360)
        self.execution_store.complete_task_execution.assert_called_once_with(
            key, str(OWNER_UUID))
        self.execution_store.release_task_execution.assert_not_called()

    def test_groupは論理batchを共通processorへ渡す(self):
        envelope = make_group_envelope(batch_index='2')
        self.assertEqual(
            {'status': 'ok'}, self.invoke(envelope, 'group_batch'))
        self.dependencies['manager_class'].assert_called_once_with(
            'bot', bot_instance=self.bot)
        self.manager.handle_batch_process_request.assert_called_once_with(
            'message-1', 2)
        self.execution_store.try_claim_task_execution.assert_called_once_with(
            'group:bot:message-1:2', str(OWNER_UUID), 960)
        self.execution_store.complete_task_execution.assert_called_once_with(
            'group:bot:message-1:2', str(OWNER_UUID))

    def test_interface指定は利用者IDを変更せず不正指定を既定へ戻さない(self):
        self.invoke(make_envelope(user='line:user,example', interface='plaintext'))
        self.assertEqual('line:user,example', str(self.bot.handled[0][0]))
        for interface in ('missing', []):
            with self.subTest(interface=interface), self.assertRaises(Exception):
                self.invoke(make_envelope(interface=interface))
        self.assertEqual(1, len(self.bot.handled))
        self.assertEqual(1, self.execution_store.complete_task_execution.call_count)
        self.assertEqual(2, self.execution_store.release_task_execution.call_count)

    def test_ownerは呼出しごとのUUIDでcontextへ依存しない(self):
        envelope = make_envelope()
        with patch.object(
                task_handler, '_load_dependencies', return_value=self.dependencies):
            task_handler.lambda_handler(envelope, None)
            task_handler.lambda_handler(envelope, None)
        owners = [args.args[1] for args in
                  self.execution_store.try_claim_task_execution.call_args_list]
        for owner in owners:
            self.assertEqual(owner, str(uuid.UUID(owner)))
        self.assertNotEqual(owners[0], owners[1])

    def test_groupは別task_idでも同じ論理batchなら完了済みをskipする(self):
        first = make_group_envelope()
        requeued = make_group_envelope()
        self.execution_store.try_claim_task_execution.side_effect = [
            TASK_EXECUTION_CLAIMED, TASK_EXECUTION_COMPLETED]
        self.invoke(first, 'group_batch')
        self.invoke(requeued, 'group_batch')
        self.manager.handle_batch_process_request.assert_called_once_with(
            'message-1', 2)
        self.assertEqual([
            call('group:bot:message-1:2', str(OWNER_UUID), 960),
            call('group:bot:message-1:2', str(OWNER_UUID), 960),
        ], self.execution_store.try_claim_task_execution.call_args_list)
        self.execution_store.complete_task_execution.assert_called_once()

    def test_実行中は例外で再試行し完了済みは成功としてskipする(self):
        self.execution_store.try_claim_task_execution.return_value = TASK_EXECUTION_BUSY
        with self.assertRaises(task_handler._TaskExecutionBusy):
            self.invoke(make_envelope())
        self.execution_store.try_claim_task_execution.return_value = TASK_EXECUTION_COMPLETED
        self.assertEqual({'status': 'ok'}, self.invoke(make_envelope()))
        self.assertEqual([], self.bot.handled)
        self.execution_store.complete_task_execution.assert_not_called()
        self.execution_store.release_task_execution.assert_not_called()

    def test_完了済みtaskはBotを取得せず成功としてskipする(self):
        self.execution_store.try_claim_task_execution.return_value = TASK_EXECUTION_COMPLETED
        self.dependencies['get_bot'].side_effect = RuntimeError('Bot初期化失敗')
        self.assertEqual({'status': 'ok'}, self.invoke(make_envelope()))
        self.dependencies['get_bot'].assert_not_called()
        self.execution_store.release_task_execution.assert_not_called()

    def test_業務処理失敗時はleaseを解放して元の例外を伝播する(self):
        envelope = make_envelope(action='failure')
        failure = RuntimeError('failure')
        self.bot.handle_action = Mock(side_effect=failure)
        with self.assertRaises(RuntimeError) as raised:
            self.invoke(envelope)
        self.assertIs(failure, raised.exception)
        self.execution_store.complete_task_execution.assert_not_called()
        self.execution_store.release_task_execution.assert_called_once_with(
            f'action:bot:{envelope["task_id"]}', str(OWNER_UUID))

    def test_handle_actionがNoneならleaseを解放して再試行する(self):
        envelope = make_envelope(action='exhausted')
        self.bot.handle_action = Mock(return_value=None)
        with self.assertRaises(task_handler.ActionNotDelivered):
            self.invoke(envelope)
        self.execution_store.complete_task_execution.assert_not_called()
        self.execution_store.release_task_execution.assert_called_once()

    def test_groupの非200応答はleaseを解放して例外を伝播する(self):
        self.manager.handle_batch_process_request.return_value = (
            {'error': '処理失敗'}, 500)
        with self.assertRaisesRegex(Exception, '処理失敗'):
            self.invoke(make_group_envelope(), 'group_batch')
        self.execution_store.complete_task_execution.assert_not_called()
        self.execution_store.release_task_execution.assert_called_once_with(
            'group:bot:message-1:2', str(OWNER_UUID))

    def test_完了保存失敗も同じtry内でleaseを解放する(self):
        failure = RuntimeError('完了保存失敗')
        self.execution_store.complete_task_execution.side_effect = failure
        envelope = make_envelope()
        with self.assertRaises(RuntimeError) as raised:
            self.invoke(envelope)
        self.assertIs(failure, raised.exception)
        self.assertEqual(1, len(self.bot.handled))
        self.assertEqual([
            call.try_claim_task_execution(
                f'action:bot:{envelope["task_id"]}', str(OWNER_UUID), 360),
            call.complete_task_execution(
                f'action:bot:{envelope["task_id"]}', str(OWNER_UUID)),
            call.release_task_execution(
                f'action:bot:{envelope["task_id"]}', str(OWNER_UUID)),
        ], self.execution_store.mock_calls)

    def test_lease解放失敗が元の例外を上書きしない(self):
        failure = RuntimeError('業務処理失敗')
        self.bot.handle_action = Mock(side_effect=failure)
        self.execution_store.release_task_execution.side_effect = RuntimeError('解放失敗')
        with self.assertRaises(RuntimeError) as raised:
            self.invoke(make_envelope())
        self.assertIs(failure, raised.exception)

    def test_claim失敗では同じownerのleaseだけを条件付き解放する(self):
        failure = RuntimeError('取得失敗')
        self.execution_store.try_claim_task_execution.side_effect = failure
        envelope = make_envelope()
        with self.assertRaises(RuntimeError) as raised:
            self.invoke(envelope)
        self.assertIs(failure, raised.exception)
        self.execution_store.release_task_execution.assert_called_once_with(
            f'action:bot:{envelope["task_id"]}', str(OWNER_UUID))
        self.assertEqual([], self.bot.handled)

    def test_claim保存後の応答喪失でも解放して次の試行で実行できる(self):
        client = _MemoryDynamoClient()
        store = AwsStateStore(
            AWS_SETTINGS, client=client, object_store=_MemoryObjectStore())
        self.dependencies['execution_store'] = store
        envelope = make_envelope()
        put_item = client.put_item
        lost_response = [True]

        def put_then_lose_response(**request):
            result = put_item(**request)
            if lost_response[0]:
                lost_response[0] = False
                raise EndpointConnectionError(endpoint_url='https://test.invalid')
            return result

        with patch.object(client, 'put_item', side_effect=put_then_lose_response):
            with self.assertRaises(StateStoreError):
                self.invoke(envelope)
            self.assertEqual({}, client.tables['test-cache'])
            self.assertEqual([], self.bot.handled)
            retry_owner = uuid.UUID('bbbbbbbb-cccc-dddd-eeee-ffffffffffff')
            self.assertEqual(
                {'status': 'ok'}, self.invoke(envelope, owner=retry_owner))

        self.assertEqual(1, len(self.bot.handled))
        item = store._decode_item(next(iter(client.tables['test-cache'].values())))
        self.assertEqual(TASK_EXECUTION_COMPLETED, item['status'])
        self.assertEqual(str(retry_owner), item['owner'])
        self.assertEqual(
            ['put_item', 'delete_item', 'put_item', 'put_item'],
            [operation for operation, request in client.calls],
        )

    def test_claim結果の読取失敗でも他ownerや完了記録を壊さない(self):
        for status in (TASK_EXECUTION_CLAIMED, TASK_EXECUTION_COMPLETED):
            with self.subTest(status=status):
                client = _MemoryDynamoClient()
                store = AwsStateStore(
                    AWS_SETTINGS, client=client, object_store=_MemoryObjectStore())
                self.dependencies['execution_store'] = store
                envelope = make_envelope()
                key = f'action:bot:{envelope["task_id"]}'
                store.try_claim_task_execution(key, 'other-owner', 360)
                if status == TASK_EXECUTION_COMPLETED:
                    store.complete_task_execution(key, 'other-owner')
                failure = EndpointConnectionError(endpoint_url='https://test.invalid')

                with patch.object(client, 'get_item', side_effect=failure), \
                        self.assertRaises(StateStoreError):
                    self.invoke(envelope)

                item = store._decode_item(
                    next(iter(client.tables['test-cache'].values())))
                self.assertEqual('other-owner', item['owner'])
                self.assertEqual(status, item['status'])
                self.assertEqual('delete_item', client.calls[-1][0])
                self.assertEqual([], self.bot.handled)

    def test_group展開の一部失敗はtask全体を失敗にしない(self):
        self.dependencies['get_group_members'].return_value = [
            FakeUser('plaintext', 'member-1'), FakeUser('plaintext', 'member-2')]
        self.bot.handle_action = Mock(side_effect=[None, 'ok'])
        self.assertEqual(
            {'status': 'ok'}, self.invoke(make_envelope(user='group:g1')))
        self.execution_store.complete_task_execution.assert_called_once()
        self.execution_store.release_task_execution.assert_not_called()

    def test_action内のgroup警告は詳細を残す(self):
        envelope = make_envelope(user='group:group-1', action='group-action')
        self.dependencies['get_group_members'].return_value = [
            FakeUser('unknown', 'member-1')]
        with patch.object(task_handler.logging, 'warning') as warning_log:
            self.invoke(envelope)
        warning_log.assert_called_once_with(
            'interface not found: unknown:member-1 group-action')

    def test_封筒の不備はclaim前に拒否する(self):
        envelopes = [None, [], {'Records': []}]
        for field, value in (
                ('version', True), ('version', 2), ('task_id', 'invalid'),
                ('queue_name', 'unknown'), ('queue_name', []),
                ('kind', 'unknown'), ('bot_name', 'bot/name'), ('params', [])):
            invalid = make_envelope()
            invalid[field] = value
            envelopes.append(invalid)
        mismatched_id = make_envelope()
        mismatched_id['params']['task_id'] = str(uuid.uuid4())
        envelopes.append(mismatched_id)
        for envelope in envelopes:
            with self.subTest(envelope=envelope), self.assertRaises(ValueError):
                self.invoke(envelope)
        self.execution_store.try_claim_task_execution.assert_not_called()
        self.assertEqual([], self.bot.handled)

    def test_worker種別を取り違えた呼出しはclaim前に拒否する(self):
        for envelope, worker_kind in (
                (make_envelope(), 'group_batch'),
                (make_group_envelope(), 'action'),
                (make_envelope(), ''), (make_envelope(), 'unknown')):
            with self.subTest(worker_kind=worker_kind), self.assertRaises(ValueError):
                self.invoke(envelope, worker_kind)
        self.execution_store.try_claim_task_execution.assert_not_called()
        self.dependencies['get_bot'].assert_not_called()

    def test_groupの不正parameterはclaim前に拒否する(self):
        for params in (
                {'batch_index': 1.9}, {'batch_index': True},
                {'batch_index': '-1'}, {'batch_index': '01'},
                {'message_task_id': ''}, {'message_task_id': []}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.invoke(make_group_envelope(**params), 'group_batch')
        self.execution_store.try_claim_task_execution.assert_not_called()

    def test_Bot未登録でも検証済みaction詳細をログに残す(self):
        envelope = make_envelope(action='missing-bot-action')
        self.dependencies['get_bot'].return_value = None
        with patch.object(task_handler.logging, 'info') as info_log, \
                self.assertRaises(ValueError):
            self.invoke(envelope)
        info_log.assert_any_call(
            'AWS action task: task_id=%s, bot_name=%s, user=%s, '
            'action=%s, owner=%s',
            envelope['task_id'], 'bot', 'plaintext:user-1',
            'missing-bot-action', str(OWNER_UUID))
        self.execution_store.release_task_execution.assert_called_once_with(
            f'action:bot:{envelope["task_id"]}', str(OWNER_UUID))

    def test_actionの詳細と完了を運用ログに残す(self):
        envelope = make_envelope(action='hello')
        envelope['api_token'] = 'api-token-must-not-be-logged'
        with patch.object(task_handler.logging, 'info') as info_log:
            self.invoke(envelope)
        info_log.assert_any_call(
            'AWS action task: task_id=%s, bot_name=%s, user=%s, '
            'action=%s, owner=%s',
            envelope['task_id'], 'bot', 'plaintext:user-1', 'hello', str(OWNER_UUID))
        info_log.assert_any_call(
            'AWS action task completed: task_id=%s, owner=%s',
            envelope['task_id'], str(OWNER_UUID))
        self.assertNotIn(envelope['api_token'], ' '.join(
            str(value) for log_call in info_log.call_args_list
            for value in log_call.args))

    def test_検証前の不正envelope値は詳細ログに出さない(self):
        untrusted = 'untrusted-kind-value'
        envelope = make_envelope(kind=untrusted)
        with patch.object(task_handler.logging, 'info') as info_log, \
                patch.object(task_handler.logging, 'exception') as error_log, \
                self.assertRaises(ValueError):
            self.invoke(envelope)
        self.assertNotIn(untrusted, ' '.join(
            str(value) for log_call in
            info_log.call_args_list + error_log.call_args_list
            for value in log_call.args))

    def test_不正batch_indexの変換元をtracebackへ出さない(self):
        untrusted = 'untrusted-batch-index'
        with self.assertLogs(level='ERROR') as captured, \
                self.assertRaises(ValueError):
            self.invoke(
                make_group_envelope(batch_index=untrusted), 'group_batch')
        self.assertNotIn(untrusted, '\n'.join(captured.output))

    def test_処理例外を運用ログに残して伝播する(self):
        failure = RuntimeError('failed action')
        self.bot.handle_action = Mock(side_effect=failure)
        with patch.object(task_handler.logging, 'exception') as error_log, \
                self.assertRaises(RuntimeError):
            self.invoke(make_envelope())
        error_log.assert_called_once_with(
            'AWS task failed: owner=%s, error_type=%s, error=%s',
            str(OWNER_UUID), 'RuntimeError', failure)
