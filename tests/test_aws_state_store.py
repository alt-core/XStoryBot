import datetime
from decimal import Decimal
import hashlib
import re
import sys
import types
import unittest
import uuid
from unittest.mock import Mock, patch

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError, NoCredentialsError, ReadTimeoutError
from botocore.stub import Stubber

from cloud_backend import aws as aws_backend
from cloud_backend.contracts import (
    ObjectNotFoundError,
    StateConflictError,
    StateStoreError,
    StateVersion,
)
from tests.cloud_backend.state_store_contract import StateStoreContractMixin


AWS_SETTINGS = {
    'region': 'test-region-1',
    'state_store': {
        'state_table': 'test-state',
        'group_task_table': 'test-group-task',
        'group_task_index': 'test-bot-created-at-index',
        'cache_table': 'test-cache',
        'player_max_bytes': 358400,
    },
}


class _MemoryObjectStore:
    """build cacheの外部化だけを扱うObjectStore fake。"""

    def __init__(self):
        self.objects = {}
        self.stored_keys = []

    def store_private(self, key, data, content_type=None):
        del content_type
        self.objects[key] = bytes(data)
        self.stored_keys.append(key)
        return f'opaque://{key}'

    def load_private(self, key):
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]


class _MemoryDynamoClient:
    """AwsStateStoreが使う低水準DynamoDB APIの状態保持fake。"""

    def __init__(self):
        self.tables = {
            'test-state': {},
            'test-group-task': {},
            'test-cache': {},
        }
        self.calls = []
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    def _record(self, operation, request):
        self.calls.append((operation, dict(request)))

    def get_item(self, **request):
        self._record('get_item', request)
        table = self.tables[request['TableName']]
        key = self._key(request['Key'])
        item = table.get(key)
        return {} if item is None else {'Item': self._copy(item)}

    def put_item(self, **request):
        self._record('put_item', request)
        table = self.tables[request['TableName']]
        item = self._copy(request['Item'])
        key = self._key_from_item(item)
        current = table.get(key)
        if not self._condition_matches(current, request):
            raise self._conditional_error('PutItem')
        table[key] = item
        if request.get('ReturnValues') == 'ALL_OLD' and current is not None:
            return {'Attributes': self._copy(current)}
        return {}

    def delete_item(self, **request):
        self._record('delete_item', request)
        table = self.tables[request['TableName']]
        key = self._key(request['Key'])
        current = table.get(key)
        if not self._condition_matches(current, request):
            raise self._conditional_error('DeleteItem')
        removed = table.pop(key, None)
        if request.get('ReturnValues') == 'ALL_OLD' and removed is not None:
            return {'Attributes': self._copy(removed)}
        return {}

    def update_item(self, **request):
        self._record('update_item', request)
        table = self.tables[request['TableName']]
        key = self._key(request['Key'])
        current = table.get(key)
        if not self._condition_matches(current, request):
            raise self._conditional_error('UpdateItem')
        item = self._copy(current or request['Key'])
        self._apply_update(item, request)
        table[key] = item
        if request.get('ReturnValues') in ('ALL_NEW', 'UPDATED_NEW'):
            return {'Attributes': self._copy(item)}
        return {}

    def query(self, **request):
        self._record('query', request)
        items = list(self.tables[request['TableName']].values())
        items = [item for item in items if self._condition_matches(
            item, request, expression_key='KeyConditionExpression')]
        items = [item for item in items if self._condition_matches(
            item, request, expression_key='FilterExpression')]
        sort_name = self._query_sort_name(request, items)
        if sort_name is not None:
            items.sort(
                key=lambda item: self._decoded_value(item.get(sort_name)),
                reverse=not request.get('ScanIndexForward', True),
            )
        limit = request.get('Limit')
        if limit is not None:
            items = items[:limit]
        return {'Items': [self._copy(item) for item in items]}

    def scan(self, **request):
        self._record('scan', request)
        items = list(self.tables[request['TableName']].values())
        items = [item for item in items if self._condition_matches(
            item, request, expression_key='FilterExpression')]
        return {'Items': [self._copy(item) for item in items]}

    def transact_write_items(self, **request):
        self._record('transact_write_items', request)
        original_tables = self._copy(self.tables)
        try:
            for action in request['TransactItems']:
                if 'Put' in action:
                    self.put_item(**action['Put'])
                elif 'Update' in action:
                    self.update_item(**action['Update'])
                elif 'Delete' in action:
                    self.delete_item(**action['Delete'])
                elif 'ConditionCheck' in action:
                    check = action['ConditionCheck']
                    item = self.tables[check['TableName']].get(
                        self._key(check['Key']))
                    if not self._condition_matches(item, check):
                        raise self._conditional_error('TransactWriteItems')
                else:
                    raise AssertionError(f'未対応のtransactionです: {action!r}')
        except Exception:
            self.tables = original_tables
            raise
        return {}

    def batch_write_item(self, **request):
        self._record('batch_write_item', request)
        for table_name, actions in request['RequestItems'].items():
            for action in actions:
                if 'PutRequest' in action:
                    self.put_item(
                        TableName=table_name,
                        Item=action['PutRequest']['Item'],
                    )
                elif 'DeleteRequest' in action:
                    self.delete_item(
                        TableName=table_name,
                        Key=action['DeleteRequest']['Key'],
                    )
                else:
                    raise AssertionError(f'未対応のbatchです: {action!r}')
        return {'UnprocessedItems': {}}

    def _condition_matches(
            self, item, request, expression_key='ConditionExpression'):
        expression = request.get(expression_key)
        if not expression:
            return True
        names = request.get('ExpressionAttributeNames', {})
        values = request.get('ExpressionAttributeValues', {})
        return self._evaluate_condition(expression, item, names, values)

    def _evaluate_condition(self, expression, item, names, values):
        expression = self._strip_parentheses(expression.strip())
        parts = self._split_boolean(expression, 'OR')
        if len(parts) > 1:
            return any(self._evaluate_condition(part, item, names, values)
                       for part in parts)
        parts = self._split_boolean(expression, 'AND')
        if len(parts) > 1:
            return all(self._evaluate_condition(part, item, names, values)
                       for part in parts)

        exists = re.fullmatch(r'attribute_(not_)?exists\(([^)]+)\)', expression)
        if exists:
            present = self._attribute_name(exists.group(2), names) in (item or {})
            return not present if exists.group(1) else present

        begins = re.fullmatch(r'begins_with\(([^,]+),\s*(:\w+)\)', expression)
        if begins:
            name = self._attribute_name(begins.group(1), names)
            actual = self._decoded_value((item or {}).get(name))
            prefix = self._decoded_value(values[begins.group(2)])
            return actual is not None and str(actual).startswith(str(prefix))

        comparison = re.fullmatch(
            r'([^\s]+)\s*(=|<>|<|<=|>|>=)\s*(:\w+)', expression)
        if comparison:
            name = self._attribute_name(comparison.group(1), names)
            actual = self._decoded_value((item or {}).get(name))
            expected = self._decoded_value(values[comparison.group(3)])
            return {
                '=': actual == expected,
                '<>': actual != expected,
                '<': actual is not None and actual < expected,
                '<=': actual is not None and actual <= expected,
                '>': actual is not None and actual > expected,
                '>=': actual is not None and actual >= expected,
            }[comparison.group(2)]
        raise AssertionError(f'未対応の条件式です: {expression!r}')

    @staticmethod
    def _split_boolean(expression, operator):
        parts = []
        depth = 0
        start = 0
        marker = f' {operator} '
        index = 0
        while index < len(expression):
            if expression[index] == '(':
                depth += 1
            elif expression[index] == ')':
                depth -= 1
            elif depth == 0 and expression.startswith(marker, index):
                parts.append(expression[start:index])
                index += len(marker)
                start = index
                continue
            index += 1
        parts.append(expression[start:])
        return parts

    @staticmethod
    def _strip_parentheses(expression):
        while expression.startswith('(') and expression.endswith(')'):
            depth = 0
            closes_at_end = True
            for index, character in enumerate(expression):
                if character == '(':
                    depth += 1
                elif character == ')':
                    depth -= 1
                    if depth == 0 and index != len(expression) - 1:
                        closes_at_end = False
                        break
            if not closes_at_end:
                break
            expression = expression[1:-1].strip()
        return expression

    def _apply_update(self, item, request):
        expression = request['UpdateExpression']
        names = request.get('ExpressionAttributeNames', {})
        values = request.get('ExpressionAttributeValues', {})
        sections = re.split(r'\s+(?=(?:SET|REMOVE|ADD|DELETE)\s)', expression)
        for section in sections:
            operation, body = section.split(None, 1)
            if operation == 'SET':
                for assignment in self._split_commas(body):
                    target, value_expression = assignment.split('=', 1)
                    name = self._attribute_name(target, names)
                    item[name] = self._evaluate_update_value(
                        value_expression.strip(), item, names, values)
            elif operation == 'REMOVE':
                for target in self._split_commas(body):
                    item.pop(self._attribute_name(target, names), None)
            elif operation == 'ADD':
                for target, value_name in re.findall(
                        r'([^\s,]+)\s+(:\w+)', body):
                    name = self._attribute_name(target, names)
                    current = self._decoded_value(item.get(name)) or 0
                    amount = self._decoded_value(values[value_name])
                    item[name] = self._serializer.serialize(current + amount)
            else:
                raise AssertionError(f'未対応の更新式です: {section!r}')

    def _evaluate_update_value(self, expression, item, names, values):
        addition = self._split_addition(expression)
        if addition is not None:
            left, right = addition
            left_value = self._decoded_value(self._evaluate_update_value(
                left, item, names, values))
            right_value = self._decoded_value(self._evaluate_update_value(
                right, item, names, values))
            return self._serializer.serialize(left_value + right_value)
        default = re.fullmatch(
            r'if_not_exists\(([^,]+),\s*(:\w+)\)', expression)
        if default:
            name = self._attribute_name(default.group(1), names)
            return self._copy(item.get(name, values[default.group(2)]))
        if expression.startswith(':'):
            return self._copy(values[expression])
        name = self._attribute_name(expression, names)
        if name not in item:
            raise AssertionError(f'存在しない属性を参照しました: {name}')
        return self._copy(item[name])

    @staticmethod
    def _split_addition(expression):
        depth = 0
        for index, character in enumerate(expression):
            if character == '(':
                depth += 1
            elif character == ')':
                depth -= 1
            elif character == '+' and depth == 0:
                return expression[:index].strip(), expression[index + 1:].strip()
        return None

    @staticmethod
    def _split_commas(expression):
        parts = []
        depth = 0
        start = 0
        for index, character in enumerate(expression):
            if character == '(':
                depth += 1
            elif character == ')':
                depth -= 1
            elif character == ',' and depth == 0:
                parts.append(expression[start:index].strip())
                start = index + 1
        parts.append(expression[start:].strip())
        return parts

    @staticmethod
    def _attribute_name(token, names):
        token = token.strip()
        return names.get(token, token)

    def _decoded_value(self, value):
        if value is None:
            return None
        return self._deserializer.deserialize(value)

    def _query_sort_name(self, request, items):
        if not items:
            return None
        names = request.get('ExpressionAttributeNames', {})
        expression = request.get('KeyConditionExpression', '')
        equality_names = {
            self._attribute_name(name, names)
            for name in re.findall(r'([^\s]+)\s*=\s*:\w+', expression)
        }
        candidates = set(items[0]) - equality_names
        for preferred in (
                'created_at_index', 'created_at', 'sk', 'generation'):
            if preferred in candidates:
                return preferred
        return None

    @staticmethod
    def _copy(value):
        if isinstance(value, dict):
            return {key: _MemoryDynamoClient._copy(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [_MemoryDynamoClient._copy(item) for item in value]
        return value

    @staticmethod
    def _scalar(value):
        if not isinstance(value, dict) or len(value) != 1:
            raise AssertionError(f'不正なDynamoDB属性です: {value!r}')
        return next(iter(value.values()))

    @classmethod
    def _key(cls, key):
        return tuple((name, cls._scalar(value))
                     for name, value in sorted(key.items()))

    @classmethod
    def _key_from_item(cls, item):
        names = [name for name in ('pk', 'sk', 'task_id') if name in item]
        if not names:
            raise AssertionError(f'主キーを判定できません: {item!r}')
        return cls._key({name: item[name] for name in names})

    @staticmethod
    def _conditional_error(operation):
        return ClientError(
            {
                'Error': {
                    'Code': 'ConditionalCheckFailedException',
                    'Message': 'condition failed',
                },
                'ResponseMetadata': {'RetryAttempts': 0},
            },
            operation,
        )


class AwsStateStoreContractTest(StateStoreContractMixin, unittest.TestCase):
    """同じMixinをGCP fixtureからも継承して共通契約を適用できる。"""

    def create_contract_store(self):
        from cloud_backend.aws.state_store import AwsStateStore

        return AwsStateStore(
            AWS_SETTINGS,
            client=_MemoryDynamoClient(),
            object_store=_MemoryObjectStore(),
            clock=lambda: datetime.datetime(
                2026, 8, 12, 9, 0, tzinfo=datetime.timezone.utc),
        )


class AwsStateStoreTest(unittest.TestCase):
    def create_store(self, **kwargs):
        from cloud_backend.aws.state_store import AwsStateStore

        options = {
            'client': _MemoryDynamoClient(),
            'object_store': _MemoryObjectStore(),
            'clock': lambda: datetime.datetime(
                2026, 8, 12, 9, 0, tzinfo=datetime.timezone.utc),
        }
        options.update(kwargs)
        return AwsStateStore(AWS_SETTINGS, **options)

    def test_DynamoDB_clientは最初の操作まで生成しない(self):
        from cloud_backend.aws.state_store import AwsStateStore

        client = _MemoryDynamoClient()
        factory = Mock(return_value=client)
        store = AwsStateStore(
            AWS_SETTINGS,
            client_factory=factory,
            object_store=_MemoryObjectStore(),
        )

        factory.assert_not_called()
        self.assertIsNone(store.get_global_bot_variables('missing'))
        factory.assert_called_once_with(
            'dynamodb', region_name='test-region-1')

    def test_Player条件付き変更も標準SDKclientを使い成功時に確認読取しない(self):
        from cloud_backend.aws.state_store import AwsStateStore
        standard = _MemoryDynamoClient()
        factory = Mock(return_value=standard)
        store = AwsStateStore(
            AWS_SETTINGS, client_factory=factory, object_store=_MemoryObjectStore())
        factory.assert_not_called()
        version = store.create_player_status('status', {'scene': 'one'})
        version = store.update_player_status('status', {'scene': 'two'}, version)
        store.delete_player_status('status', version)
        self.assertEqual(['put_item', 'put_item', 'delete_item'],
                         [operation for operation, _request in standard.calls])
        store.force_put_player_status('forced', {'scene': 'forced'})
        store.delete_player_status('forced')
        store.save_global_bot_variables('bot', 'scenario')
        store.append_group_member('group', 'shard', 'line:user,one')

        factory.assert_called_once_with('dynamodb', region_name='test-region-1')
        self.assertIn('update_item', [operation for operation, _request in standard.calls])
        self.assertIn('transact_write_items', [operation for operation, _request in standard.calls])

    def test_Playerの保存ACK喪失は自分のUUIDを1回強整合で確認して成功にする(self):
        for operation in ('create', 'update'):
            with self.subTest(operation=operation):
                client = _MemoryDynamoClient()
                store = self.create_store(client=client)
                version = None if operation == 'create' else store.create_player_status(
                    'status', {'scene': 'old'})
                client.calls.clear()
                put = client.put_item

                def committed_without_ack(**request):
                    put(**request)
                    raise ReadTimeoutError(endpoint_url='https://test.invalid')

                with patch.object(client, 'put_item', side_effect=committed_without_ack):
                    if operation == 'create':
                        saved = store.create_player_status('status', {'scene': 'one'})
                    else:
                        saved = store.update_player_status('status', {'scene': 'one'}, version)
                self.assertEqual(['put_item', 'get_item'],
                                 [name for name, _request in client.calls])
                self.assertTrue(client.calls[-1][1]['ConsistentRead'])
                state = store.load_player_status('status')
                self.assertEqual(saved, state.version)
                self.assertEqual({'scene': 'one'}, state.data)

    def test_PlayerのSDK再送後の条件拒否でも自己UUIDや削除完了を確認する(self):
        for operation in ('create', 'update', 'delete'):
            with self.subTest(operation=operation):
                client = _MemoryDynamoClient()
                store = self.create_store(client=client)
                version = None if operation == 'create' else store.create_player_status(
                    'status', {'scene': 'old'})
                client.calls.clear()
                method = 'delete_item' if operation == 'delete' else 'put_item'
                write = getattr(client, method)

                def committed_then_retried(**request):
                    write(**request)
                    try:
                        return write(**request)
                    except ClientError as error:
                        error.response['ResponseMetadata'] = {'RetryAttempts': 1}
                        raise

                with patch.object(client, method, side_effect=committed_then_retried):
                    if operation == 'create':
                        saved = store.create_player_status('status', {'scene': 'new'})
                    elif operation == 'update':
                        saved = store.update_player_status('status', {'scene': 'new'}, version)
                    else:
                        saved = store.delete_player_status('status', version)
                self.assertEqual([method, method, 'get_item'],
                                 [name for name, _request in client.calls])
                self.assertTrue(client.calls[-1][1]['ConsistentRead'])
                state = store.load_player_status('status')
                if operation == 'delete':
                    self.assertIsNone(saved)
                    self.assertIsNone(state)
                else:
                    self.assertEqual(saved, state.version)
                    self.assertEqual({'scene': 'new'}, state.data)

    def test_自己保存の後に他者が更新した場合は未保存競合へ戻さない(self):
        client = _MemoryDynamoClient()
        store = self.create_store(client=client)
        put = client.put_item

        def committed_then_overwritten(**request):
            put(**request)
            item = store._decode_item(request['Item'])
            other = {**item, 'version': 'b' * 32,
                     'payload': store._encode_payload({'scene': 'other'})}
            put(TableName=request['TableName'], Item=store._item(other),
                ConditionExpression='#version = :expected',
                ExpressionAttributeNames={'#version': 'version'},
                ExpressionAttributeValues={':expected': store._attribute(item['version'])})
            raise ReadTimeoutError(endpoint_url='https://test.invalid')

        with patch.object(client, 'put_item', side_effect=committed_then_overwritten), \
                self.assertRaises(StateStoreError) as raised:
            store.create_player_status('status', {'scene': 'mine'})
        self.assertIs(type(raised.exception), StateStoreError)
        self.assertEqual(['put_item', 'put_item', 'get_item'],
                         [name for name, _request in client.calls])
        state = store.load_player_status('status')
        self.assertEqual({'scene': 'other'}, state.data)
        self.assertEqual(StateVersion('dynamodb:v1:' + 'b' * 32), state.version)

    def test_読めた元の版や欠落は未保存の証拠として再計画へ戻さない(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                client = _MemoryDynamoClient()
                store = self.create_store(client=client)
                version = store.create_player_status('status', {'scene': 'old'}) if existing else None
                client.calls.clear()
                with patch.object(client, 'put_item', side_effect=ReadTimeoutError(
                        endpoint_url='https://test.invalid')) as put, \
                        self.assertRaises(StateStoreError) as raised:
                    if existing:
                        store.update_player_status('status', {'scene': 'new'}, version)
                    else:
                        store.create_player_status('status', {'scene': 'new'})
                self.assertIs(type(raised.exception), StateStoreError)
                put.assert_called_once()
                self.assertEqual(['get_item'], [name for name, _request in client.calls])

    def test_最初の条件拒否だけ競合として返し履歴欠落や再送済みを0とみなさない(self):
        for retries in (0, None, 1):
            with self.subTest(retries=retries):
                client = _MemoryDynamoClient()
                store = self.create_store(client=client)
                version = store.create_player_status('status', {'scene': 'old'})
                client.calls.clear()
                error = client._conditional_error('PutItem')
                if retries is None:
                    error.response.pop('ResponseMetadata')
                else:
                    error.response['ResponseMetadata']['RetryAttempts'] = retries
                expected = StateConflictError if retries == 0 else StateStoreError
                with patch.object(client, 'put_item', side_effect=error), \
                        self.assertRaises(expected) as raised:
                    store.update_player_status('status', {'scene': 'new'}, version)
                self.assertIs(type(raised.exception), expected)
                self.assertEqual([] if retries == 0 else ['get_item'],
                                 [name for name, _request in client.calls])

    def test_確認読取も失敗した場合は保存を再実行せず結果不明で止める(self):
        client = _MemoryDynamoClient()
        store = self.create_store(client=client)
        put = client.put_item

        def committed_without_ack(**request):
            put(**request)
            raise ReadTimeoutError(endpoint_url='https://test.invalid')

        with patch.object(client, 'put_item', side_effect=committed_without_ack) as written, \
                patch.object(client, 'get_item', side_effect=ReadTimeoutError(
                    endpoint_url='https://test.invalid')) as read, \
                self.assertRaises(StateStoreError) as raised:
            store.create_player_status('status', {'scene': 'mine'})
        self.assertIs(type(raised.exception), StateStoreError)
        written.assert_called_once()
        read.assert_called_once()
        self.assertTrue(read.call_args.kwargs['ConsistentRead'])

    def test_削除ACK喪失後に他者の状態があれば削除も成功推定もしない(self):
        client = _MemoryDynamoClient()
        store = self.create_store(client=client)
        version = store.create_player_status('status', {'scene': 'mine'})
        delete = client.delete_item

        def deleted_then_recreated(**request):
            delete(**request)
            client.put_item(TableName='test-state', Item=store._item({
                **store._player_key('status'), 'status_id': 'status', 'version': 'b' * 32,
                'payload': store._encode_payload({'scene': 'other'})}))
            raise ReadTimeoutError(endpoint_url='https://test.invalid')

        client.calls.clear()
        with patch.object(client, 'delete_item', side_effect=deleted_then_recreated) as deleted, \
                self.assertRaises(StateStoreError) as raised:
            store.delete_player_status('status', version)
        self.assertIs(type(raised.exception), StateStoreError)
        deleted.assert_called_once()
        self.assertEqual(['delete_item', 'put_item', 'get_item'],
                         [name for name, _request in client.calls])
        self.assertEqual({'scene': 'other'}, store.load_player_status('status').data)

    def test_巻戻し更新は元のUUIDを再利用せず自己確認した版で他者を保護する(self):
        client = _MemoryDynamoClient()
        store = self.create_store(client=client)
        original_version = store.create_player_status('status', {'scene': 'old'})
        saved_version = store.update_player_status('status', {'scene': 'new'}, original_version)
        put = client.put_item

        def committed_then_retried(**request):
            put(**request)
            try:
                return put(**request)
            except ClientError as error:
                error.response['ResponseMetadata']['RetryAttempts'] = 1
                raise

        with patch.object(client, 'put_item', side_effect=committed_then_retried), \
                patch.object(store, '_version_value', wraps=store._version_value) as generate:
            restored_version = store.update_player_status('status', {'scene': 'old'}, saved_version)
        generate.assert_called_once()
        self.assertNotIn(restored_version, (original_version, saved_version))
        later_version = store.update_player_status('status', {'scene': 'later'}, restored_version)
        with self.assertRaises(StateConflictError):
            store.update_player_status('status', {'scene': 'old'}, restored_version)
        self.assertEqual(later_version, store.load_player_status('status').version)

    def test_公開後に別writerが更新しても自分の原子的な世代を返す(self):
        client = _MemoryDynamoClient()
        store = self.create_store(client=client)
        update = client.update_item

        def publish_other(**request):
            response = update(**request)
            other = dict(request)
            other['ExpressionAttributeValues'] = dict(request['ExpressionAttributeValues'])
            other['ExpressionAttributeValues'][':payload'] = store._attribute(
                store._encode_payload({'scenario_uri': 'other'}))
            update(**other)
            return response

        with patch.object(client, 'update_item', side_effect=publish_other):
            self.assertEqual(1, store.save_global_bot_variables('bot', 'mine'))
        self.assertEqual(['update_item', 'update_item'], [operation for operation, request in client.calls])
        self.assertEqual({'scenario_uri': 'other', 'generation': 2}, store.get_global_bot_variables('bot'))

    def test_世代欠損pointerはNoneを返し初回公開だけ1から始める(self):
        client = _MemoryDynamoClient()
        store = self.create_store(client=client)
        client.put_item(TableName='test-state', Item=store._item({
            'pk': 'GLOBAL', 'sk': f'BOT#{store._hash("bot")}',
            'payload': store._encode_payload({'scenario_uri': 'old'}),
        }))
        self.assertEqual({'scenario_uri': 'old', 'generation': None}, store.get_global_bot_variables('bot'))
        self.assertEqual(1, store.save_global_bot_variables('bot', 'new'))

    def test_不正な公開世代を整数へ切り捨てて読まない(self):
        client = _MemoryDynamoClient()
        store = self.create_store(client=client)
        for value in (0, -1, True, Decimal('1.5'), '2'):
            with self.subTest(value=value):
                client.put_item(TableName='test-state', Item=store._item({
                    'pk': 'GLOBAL', 'sk': f'BOT#{store._hash("bot")}',
                    'payload': store._encode_payload({'scenario_uri': 'old'}), 'generation': value,
                }))
                with self.assertRaises(StateStoreError):
                    store.get_global_bot_variables('bot')

    def test_provider内ではStateStoreを共有する(self):
        state_store = Mock()
        object_store = Mock()
        original = getattr(aws_backend, '_state_store', None)
        aws_backend._state_store = None
        try:
            with (
                patch.dict(sys.modules, {
                    'settings': types.SimpleNamespace(
                        BACKEND_SETTINGS=AWS_SETTINGS),
                }),
                patch(
                    'cloud_backend.aws.state_store.AwsStateStore',
                    return_value=state_store,
                ) as constructor,
                patch.object(
                    aws_backend,
                    'create_object_store',
                    return_value=object_store,
                ),
            ):
                first = aws_backend.create_state_store()
                second = aws_backend.create_state_store()
        finally:
            aws_backend._state_store = original

        self.assertIs(first, state_store)
        self.assertIs(second, state_store)
        constructor.assert_called_once_with(
            AWS_SETTINGS, object_store=object_store)

    def test_StateStore設定不足とtable共有を拒否する(self):
        from cloud_backend.aws.state_store import AwsStateStore

        invalid_state_settings = (
            {
                'state_table': '',
                'group_task_table': 'task',
                'group_task_index': 'index',
                'cache_table': 'cache',
            },
            {
                'state_table': 'same',
                'group_task_table': 'same',
                'group_task_index': 'index',
                'cache_table': 'cache',
            },
            {
                'state_table': 'state',
                'group_task_table': 'task',
                'group_task_index': '',
                'cache_table': 'cache',
            },
            {
                'state_table': 'state',
                'group_task_table': 'task',
                'group_task_index': 'index',
                'cache_table': 'cache',
                'player_max_bytes': 0,
            },
        )
        for state_settings in invalid_state_settings:
            with self.subTest(state_settings=state_settings):
                with self.assertRaises(ValueError):
                    AwsStateStore(
                        {'state_store': state_settings},
                        client=_MemoryDynamoClient(),
                        object_store=_MemoryObjectStore(),
                    )

    def test_Player状態は既定350KiBを超えると書き込まない(self):
        client = _MemoryDynamoClient()
        store = self.create_store(client=client)

        with self.assertRaises(StateStoreError):
            store.create_player_status(
                'bot-a:line:user-1', {'value': 'x' * 358401})

        self.assertEqual([], client.calls)

    def test_Player状態は上限内ならUUID版tokenで保存する(self):
        from cloud_backend.aws.state_store import AwsStateStore

        settings = {
            **AWS_SETTINGS,
            'state_store': {
                **AWS_SETTINGS['state_store'],
                'player_max_bytes': 1024,
            },
        }
        store = AwsStateStore(
            settings,
            client=_MemoryDynamoClient(),
            object_store=_MemoryObjectStore(),
        )

        version = store.create_player_status(
            'bot-a:line:user-1', {'value': 'x' * 900})

        raw_version = version.value.removeprefix('dynamodb:v1:')
        self.assertEqual(32, len(raw_version))
        self.assertEqual(raw_version, uuid.UUID(raw_version).hex)

    def test_Player上限は文字数でなくUTF8_bytesで判定する(self):
        from cloud_backend.aws.state_store import AwsStateStore

        settings = {
            **AWS_SETTINGS,
            'state_store': {
                **AWS_SETTINGS['state_store'],
                'player_max_bytes': 1024,
            },
        }
        client = _MemoryDynamoClient()
        store = AwsStateStore(
            settings,
            client=client,
            object_store=_MemoryObjectStore(),
        )

        with self.assertRaises(StateStoreError):
            store.create_player_status(
                'bot-a:line:user-1', {'value': 'あ' * 400})

        self.assertEqual([], client.calls)

    def test_build_cacheはbytesだけS3へ外部化しTTLを秒へ変換する(self):
        now = [datetime.datetime(
            2026, 8, 12, 9, 0, tzinfo=datetime.timezone.utc)]
        object_store = _MemoryObjectStore()
        client = _MemoryDynamoClient()
        store = self.create_store(
            client=client,
            object_store=object_store,
            clock=lambda: now[0],
        )
        expire_at = now[0] + datetime.timedelta(days=30)

        store.set_build_cache('binary', b'\x00payload', expire_at=expire_at)

        self.assertEqual(1, len(object_store.stored_keys))
        first_object_key = object_store.stored_keys[0]
        self.assertTrue(first_object_key.endswith(
            hashlib.sha256(b'\x00payload').hexdigest()))
        binary_items = [
            store._decode_item(item)
            for item in client.tables['test-cache'].values()
            if store._decode_item(item)['pk'].startswith('BUILD_CACHE#')
        ]
        self.assertEqual(1, len(binary_items))
        self.assertEqual('object', binary_items[0]['storage'])
        self.assertEqual(first_object_key, binary_items[0]['object_key'])
        self.assertEqual(int(expire_at.timestamp()), binary_items[0]['expire_at'])
        self.assertEqual(
            b'\x00payload', store.get_build_cache('binary'))

        store.set_build_cache('binary', b'updated', expire_at=expire_at)
        second_object_key = object_store.stored_keys[-1]
        self.assertNotEqual(first_object_key, second_object_key)
        self.assertTrue(second_object_key.endswith(
            hashlib.sha256(b'updated').hexdigest()))
        self.assertEqual(b'updated', store.get_build_cache('binary'))

        now[0] = expire_at + datetime.timedelta(seconds=1)
        self.assertIsNone(store.get_build_cache('binary'))

        object_store.stored_keys.clear()
        future = now[0] + datetime.timedelta(days=30)
        store.set_build_cache('inline', {'value': 1}, expire_at=future)
        self.assertEqual([], object_store.stored_keys)
        self.assertEqual(
            {'value': 1}, store.get_build_cache('inline'))

    def test_Groupはtransactionとgenerationを使いScanしない(self):
        client = _MemoryDynamoClient()
        store = self.create_store(client=client)

        store.append_group_member(
            'group-a', 'shard-1', 'line:user-1')
        first_generation = store._load_group_catalog(
            'group-a')['generation']
        store.append_group_member(
            'group-a', 'shard-1', 'line:user-1')
        self.assertEqual(
            ['line:user-1'], store.get_group_members('group-a'))

        store.clear_group_members('group-a')
        store.append_group_member(
            'group-a', 'shard-2', 'line:user-2')
        second_generation = store._load_group_catalog(
            'group-a')['generation']

        self.assertNotEqual(first_generation, second_generation)
        self.assertEqual(
            ['line:user-2'], store.get_group_members('group-a'))
        operations = [operation for operation, _request in client.calls]
        self.assertIn('transact_write_items', operations)
        self.assertIn('query', operations)
        self.assertNotIn('scan', operations)

    def test_Group競合は現在世代を読み直して再試行する(self):
        client = _MemoryDynamoClient()
        original = client.transact_write_items
        attempts = []

        def fail_once(**request):
            attempts.append(request)
            if len(attempts) == 1:
                raise client._conditional_error('TransactWriteItems')
            return original(**request)

        client.transact_write_items = fail_once
        store = self.create_store(client=client)

        store.append_group_member(
            'group-a', 'shard-1', 'line:user-1')

        self.assertEqual(2, len(attempts))
        self.assertEqual(
            ['line:user-1'], store.get_group_members('group-a'))

    def test_query_allはLastEvaluatedKeyを引き継ぐ(self):
        client = Mock()
        store = self.create_store(client=client)
        attribute = TypeSerializer().serialize
        first = {
            'pk': attribute('GROUP#hash#GEN#generation'),
            'sk': attribute('MEMBER#1'),
            'member': attribute('line:user-1'),
        }
        second = {
            'pk': attribute('GROUP#hash#GEN#generation'),
            'sk': attribute('MEMBER#2'),
            'member': attribute('line:user-2'),
        }
        last_key = {
            'pk': first['pk'],
            'sk': first['sk'],
        }
        client.query.side_effect = [
            {'Items': [first], 'LastEvaluatedKey': last_key},
            {'Items': [second]},
        ]

        items = store._query_all(
            'test-state',
            KeyConditionExpression='#pk = :pk',
            ExpressionAttributeNames={'#pk': 'pk'},
            ExpressionAttributeValues={':pk': attribute('partition')},
        )

        self.assertEqual(
            ['line:user-1', 'line:user-2'],
            [item['member'] for item in items],
        )
        self.assertNotIn(
            'ExclusiveStartKey', client.query.call_args_list[0].kwargs)
        self.assertEqual(
            last_key,
            client.query.call_args_list[1].kwargs['ExclusiveStartKey'],
        )

    def test_UnprocessedItemsはbackoffして再送する(self):
        client = Mock()
        store = self.create_store(client=client)
        pending = [{
            'DeleteRequest': {
                'Key': store._item({'pk': 'partition', 'sk': 'item'}),
            },
        }]
        client.batch_write_item.side_effect = [
            {'UnprocessedItems': {'test-cache': pending}},
            {'UnprocessedItems': {}},
        ]

        with patch('cloud_backend.aws.state_store.time.sleep') as sleep:
            store._delete_items(
                'test-cache', [{'pk': 'partition', 'sk': 'item'}])

        self.assertEqual(2, client.batch_write_item.call_count)
        sleep.assert_called_once_with(0.01)

    def test_最近の配信TaskはGSIを降順limit付きでqueryする(self):
        client = _MemoryDynamoClient()
        store = self.create_store(client=client)
        old_time = datetime.datetime(
            2026, 8, 12, 1, 0, tzinfo=datetime.timezone.utc)
        new_time = datetime.datetime(
            2026, 8, 12, 2, 0, tzinfo=datetime.timezone.utc)
        store.create_group_message_task('old', {
            'bot_name': 'bot-a', 'created_at': old_time,
        })
        store.create_group_message_task('new', {
            'bot_name': 'bot-a', 'created_at': new_time,
        })
        client.calls.clear()

        recent = store.get_recent_group_message_tasks('bot-a', 1)

        self.assertEqual(['new'], [task['id'] for task in recent])
        self.assertEqual(1, len(client.calls))
        operation, request = client.calls[0]
        self.assertEqual('query', operation)
        self.assertEqual('test-bot-created-at-index', request['IndexName'])
        self.assertFalse(request['ScanIndexForward'])
        self.assertEqual(1, request['Limit'])

    def test_配信Task競合はcallbackを現在値へ再適用する(self):
        client = _MemoryDynamoClient()
        store = self.create_store(client=client)
        store.create_group_message_task('task-1', {
            'bot_name': 'bot-a',
            'status': 'pending',
            'count': 0,
        })
        original = client.put_item
        attempts = []

        def fail_once(**request):
            if request.get('ConditionExpression'):
                attempts.append(request)
                if len(attempts) == 1:
                    raise client._conditional_error('PutItem')
            return original(**request)

        client.put_item = fail_once
        callback_values = []

        def update(current):
            callback_values.append(current['count'])
            return {'count': current['count'] + 1}

        self.assertTrue(store.update_group_message_task(
            'task-1', update))

        self.assertEqual([0, 0], callback_values)
        self.assertEqual(
            1, store.get_group_message_task('task-1')['count'])

    def test_実botocore_Stubberで代表request形状を固定する(self):
        from cloud_backend.aws.state_store import AwsStateStore

        client = boto3.client(
            'dynamodb',
            region_name='test-region-1',
            aws_access_key_id='test-access-key',
            aws_secret_access_key='test-secret-key',
            endpoint_url='https://dynamodb.test.invalid',
        )
        store = AwsStateStore(
            AWS_SETTINGS,
            client=client,
            object_store=_MemoryObjectStore(),
        )
        attribute = TypeSerializer().serialize
        bot_name = 'bot-a'
        group_id = 'group-a'
        shard_id = 'shard-1'
        member = 'line:user-1'
        generation = 'a' * 32
        group_hash = store._hash(group_id)

        with Stubber(client) as stubber:
            stubber.add_response('update_item', {'Attributes': {
                'pk': attribute('GLOBAL'), 'sk': attribute(f'BOT#{store._hash(bot_name)}'),
                'bot_name': attribute(bot_name), 'generation': attribute(1),
                'payload': attribute('{"scenario_uri":"opaque://scenario/1"}'),
            }}, {
                'TableName': 'test-state',
                'Key': {
                    'pk': attribute('GLOBAL'),
                    'sk': attribute(f'BOT#{store._hash(bot_name)}'),
                },
                'UpdateExpression': 'SET #payload = :payload, #bot = :bot ADD #generation :one',
                'ConditionExpression': 'attribute_not_exists(#generation) OR #generation >= :one',
                'ExpressionAttributeNames': {
                    '#payload': 'payload', '#bot': 'bot_name', '#generation': 'generation'},
                'ExpressionAttributeValues': {
                    ':payload': attribute('{"scenario_uri":"opaque://scenario/1"}'),
                    ':bot': attribute(bot_name), ':one': attribute(1)},
                'ReturnValues': 'ALL_NEW',
            })
            stubber.add_response('get_item', {
                'Item': {
                    'pk': attribute('GLOBAL'),
                    'sk': attribute(f'BOT#{store._hash(bot_name)}'),
                    'payload': attribute(
                        '{"scenario_uri":"opaque://scenario/1"}'),
                    'generation': attribute(1),
                },
            }, {
                'TableName': 'test-state',
                'Key': {
                    'pk': attribute('GLOBAL'),
                    'sk': attribute(f'BOT#{store._hash(bot_name)}'),
                },
                'ConsistentRead': True,
            })
            stubber.add_response('get_item', {}, {
                'TableName': 'test-state',
                'Key': {
                    'pk': attribute('GROUP_CATALOG'),
                    'sk': attribute(f'GROUP#{group_hash}'),
                },
                'ConsistentRead': True,
            })
            stubber.add_response('transact_write_items', {}, {
                'TransactItems': [
                    {
                        'Put': {
                            'TableName': 'test-state',
                            'Item': {
                                'pk': attribute('GROUP_CATALOG'),
                                'sk': attribute(f'GROUP#{group_hash}'),
                                'group_id': attribute(group_id),
                                'generation': attribute(generation),
                            },
                            'ConditionExpression': 'attribute_not_exists(#pk)',
                            'ExpressionAttributeNames': {'#pk': 'pk'},
                        },
                    },
                    {
                        'Put': {
                            'TableName': 'test-state',
                            'Item': {
                                'pk': attribute(
                                    f'GROUP#{group_hash}#GEN#{generation}'),
                                'sk': attribute(
                                    f'MEMBER#{store._hash(shard_id)}#'
                                    f'{store._hash(member)}'),
                                'group_id': attribute(group_id),
                                'shard_id': attribute(shard_id),
                                'member': attribute(member),
                            },
                            'ConditionExpression': 'attribute_not_exists(#pk)',
                            'ExpressionAttributeNames': {'#pk': 'pk'},
                        },
                    },
                ],
            })
            task_time = datetime.datetime(
                2026, 8, 12, 3, 0, tzinfo=datetime.timezone.utc)
            task = {
                'bot_name': bot_name,
                'created_at': task_time,
                'updated_at': task_time,
                'status': 'done',
            }
            stubber.add_response('query', {
                'Items': [{
                    'task_id': attribute('task-1'),
                    'payload': attribute(store._encode_payload(task)),
                    'revision': attribute(1),
                    'bot_name_index': attribute(bot_name),
                    'created_at_index': attribute(
                        f'{task_time.isoformat(timespec="microseconds")}#task-1'),
                }],
            }, {
                'TableName': 'test-group-task',
                'IndexName': 'test-bot-created-at-index',
                'KeyConditionExpression': '#bot = :bot',
                'ExpressionAttributeNames': {'#bot': 'bot_name_index'},
                'ExpressionAttributeValues': {
                    ':bot': attribute(bot_name),
                },
                'ScanIndexForward': False,
                'Limit': 1,
            })

            self.assertEqual(1, store.save_global_bot_variables(
                bot_name, 'opaque://scenario/1'))
            self.assertEqual(
                {'scenario_uri': 'opaque://scenario/1', 'generation': 1},
                store.get_global_bot_variables(bot_name),
            )
            with patch(
                    'cloud_backend.aws.state_store.uuid.uuid4',
                    return_value=types.SimpleNamespace(hex=generation)):
                store.append_group_member(
                    group_id, shard_id, member)
            self.assertEqual(
                ['task-1'],
                [task['id'] for task in
                 store.get_recent_group_message_tasks(bot_name, 1)],
            )

    def test_version_tokenはprovider固有型を受け付けない(self):
        store = self.create_store()
        version = store.create_player_status(
            'bot-a:line:user-1', {'value': '{}'})
        self.assertTrue(version.value.startswith('dynamodb:v1:'))
        self.assertTrue(version.value.removeprefix('dynamodb:v1:'))

        with self.assertRaises(TypeError):
            store.update_player_status(
                'bot-a:line:user-1', {'value': '{}'}, 'version-1')
        with self.assertRaises(StateStoreError):
            store.update_player_status(
                'bot-a:line:user-1',
                {'value': '{}'},
                StateVersion('dynamodb:v1:broken'),
            )

    def test_JSON_codecの予約markerと同じ通常dictを保持する(self):
        store = self.create_store()
        user_value = {
            '__xstorybot_type__': 'datetime',
            'value': 'user-value',
        }

        store.create_player_status(
            'bot-a:line:user-1', {'nested': user_value})

        loaded = store.load_player_status('bot-a:line:user-1')
        self.assertEqual(user_value, loaded.data['nested'])

    def test_AWS_SDK例外だけを共通例外へ変換する(self):
        from cloud_backend.aws.state_store import AwsStateStore

        client = Mock()
        store = AwsStateStore(
            AWS_SETTINGS, client=client, object_store=_MemoryObjectStore())
        client.get_item.side_effect = NoCredentialsError()
        with self.assertRaises(StateStoreError):
            store.get_global_bot_variables('bot-a')

        client.get_item.side_effect = ClientError(
            {
                'Error': {
                    'Code': 'ConditionalCheckFailedException',
                    'Message': 'condition failed',
                },
            },
            'GetItem',
        )
        with self.assertRaises(StateStoreError):
            store.get_global_bot_variables('bot-a')

        client.get_item.side_effect = None
        client.put_item.side_effect = ClientError(
            {
                'Error': {
                    'Code': 'ConditionalCheckFailedException',
                    'Message': 'condition failed',
                },
                'ResponseMetadata': {'RetryAttempts': 0},
            },
            'PutItem',
        )
        with self.assertRaises(StateConflictError):
            store.create_player_status(
                'bot-a:line:user-1', {'value': '{}'})

        application_error = RuntimeError('application error')
        client.put_item.side_effect = None
        client.get_item.side_effect = application_error
        with self.assertRaises(RuntimeError) as raised:
            store.get_global_bot_variables('bot-a')
        self.assertIs(application_error, raised.exception)

    def test_TransactionCanceledは理由が競合の場合だけ競合とする(self):
        from cloud_backend.aws.state_store import AwsStateStore

        conditional = ClientError(
            {
                'Error': {
                    'Code': 'TransactionCanceledException',
                    'Message': 'transaction canceled',
                },
                'CancellationReasons': [
                    {'Code': 'ConditionalCheckFailed'},
                    {'Code': 'None'},
                ],
            },
            'TransactWriteItems',
        )
        capacity = ClientError(
            {
                'Error': {
                    'Code': 'TransactionCanceledException',
                    'Message': 'transaction canceled',
                },
                'CancellationReasons': [
                    {'Code': 'ProvisionedThroughputExceeded'},
                ],
            },
            'TransactWriteItems',
        )
        no_conflict = ClientError(
            {
                'Error': {
                    'Code': 'TransactionCanceledException',
                    'Message': 'transaction canceled',
                },
                'CancellationReasons': [
                    {'Code': 'None'},
                    {'Code': None},
                ],
            },
            'TransactWriteItems',
        )

        self.assertTrue(AwsStateStore._is_conditional_error(conditional))
        self.assertFalse(AwsStateStore._is_conditional_error(capacity))
        self.assertFalse(AwsStateStore._is_conditional_error(no_conflict))


if __name__ == '__main__':
    unittest.main()
