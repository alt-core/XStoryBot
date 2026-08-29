"""Amazon DynamoDBを利用するStateStore実装。"""

import base64
import datetime
import hashlib
import json
import logging
import time
import uuid

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from cloud_backend.contracts import (
    ObjectStoreError,
    StateConflictError,
    StateStore,
    StateStoreError,
    StateVersion,
    VersionedState,
)


TASK_EXECUTION_CLAIMED = 'claimed'
TASK_EXECUTION_BUSY = 'busy'
TASK_EXECUTION_COMPLETED = 'completed'


class AwsStateStore(StateStore):
    """三つのDynamoDBテーブルへ状態、配信Task、cacheを保存する。"""

    _GROUP_RETRY_LIMIT = 5
    _TASK_RETRY_LIMIT = 5
    _CACHE_PARTITIONS = 16
    _TASK_EXECUTION_TTL_SECONDS = 15 * 24 * 60 * 60
    _CONDITIONAL_ERROR_CODES = {'ConditionalCheckFailedException'}

    def __init__(
            self, aws_settings=None, client=None, client_factory=None,
            object_store=None, clock=None):
        if aws_settings is None:
            import settings
            aws_settings = settings.BACKEND_SETTINGS

        store_settings = aws_settings.get('state_store', {})
        self._state_table = store_settings.get('state_table', '')
        self._group_task_table = store_settings.get('group_task_table', '')
        self._group_task_index = store_settings.get('group_task_index', '')
        self._cache_table = store_settings.get('cache_table', '')
        self._player_max_bytes = int(
            store_settings.get('player_max_bytes', 350 * 1024))
        self._region = aws_settings.get('region') or None
        self._validate_settings()

        if object_store is None:
            from cloud_backend.aws import create_object_store
            object_store = create_object_store()
        self._object_store = object_store
        self._client_instance = client
        self._client_factory = client_factory or boto3.client
        self._clock = clock or (
            lambda: datetime.datetime.now(datetime.timezone.utc))
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    def _validate_settings(self):
        required = {
            'state_table': self._state_table,
            'group_task_table': self._group_task_table,
            'group_task_index': self._group_task_index,
            'cache_table': self._cache_table,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                'AWS StateStoreの設定が不足しています: '
                + ', '.join(missing))
        if len({
                self._state_table,
                self._group_task_table,
                self._cache_table,
        }) != 3:
            raise ValueError('AWS StateStoreの三つのtableは分けてください')
        if self._player_max_bytes <= 0:
            raise ValueError('AWS Player状態の上限は正の値にしてください')

    @staticmethod
    def _is_conditional_error(error):
        if not isinstance(error, ClientError):
            return False
        code = str(error.response.get('Error', {}).get('Code', ''))
        if code in AwsStateStore._CONDITIONAL_ERROR_CODES:
            return True
        if code != 'TransactionCanceledException':
            return False
        reasons = error.response.get('CancellationReasons', [])
        if not reasons:
            return False
        codes = [reason.get('Code') for reason in reasons]
        return (
            any(code in (
                'ConditionalCheckFailed',
                'TransactionConflict',
            ) for code in codes)
            and all(code in (
                None,
                'None',
                'ConditionalCheckFailed',
                'TransactionConflict',
            ) for code in codes)
        )

    @classmethod
    def _raise_store_error(cls, error, conflict=False):
        if conflict and cls._is_conditional_error(error):
            raise StateConflictError(str(error)) from error
        if isinstance(error, (ClientError, BotoCoreError)):
            raise StateStoreError(str(error)) from error
        if type(error).__module__.startswith(('boto3.', 'botocore.')):
            raise StateStoreError(str(error)) from error
        raise error

    def _call(self, operation, conflict=False):
        try:
            return operation()
        except Exception as error:
            self._raise_store_error(error, conflict=conflict)

    def _client(self):
        if self._client_instance is None:
            try:
                options = {}
                if self._region is not None:
                    options['region_name'] = self._region
                self._client_instance = self._client_factory(
                    'dynamodb', **options)
            except Exception as error:
                self._raise_store_error(error)
        return self._client_instance

    @staticmethod
    def _hash(value):
        return hashlib.sha256(str(value).encode('utf-8')).hexdigest()

    @staticmethod
    def _normalize_datetime(value):
        if not isinstance(value, datetime.datetime):
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return datetime.datetime.fromtimestamp(
            value.timestamp(), tz=datetime.timezone.utc)

    def _now(self):
        return self._normalize_datetime(self._clock())

    @classmethod
    def _to_json_value(cls, value):
        if isinstance(value, datetime.datetime):
            return {
                '__xstorybot_type__': 'datetime',
                'value': cls._normalize_datetime(value).isoformat(),
            }
        if isinstance(value, bytes):
            return {
                '__xstorybot_type__': 'bytes',
                'value': base64.b64encode(value).decode('ascii'),
            }
        if isinstance(value, bytearray):
            return cls._to_json_value(bytes(value))
        if isinstance(value, tuple):
            return {
                '__xstorybot_type__': 'tuple',
                'value': [cls._to_json_value(item) for item in value],
            }
        if isinstance(value, list):
            return [cls._to_json_value(item) for item in value]
        if isinstance(value, dict):
            if '__xstorybot_type__' in value:
                return {
                    '__xstorybot_type__': 'dict',
                    'value': [
                        [str(key), cls._to_json_value(item)]
                        for key, item in value.items()
                    ],
                }
            return {
                str(key): cls._to_json_value(item)
                for key, item in value.items()
            }
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        raise TypeError(
            f'DynamoDBへ保存できない値です: {type(value).__name__}')

    @classmethod
    def _from_json_value(cls, value):
        if isinstance(value, list):
            return [cls._from_json_value(item) for item in value]
        if isinstance(value, dict):
            value_type = value.get('__xstorybot_type__')
            if value_type == 'datetime' and set(value) == {
                    '__xstorybot_type__', 'value'}:
                return cls._normalize_datetime(
                    datetime.datetime.fromisoformat(value['value']))
            if value_type == 'bytes' and set(value) == {
                    '__xstorybot_type__', 'value'}:
                return base64.b64decode(value['value'])
            if value_type == 'tuple' and set(value) == {
                    '__xstorybot_type__', 'value'}:
                return tuple(
                    cls._from_json_value(item) for item in value['value'])
            if value_type == 'dict' and set(value) == {
                    '__xstorybot_type__', 'value'}:
                return {
                    key: cls._from_json_value(item)
                    for key, item in value['value']
                }
            return {
                key: cls._from_json_value(item)
                for key, item in value.items()
            }
        return value

    @classmethod
    def _encode_payload(cls, data):
        return json.dumps(
            cls._to_json_value(data),
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )

    @classmethod
    def _decode_payload(cls, payload):
        return cls._from_json_value(json.loads(payload))

    def _attribute(self, value):
        return self._serializer.serialize(value)

    def _item(self, values):
        return {
            key: self._attribute(value)
            for key, value in values.items()
            if value is not None
        }

    def _decode_item(self, item):
        return {
            key: self._deserializer.deserialize(value)
            for key, value in item.items()
        }

    def _get_item(self, table, key, consistent=True):
        request = {
            'TableName': table,
            'Key': self._item(key),
        }
        if consistent:
            request['ConsistentRead'] = True
        response = self._client().get_item(**request)
        item = response.get('Item')
        return self._decode_item(item) if item else None

    def _query_all(self, table, **request):
        items = []
        last_key = None
        while True:
            current = dict(request)
            current['TableName'] = table
            if last_key is not None:
                current['ExclusiveStartKey'] = last_key
            response = self._client().query(**current)
            items.extend(
                self._decode_item(item)
                for item in response.get('Items', []))
            last_key = response.get('LastEvaluatedKey')
            if not last_key:
                return items

    @staticmethod
    def _player_key(status_id):
        return {
            'pk': f'PLAYER#{AwsStateStore._hash(status_id)}',
            'sk': 'STATE',
        }

    @staticmethod
    def _version_value():
        return uuid.uuid4().hex

    @staticmethod
    def _version_token(value):
        return StateVersion(f'dynamodb:v1:{value}')

    @staticmethod
    def _decode_version(version):
        if not isinstance(version, StateVersion):
            raise TypeError('versionにはStateVersionを指定してください')
        prefix = 'dynamodb:v1:'
        if not version.value.startswith(prefix) or not version.value[len(prefix):]:
            raise StateStoreError('未対応のversion tokenです')
        raw_version = version.value[len(prefix):]
        try:
            parsed = uuid.UUID(raw_version)
        except (ValueError, AttributeError) as error:
            raise StateStoreError('未対応のversion tokenです') from error
        if parsed.hex != raw_version:
            raise StateStoreError('未対応のversion tokenです')
        return raw_version

    def _player_payload(self, data):
        payload = self._encode_payload(dict(data))
        if len(payload.encode('utf-8')) > self._player_max_bytes:
            raise StateStoreError(
                f'Player状態が上限{self._player_max_bytes} bytesを超えています')
        return payload

    def get_global_bot_variables(self, bot_name):
        def operation():
            item = self._get_item(self._state_table, {
                'pk': 'GLOBAL',
                'sk': f'BOT#{self._hash(bot_name)}',
            })
            return self._decode_payload(item['payload']) if item else None
        return self._call(operation)

    def save_global_bot_variables(self, bot_name, scenario_uri):
        def operation():
            self._client().put_item(
                TableName=self._state_table,
                Item=self._item({
                    'pk': 'GLOBAL',
                    'sk': f'BOT#{self._hash(bot_name)}',
                    'bot_name': bot_name,
                    'payload': self._encode_payload({
                        'scenario_uri': scenario_uri,
                    }),
                }),
            )
        return self._call(operation)

    def load_player_status(self, status_id):
        def operation():
            item = self._get_item(
                self._state_table, self._player_key(status_id))
            if item is None:
                return None
            return VersionedState(
                data=self._decode_payload(item['payload']),
                version=self._version_token(item['version']),
            )
        return self._call(operation)

    def create_player_status(self, status_id, data):
        payload = self._player_payload(data)
        version = self._version_value()

        def operation():
            self._client().put_item(
                TableName=self._state_table,
                Item=self._item({
                    **self._player_key(status_id),
                    'status_id': status_id,
                    'payload': payload,
                    'version': version,
                }),
                ConditionExpression='attribute_not_exists(#pk)',
                ExpressionAttributeNames={'#pk': 'pk'},
            )
            return self._version_token(version)
        return self._call(operation, conflict=True)

    def update_player_status(self, status_id, data, version):
        expected_version = self._decode_version(version)
        payload = self._player_payload(data)
        next_version = self._version_value()

        def operation():
            self._client().put_item(
                TableName=self._state_table,
                Item=self._item({
                    **self._player_key(status_id),
                    'status_id': status_id,
                    'payload': payload,
                    'version': next_version,
                }),
                ConditionExpression='#version = :expected',
                ExpressionAttributeNames={'#version': 'version'},
                ExpressionAttributeValues={
                    ':expected': self._attribute(expected_version),
                },
            )
            return self._version_token(next_version)
        return self._call(operation, conflict=True)

    def force_put_player_status(self, status_id, data):
        payload = self._player_payload(data)
        version = self._version_value()

        def operation():
            self._client().put_item(
                TableName=self._state_table,
                Item=self._item({
                    **self._player_key(status_id),
                    'status_id': status_id,
                    'payload': payload,
                    'version': version,
                }),
            )
            return self._version_token(version)
        return self._call(operation)

    def delete_player_status(self, status_id):
        def operation():
            self._client().delete_item(
                TableName=self._state_table,
                Key=self._item(self._player_key(status_id)),
            )
        return self._call(operation)

    @staticmethod
    def _group_catalog_key(group_id):
        return {
            'pk': 'GROUP_CATALOG',
            'sk': f'GROUP#{AwsStateStore._hash(group_id)}',
        }

    @staticmethod
    def _group_member_pk(group_id, generation):
        return f'GROUP#{AwsStateStore._hash(group_id)}#GEN#{generation}'

    @staticmethod
    def _group_member_sk(shard_id, member):
        return (
            f'MEMBER#{AwsStateStore._hash(shard_id)}#'
            f'{AwsStateStore._hash(member)}')

    def _load_group_catalog(self, group_id):
        return self._get_item(
            self._state_table, self._group_catalog_key(group_id))

    def _load_group_member(self, group_id, generation, shard_id, member):
        return self._get_item(self._state_table, {
            'pk': self._group_member_pk(group_id, generation),
            'sk': self._group_member_sk(shard_id, member),
        })

    def get_group_members(self, group_id):
        def operation():
            catalog = self._load_group_catalog(group_id)
            if catalog is None:
                return []
            items = self._query_all(
                self._state_table,
                KeyConditionExpression='#pk = :pk',
                ExpressionAttributeNames={'#pk': 'pk'},
                ExpressionAttributeValues={
                    ':pk': self._attribute(self._group_member_pk(
                        group_id, catalog['generation'])),
                },
                ConsistentRead=True,
            )
            return [item['member'] for item in items]
        return self._call(operation)

    def append_group_member(self, group_id, shard_id, member):
        for _attempt in range(self._GROUP_RETRY_LIMIT):
            try:
                catalog = self._load_group_catalog(group_id)
                if catalog is None:
                    generation = uuid.uuid4().hex
                    transaction = [{
                        'Put': {
                            'TableName': self._state_table,
                            'Item': self._item({
                                **self._group_catalog_key(group_id),
                                'group_id': group_id,
                                'generation': generation,
                            }),
                            'ConditionExpression': 'attribute_not_exists(#pk)',
                            'ExpressionAttributeNames': {'#pk': 'pk'},
                        },
                    }]
                else:
                    generation = catalog['generation']
                    transaction = [{
                        'ConditionCheck': {
                            'TableName': self._state_table,
                            'Key': self._item(
                                self._group_catalog_key(group_id)),
                            'ConditionExpression': '#generation = :generation',
                            'ExpressionAttributeNames': {
                                '#generation': 'generation',
                            },
                            'ExpressionAttributeValues': {
                                ':generation': self._attribute(generation),
                            },
                        },
                    }]
                member_key = {
                    'pk': self._group_member_pk(group_id, generation),
                    'sk': self._group_member_sk(shard_id, member),
                }
                transaction.append({
                    'Put': {
                        'TableName': self._state_table,
                        'Item': self._item({
                            **member_key,
                            'group_id': group_id,
                            'shard_id': shard_id,
                            'member': member,
                        }),
                        'ConditionExpression': 'attribute_not_exists(#pk)',
                        'ExpressionAttributeNames': {'#pk': 'pk'},
                    },
                })
                self._client().transact_write_items(
                    TransactItems=transaction)
                return None
            except Exception as error:
                if not self._is_conditional_error(error):
                    self._raise_store_error(error)
                current = self._call(
                    lambda: self._load_group_catalog(group_id))
                existing_member = None
                if current is not None:
                    existing_member = self._call(
                        lambda: self._load_group_member(
                            group_id,
                            current['generation'],
                            shard_id,
                            member,
                        ))
                if existing_member is not None:
                    return None
        raise StateConflictError('Group構成の同時更新を完了できませんでした')

    def remove_group_member(self, group_id, shard_id, member):
        for _attempt in range(self._GROUP_RETRY_LIMIT):
            catalog = self._call(lambda: self._load_group_catalog(group_id))
            if catalog is None:
                return None
            generation = catalog['generation']
            try:
                self._client().transact_write_items(TransactItems=[
                    {
                        'ConditionCheck': {
                            'TableName': self._state_table,
                            'Key': self._item(
                                self._group_catalog_key(group_id)),
                            'ConditionExpression': '#generation = :generation',
                            'ExpressionAttributeNames': {
                                '#generation': 'generation',
                            },
                            'ExpressionAttributeValues': {
                                ':generation': self._attribute(generation),
                            },
                        },
                    },
                    {
                        'Delete': {
                            'TableName': self._state_table,
                            'Key': self._item({
                                'pk': self._group_member_pk(
                                    group_id, generation),
                                'sk': self._group_member_sk(
                                    shard_id, member),
                            }),
                        },
                    },
                ])
                return None
            except Exception as error:
                if not self._is_conditional_error(error):
                    self._raise_store_error(error)
        raise StateConflictError('Group構成の同時更新を完了できませんでした')

    def _delete_items(self, table, keys):
        for start in range(0, len(keys), 25):
            pending = [{
                'DeleteRequest': {'Key': self._item(key)},
            } for key in keys[start:start + 25]]
            for _attempt in range(8):
                response = self._client().batch_write_item(
                    RequestItems={table: pending})
                pending = response.get(
                    'UnprocessedItems', {}).get(table, [])
                if not pending:
                    break
                # SDKの通常retry後にも未処理が返った場合だけ短く待つ。
                time.sleep(min(0.01 * (2 ** _attempt), 0.5))
            if pending:
                raise StateStoreError('DynamoDBの一括削除を完了できませんでした')

    def clear_group_members(self, group_id):
        def operation():
            catalog = self._load_group_catalog(group_id)
            if catalog is None:
                return None
            generation = catalog['generation']
            try:
                self._client().delete_item(
                    TableName=self._state_table,
                    Key=self._item(self._group_catalog_key(group_id)),
                    ConditionExpression='#generation = :generation',
                    ExpressionAttributeNames={'#generation': 'generation'},
                    ExpressionAttributeValues={
                        ':generation': self._attribute(generation),
                    },
                )
            except Exception as error:
                if not self._is_conditional_error(error):
                    raise
                current = self._load_group_catalog(group_id)
                if (
                        current is None
                        or current.get('generation') != generation):
                    return None
                raise

            # catalog削除がclearの確定点である。ここから失敗を返すと、
            # 呼出し側のretryが直後に追加された新世代まで消すため、旧世代の
            # 不可視item削除だけはbest effortにする。
            try:
                members = self._call(lambda: self._query_all(
                    self._state_table,
                    KeyConditionExpression='#pk = :pk',
                    ExpressionAttributeNames={'#pk': 'pk'},
                    ExpressionAttributeValues={
                        ':pk': self._attribute(
                            self._group_member_pk(group_id, generation)),
                    },
                    ConsistentRead=True,
                ))
                self._call(lambda: self._delete_items(
                    self._state_table,
                    [
                        {'pk': member['pk'], 'sk': member['sk']}
                        for member in members
                    ],
                ))
            except StateStoreError as error:
                logging.error(
                    'Group旧世代itemの後処理に失敗しました: %s',
                    type(error).__name__,
                )
            return None
        return self._call(operation, conflict=True)

    def get_all_groups(self):
        def operation():
            items = self._query_all(
                self._state_table,
                KeyConditionExpression='#pk = :pk',
                ExpressionAttributeNames={'#pk': 'pk'},
                ExpressionAttributeValues={
                    ':pk': self._attribute('GROUP_CATALOG'),
                },
                ConsistentRead=True,
            )
            return [{'id': item['group_id']} for item in items]
        return self._call(operation)

    def _stat_key(self, kind, key):
        return {
            'pk': f'STAT#{kind}',
            'sk': f'KEY#{self._hash(key)}',
        }

    def _get_stat(self, kind, key):
        item = self._get_item(
            self._cache_table, self._stat_key(kind, key))
        return self._decode_payload(item['payload']) if item else None

    def _put_stat(self, kind, key, data):
        self._client().put_item(
            TableName=self._cache_table,
            Item=self._item({
                **self._stat_key(kind, key),
                'source_key': key,
                'payload': self._encode_payload(dict(data)),
            }),
        )

    def get_image_file_stat(self, key):
        return self._call(lambda: self._get_stat('IMAGE', key))

    def put_image_file_stat(self, key, data):
        return self._call(lambda: self._put_stat('IMAGE', key, data))

    def get_media_file_stat(self, key):
        return self._call(lambda: self._get_stat('MEDIA', key))

    def put_media_file_stat(self, key, data):
        return self._call(lambda: self._put_stat('MEDIA', key, data))

    def get_image_text_stat(self, key):
        return self._call(lambda: self._get_stat('IMAGE_TEXT', key))

    def put_image_text_stat(self, key, data):
        return self._call(lambda: self._put_stat('IMAGE_TEXT', key, data))

    def _next_label_key(self, status_id):
        return {
            'pk': f'NEXT_LABEL#{self._hash(status_id)}',
            'sk': 'STATE',
        }

    def get_next_label(self, status_id):
        def operation():
            item = self._get_item(
                self._state_table, self._next_label_key(status_id))
            if item is None:
                return None, None
            return item.get('next_label'), item.get('trigger_message')
        return self._call(operation)

    def set_next_label(self, status_id, label, trigger_message):
        def operation():
            response = self._client().put_item(
                TableName=self._state_table,
                Item=self._item({
                    **self._next_label_key(status_id),
                    'status_id': status_id,
                    'next_label': label,
                    'trigger_message': trigger_message,
                }),
                ReturnValues='ALL_OLD',
            )
            old = response.get('Attributes')
            if not old:
                return None, None
            current = self._decode_item(old)
            if current.get('next_label'):
                return (
                    current['next_label'],
                    current.get('trigger_message'),
                )
            return None, None
        return self._call(operation)

    def compare_and_clear_next_label(self, status_id, next_label):
        for _attempt in range(self._GROUP_RETRY_LIMIT):
            current = self._call(lambda: self._get_item(
                self._state_table, self._next_label_key(status_id)))
            if current is None or current.get('next_label') != next_label:
                return None, None
            try:
                response = self._client().delete_item(
                    TableName=self._state_table,
                    Key=self._item(self._next_label_key(status_id)),
                    ConditionExpression='#next_label = :expected',
                    ExpressionAttributeNames={
                        '#next_label': 'next_label',
                    },
                    ExpressionAttributeValues={
                        ':expected': self._attribute(next_label),
                    },
                    ReturnValues='ALL_OLD',
                )
                removed = self._decode_item(response['Attributes'])
                return (
                    removed.get('next_label'),
                    removed.get('trigger_message'),
                )
            except Exception as error:
                if not self._is_conditional_error(error):
                    self._raise_store_error(error)
        raise StateConflictError('次ラベルの同時更新を完了できませんでした')

    def clear_next_label(self, status_id):
        def operation():
            self._client().put_item(
                TableName=self._state_table,
                Item=self._item({
                    **self._next_label_key(status_id),
                    'status_id': status_id,
                    'next_label': None,
                    'trigger_message': None,
                }),
            )
        return self._call(operation)

    def _cache_partition(self, key):
        return int(self._hash(key)[:2], 16) % self._CACHE_PARTITIONS

    def _build_cache_key(self, key):
        return {
            'pk': f'BUILD_CACHE#{self._cache_partition(key):02d}',
            'sk': f'KEY#{self._hash(key)}',
        }

    @staticmethod
    def _expire_timestamp(expire_at):
        if expire_at is None:
            return None
        return int(AwsStateStore._normalize_datetime(expire_at).timestamp())

    def get_build_cache(self, key):
        def operation():
            item = self._get_item(
                self._cache_table, self._build_cache_key(key))
            if item is None:
                return None
            expire_at = item.get('expire_at')
            if expire_at is not None and int(expire_at) <= int(
                    self._now().timestamp()):
                return None
            if item['storage'] == 'object':
                try:
                    return self._object_store.load_private(
                        item['object_key'])
                except ObjectStoreError as error:
                    raise StateStoreError(str(error)) from error
            return self._decode_payload(item['payload'])
        return self._call(operation)

    def set_build_cache(self, key, value, expire_at=None):
        def operation():
            item = {
                **self._build_cache_key(key),
                'cache_key': key,
                'expire_at': self._expire_timestamp(expire_at),
            }
            if isinstance(value, (bytes, bytearray)):
                body = bytes(value)
                digest = hashlib.sha256(body).hexdigest()
                object_key = (
                    f'build_cache/{self._hash(key)}/{digest}')
                try:
                    self._object_store.store_private(
                        object_key, body, 'application/octet-stream')
                except ObjectStoreError as error:
                    raise StateStoreError(str(error)) from error
                item.update({
                    'storage': 'object',
                    'object_key': object_key,
                })
            else:
                item.update({
                    'storage': 'inline',
                    'payload': self._encode_payload(value),
                })
            self._client().put_item(
                TableName=self._cache_table,
                Item=self._item(item),
            )
        return self._call(operation)

    def delete_build_cache(self, key):
        def operation():
            self._client().delete_item(
                TableName=self._cache_table,
                Key=self._item(self._build_cache_key(key)),
            )
        return self._call(operation)

    def clear_build_cache(self):
        def operation():
            for partition in range(self._CACHE_PARTITIONS):
                items = self._query_all(
                    self._cache_table,
                    KeyConditionExpression='#pk = :pk',
                    ExpressionAttributeNames={'#pk': 'pk'},
                    ExpressionAttributeValues={
                        ':pk': self._attribute(
                            f'BUILD_CACHE#{partition:02d}'),
                    },
                    ConsistentRead=True,
                )
                self._delete_items(self._cache_table, [
                    {'pk': item['pk'], 'sk': item['sk']}
                    for item in items
                ])
        return self._call(operation)

    @staticmethod
    def _validate_task_execution_text(value, description):
        if not isinstance(value, str) or not value:
            raise ValueError(f'{description}が不正です')

    @staticmethod
    def _task_execution_key(execution_key):
        digest = AwsStateStore._hash(execution_key)
        return {
            'pk': f'TASK_EXECUTION#{digest[:2]}',
            'sk': f'TASK#{digest}',
        }

    @classmethod
    def _raise_task_execution_error(cls, error, conflict=False):
        message = 'AWS非同期タスクの実行記録操作に失敗しました'
        if conflict and cls._is_conditional_error(error):
            raise StateConflictError(message) from error
        if isinstance(error, (ClientError, BotoCoreError)):
            raise StateStoreError(message) from error
        if type(error).__module__.startswith(('boto3.', 'botocore.')):
            raise StateStoreError(message) from error
        raise error

    def try_claim_task_execution(
            self, execution_key, owner, lease_seconds):
        """AWS worker用taskを取得し、claimed・busy・completedを返す。"""
        self._validate_task_execution_text(execution_key, 'execution key')
        self._validate_task_execution_text(owner, 'owner')
        if (
                isinstance(lease_seconds, bool)
                or not isinstance(lease_seconds, int)
                or lease_seconds <= 0):
            raise ValueError('lease秒数が不正です')

        now = int(self._now().timestamp())
        key = self._task_execution_key(execution_key)
        request = {
            'TableName': self._cache_table,
            'Item': self._item({
                **key,
                'status': TASK_EXECUTION_CLAIMED,
                'owner': owner,
                'lease_until': now + lease_seconds,
                'expire_at': now + self._TASK_EXECUTION_TTL_SECONDS,
            }),
            'ConditionExpression': (
                'attribute_not_exists(#pk) OR '
                '(#status = :claimed AND #lease_until <= :now)'),
            'ExpressionAttributeNames': {
                '#pk': 'pk',
                '#status': 'status',
                '#lease_until': 'lease_until',
            },
            'ExpressionAttributeValues': {
                ':claimed': self._attribute(TASK_EXECUTION_CLAIMED),
                ':now': self._attribute(now),
            },
        }
        try:
            self._client().put_item(**request)
            return TASK_EXECUTION_CLAIMED
        except Exception as error:
            if not self._is_conditional_error(error):
                self._raise_task_execution_error(error)

        try:
            current = self._get_item(self._cache_table, key)
        except Exception as error:
            self._raise_task_execution_error(error)
        if current is None:
            # 条件失敗直後のTTL削除などでは安全側に倒し、次の再試行へ回す。
            return TASK_EXECUTION_BUSY
        if current.get('status') == TASK_EXECUTION_COMPLETED:
            return TASK_EXECUTION_COMPLETED
        if current.get('status') == TASK_EXECUTION_CLAIMED:
            return TASK_EXECUTION_BUSY
        raise StateStoreError(
            'AWS非同期タスクの実行記録操作に失敗しました')

    def complete_task_execution(self, execution_key, owner):
        """同じownerが取得したAWS worker用taskだけを完了させる。"""
        self._validate_task_execution_text(execution_key, 'execution key')
        self._validate_task_execution_text(owner, 'owner')

        now = int(self._now().timestamp())
        key = self._task_execution_key(execution_key)
        request = {
            'TableName': self._cache_table,
            'Item': self._item({
                **key,
                'status': TASK_EXECUTION_COMPLETED,
                'owner': owner,
                'completed_at': now,
                'expire_at': now + self._TASK_EXECUTION_TTL_SECONDS,
            }),
            'ConditionExpression': '#status = :claimed AND #owner = :owner',
            'ExpressionAttributeNames': {
                '#status': 'status',
                '#owner': 'owner',
            },
            'ExpressionAttributeValues': {
                ':claimed': self._attribute(TASK_EXECUTION_CLAIMED),
                ':owner': self._attribute(owner),
            },
        }
        # lease切れだけではownerを失効させない。別workerが再取得した場合は
        # ownerが置き換わるため、この条件付き更新が競合として失敗する。
        try:
            self._client().put_item(**request)
        except Exception as error:
            self._raise_task_execution_error(error, conflict=True)

    def _task_data(self, item):
        return self._decode_payload(item['payload'])

    def _task_item(self, task_id, data, revision):
        task = dict(data)
        now = self._now()
        task.setdefault('created_at', now)
        task.setdefault('updated_at', now)
        task['created_at'] = self._normalize_datetime(task['created_at'])
        task['updated_at'] = self._normalize_datetime(task['updated_at'])
        if task.get('scheduled_at') is not None:
            task['scheduled_at'] = self._normalize_datetime(
                task['scheduled_at'])
        created_index = (
            f'{task["created_at"].isoformat(timespec="microseconds")}#'
            f'{task_id}')
        bot_name = task.get('bot_name')
        return {
            'task_id': task_id,
            'payload': self._encode_payload(task),
            'revision': int(revision),
            'bot_name_index': bot_name if bot_name else None,
            'created_at_index': created_index if bot_name else None,
        }

    def create_group_message_task(self, task_id, data):
        def operation():
            self._client().put_item(
                TableName=self._group_task_table,
                Item=self._item(self._task_item(task_id, data, 1)),
            )
        return self._call(operation)

    def get_group_message_task(self, task_id):
        def operation():
            item = self._get_item(
                self._group_task_table,
                {'task_id': task_id},
            )
            return self._task_data(item) if item else None
        return self._call(operation)

    def update_group_message_task(self, task_id, update_builder):
        for _attempt in range(self._TASK_RETRY_LIMIT):
            item = self._call(lambda: self._get_item(
                self._group_task_table, {'task_id': task_id}))
            if item is None:
                return False
            current = self._task_data(item)
            update = dict(update_builder(dict(current)))
            merged = dict(current)
            merged.update(update)
            if 'updated_at' not in update:
                merged['updated_at'] = self._now()
            next_item = self._task_item(
                task_id, merged, int(item['revision']) + 1)
            try:
                self._client().put_item(
                    TableName=self._group_task_table,
                    Item=self._item(next_item),
                    ConditionExpression='#revision = :revision',
                    ExpressionAttributeNames={'#revision': 'revision'},
                    ExpressionAttributeValues={
                        ':revision': self._attribute(int(item['revision'])),
                    },
                )
                return True
            except Exception as error:
                if not self._is_conditional_error(error):
                    self._raise_store_error(error)
        raise StateConflictError('配信Taskの同時更新を完了できませんでした')

    def get_recent_group_message_tasks(self, bot_name, limit):
        if limit <= 0:
            return []

        def operation():
            response = self._client().query(
                TableName=self._group_task_table,
                IndexName=self._group_task_index,
                KeyConditionExpression='#bot = :bot',
                ExpressionAttributeNames={'#bot': 'bot_name_index'},
                ExpressionAttributeValues={
                    ':bot': self._attribute(bot_name),
                },
                ScanIndexForward=False,
                Limit=int(limit),
            )
            tasks = []
            for raw_item in response.get('Items', []):
                item = self._decode_item(raw_item)
                task = self._task_data(item)
                task['id'] = item['task_id']
                tasks.append(task)
            return tasks
        return self._call(operation)
