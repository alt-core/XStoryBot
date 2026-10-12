"""Firestoreを使うStateStore実装。"""

import datetime
import importlib
import uuid

from google.api_core import exceptions
from google.api_core.retry import Retry, if_exception_type

from cloud_backend.contracts import (
    StateConflictError,
    StateStore,
    StateStoreError,
    StateVersion,
    VersionedState,
    checked_publication_generation,
)


_PLAYER_WRITE_ID = '_xstorybot_write_id'


class _PlayerWriteRetry(Retry):
    """条件拒否が最初のRPCか、結果不明な再送後かを区別する。"""

    def __init__(self):
        # 使用中のFirestore commitの既定policyを維持する。
        super().__init__(
            predicate=if_exception_type(exceptions.ResourceExhausted, exceptions.ServiceUnavailable),
            initial=0.1, maximum=60.0, multiplier=1.3, timeout=60.0)
        self.attempts = 0

    def __call__(self, func, on_error=None):
        def counted(*args, **kwargs):
            self.attempts += 1
            return func(*args, **kwargs)
        return super().__call__(counted, on_error)


class GcpStateStore(StateStore):
    """Firestoreの保存形式と更新意味論を実装する。"""

    _CONFLICT_ERROR_NAMES = {
        'Aborted',
        'AlreadyExists',
        'Conflict',
        'FailedPrecondition',
        'NotFound',
    }

    def __init__(self, client=None):
        self._firestore = importlib.import_module('google.cloud.firestore')
        try:
            self.client = (
                client if client is not None else self._firestore.Client())
        except Exception as error:
            self._raise_store_error(error)
        self._opaque_versions = {}

    @classmethod
    def _is_google_error(cls, error):
        return type(error).__module__.startswith('google.')

    @classmethod
    def _is_conflict_error(cls, error):
        current = error
        visited = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            if (
                    cls._is_google_error(current)
                    and type(current).__name__ in cls._CONFLICT_ERROR_NAMES):
                return True
            current = current.__cause__ or current.__context__
        return False

    @classmethod
    def _raise_store_error(cls, error, conflict=False):
        if conflict and cls._is_conflict_error(error):
            raise StateConflictError(str(error)) from error
        if cls._is_google_error(error):
            raise StateStoreError(str(error)) from error
        raise error

    def _call(self, operation, conflict=False):
        try:
            return operation()
        except Exception as error:
            self._raise_store_error(error, conflict=conflict)

    def _encode_version(self, update_time):
        if hasattr(update_time, 'timestamp_pb'):
            timestamp = update_time.timestamp_pb()
            return StateVersion(
                f'firestore:v1:{timestamp.seconds}:{timestamp.nanos}')

        if isinstance(update_time, datetime.datetime):
            return StateVersion(f'datetime:v1:{update_time.isoformat()}')

        # テスト用fakeなど、Firestore Timestampではない値も同一instance内では
        # 失わずにwrite preconditionへ戻せるようにする。
        token = StateVersion(f'opaque:v1:{uuid.uuid4().hex}')
        self._opaque_versions[token.value] = update_time
        return token

    def _decode_version(self, version):
        if not isinstance(version, StateVersion):
            raise TypeError('versionにはStateVersionを指定してください')

        if version.value.startswith('firestore:v1:'):
            _prefix, _format_version, seconds, nanos = version.value.split(':')
            try:
                from google.api_core.datetime_helpers import DatetimeWithNanoseconds
                from google.protobuf.timestamp_pb2 import Timestamp

                timestamp = Timestamp(
                    seconds=int(seconds),
                    nanos=int(nanos),
                )
                return DatetimeWithNanoseconds.from_timestamp_pb(timestamp)
            except Exception as error:
                self._raise_store_error(error)

        if version.value.startswith('datetime:v1:'):
            return datetime.datetime.fromisoformat(
                version.value[len('datetime:v1:'):])

        if version.value.startswith('opaque:v1:'):
            try:
                return self._opaque_versions[version.value]
            except KeyError as error:
                raise StateStoreError(
                    'このプロセスでは復元できないversion tokenです') from error

        raise StateStoreError('未対応のversion tokenです')

    def _version_from_write_result(self, result):
        return self._encode_version(result.update_time)

    @staticmethod
    def _normalize_task_datetimes(data):
        normalized = dict(data)
        for key in ('created_at', 'updated_at', 'scheduled_at'):
            value = normalized.get(key)
            if isinstance(value, datetime.datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=datetime.timezone.utc)
                normalized[key] = datetime.datetime.fromtimestamp(
                    value.timestamp(), tz=datetime.timezone.utc)
        return normalized

    def get_global_bot_variables(self, bot_name):
        def operation():
            document = self.client.collection(
                'global_bot_variables').document(bot_name).get()
            if not document.exists:
                return None
            data = document.to_dict()
            return {**data, 'generation': checked_publication_generation(data.get('generation'))}
        return self._call(operation)

    def save_global_bot_variables(self, bot_name, scenario_uri):
        def operation():
            document = self.client.collection('global_bot_variables').document(bot_name)
            transaction = self.client.transaction()

            @self._firestore.transactional
            def publish(current_transaction):
                snapshot = document.get(transaction=current_transaction)
                data = snapshot.to_dict() if snapshot.exists else {}
                generation = checked_publication_generation(
                    data['generation'], allow_missing=False) if 'generation' in data else 0
                generation += 1
                current_transaction.set(document, {
                    'scenario_uri': scenario_uri, 'generation': generation})
                return generation

            return publish(transaction)
        return self._call(operation, conflict=True)

    def load_player_status(self, status_id):
        def operation():
            document = self.client.collection(
                'player_status').document(status_id).get()
            if not document.exists:
                return None
            data = document.to_dict()
            data.pop(_PLAYER_WRITE_ID, None)
            return VersionedState(
                data=data,
                version=self._encode_version(document.update_time),
            )
        return self._call(operation)

    def _write_player(self, document, data, operation):
        payload = dict(data)
        if _PLAYER_WRITE_ID in payload:
            raise ValueError('Player状態には保存先の予約fieldを指定できません')
        write_id = uuid.uuid4().hex
        payload[_PLAYER_WRITE_ID] = write_id
        retry = _PlayerWriteRetry()
        try:
            result = operation(payload, retry)
            return self._version_from_write_result(result)
        except Exception as error:
            if retry.attempts == 1 and self._is_conflict_error(error):
                self._raise_store_error(error, conflict=True)
            try:
                snapshot = document.get()
                if snapshot.exists and snapshot.to_dict().get(_PLAYER_WRITE_ID) == write_id:
                    return self._encode_version(snapshot.update_time)
            except Exception:
                pass
            # 内容一致や現在の欠落では、自分の書込みが無かったと判断できない。
            raise StateStoreError('Player状態の保存結果を確認できません') from error

    def create_player_status(self, status_id, data):
        document = self.client.collection('player_status').document(status_id)
        return self._write_player(document, data, lambda payload, retry: document.create(payload, retry=retry))

    def update_player_status(self, status_id, data, version):
        option = self.client.write_option(last_update_time=self._decode_version(version))
        document = self.client.collection('player_status').document(status_id)
        return self._write_player(
            document, data, lambda payload, retry: document.update(payload, option=option, retry=retry))

    def force_put_player_status(self, status_id, data):
        document = self.client.collection('player_status').document(status_id)
        return self._write_player(document, data, lambda payload, retry: document.set(payload, retry=retry))

    def delete_player_status(self, status_id, version=None):
        def operation():
            document = self.client.collection('player_status').document(status_id)
            if version is None:
                document.delete()
            else:
                option = self.client.write_option(
                    last_update_time=self._decode_version(version))
                retry = _PlayerWriteRetry()
                try:
                    document.delete(option=option, retry=retry)
                except Exception as error:
                    if retry.attempts == 1 and self._is_conflict_error(error):
                        self._raise_store_error(error, conflict=True)
                    try:
                        if not document.get().exists:
                            return
                    except Exception:
                        pass
                    raise StateStoreError('Player状態の削除結果を確認できません') from error
        return self._call(operation)

    def _group_shards(self, group_id):
        return self.client.collection(
            'group_members').document(group_id).collection('shards')

    def get_group_members(self, group_id):
        def operation():
            members = []
            for document in self._group_shards(group_id).stream():
                members.extend(document.to_dict().get('members', []))
            return members
        return self._call(operation)

    def append_group_member(self, group_id, shard_id, member):
        def operation():
            shard_ref = self._group_shards(group_id).document(shard_id)
            transaction = self.client.transaction()

            @self._firestore.transactional
            def update_shard(current_transaction):
                snapshot = shard_ref.get(transaction=current_transaction)
                data = snapshot.to_dict() if snapshot.exists else {}
                members = data.get('members', [])
                if member not in members:
                    members.append(member)
                    current_transaction.set(
                        shard_ref, {'members': members})

            return update_shard(transaction)
        return self._call(operation, conflict=True)

    def remove_group_member(self, group_id, shard_id, member):
        def operation():
            shard_ref = self._group_shards(group_id).document(shard_id)
            transaction = self.client.transaction()

            @self._firestore.transactional
            def update_shard(current_transaction):
                snapshot = shard_ref.get(transaction=current_transaction)
                if not snapshot.exists:
                    return None
                members = snapshot.to_dict().get('members', [])
                if member in members:
                    members.remove(member)
                    current_transaction.set(
                        shard_ref, {'members': members})
                return None

            return update_shard(transaction)
        return self._call(operation, conflict=True)

    def clear_group_members(self, group_id):
        def operation():
            shard_collection = self._group_shards(group_id)
            batch = self.client.batch()
            for document in shard_collection.stream():
                batch.delete(document.reference)
            batch.commit()
        return self._call(operation)

    def get_all_groups(self):
        def operation():
            group_ids = set()
            for document in self.client.collection_group('shards').get():
                path_parts = document.reference.path.split('/')
                if len(path_parts) >= 2:
                    group_ids.add(path_parts[1])
            return [{'id': group_id} for group_id in group_ids]
        return self._call(operation)

    def _get_stat(self, collection_name, key):
        document = self.client.collection(
            collection_name).document(key).get()
        return document.to_dict() if document.exists else None

    def _put_stat(self, collection_name, key, data):
        self.client.collection(
            collection_name).document(key).set(dict(data))

    def get_image_file_stat(self, key):
        return self._call(
            lambda: self._get_stat('image_file_stats', key))

    def put_image_file_stat(self, key, data):
        return self._call(
            lambda: self._put_stat('image_file_stats', key, data))

    def get_media_file_stat(self, key):
        return self._call(
            lambda: self._get_stat('media_file_stats', key))

    def put_media_file_stat(self, key, data):
        return self._call(
            lambda: self._put_stat('media_file_stats', key, data))

    def get_image_text_stat(self, key):
        return self._call(
            lambda: self._get_stat('image_text_stats', key))

    def put_image_text_stat(self, key, data):
        return self._call(
            lambda: self._put_stat('image_text_stats', key, data))

    def get_next_label(self, status_id):
        def operation():
            document = self.client.collection(
                'player_next_labels').document(status_id).get()
            if not document.exists:
                return None, None
            data = document.to_dict()
            return data.get('next_label'), data.get('trigger_message')
        return self._call(operation)

    def set_next_label(self, status_id, label, trigger_message):
        def operation():
            document_ref = self.client.collection(
                'player_next_labels').document(status_id)
            transaction = self.client.transaction()

            @self._firestore.transactional
            def update_in_transaction(current_transaction):
                document = document_ref.get(
                    transaction=current_transaction)
                overwrite = (None, None)
                data = {
                    'next_label': label,
                    'trigger_message': trigger_message,
                }
                if not document.exists:
                    current_transaction.set(document_ref, data)
                else:
                    current = document.to_dict()
                    if current.get('next_label'):
                        overwrite = (
                            current['next_label'],
                            current.get('trigger_message'),
                        )
                    current_transaction.update(document_ref, data)
                return overwrite

            return update_in_transaction(transaction)
        return self._call(operation, conflict=True)

    def compare_and_clear_next_label(self, status_id, next_label):
        def operation():
            document_ref = self.client.collection(
                'player_next_labels').document(status_id)
            transaction = self.client.transaction()

            @self._firestore.transactional
            def update_in_transaction(current_transaction):
                document = document_ref.get(
                    transaction=current_transaction)
                if document.exists:
                    data = document.to_dict()
                    current = (
                        data.get('next_label'),
                        data.get('trigger_message'),
                    )
                    if current[0] == next_label:
                        current_transaction.update(document_ref, {
                            'next_label': None,
                            'trigger_message': None,
                        })
                        return current
                return None, None

            return update_in_transaction(transaction)
        return self._call(operation, conflict=True)

    def clear_next_label(self, status_id):
        def operation():
            self.client.collection(
                'player_next_labels').document(status_id).set({
                'next_label': None,
                'trigger_message': None,
            })
        return self._call(operation)

    def get_build_cache(self, key):
        def operation():
            document = self.client.collection(
                'build_cache').document(key).get()
            return document.to_dict().get('value') if document.exists else None
        return self._call(operation)

    def set_build_cache(self, key, value, expire_at=None):
        data = {'value': value}
        if expire_at is not None:
            data['expireAt'] = expire_at
        def operation():
            self.client.collection('build_cache').document(key).set(data)
        return self._call(operation)

    def delete_build_cache(self, key):
        def operation():
            self.client.collection(
                'build_cache').document(key).delete()
        return self._call(operation)

    def clear_build_cache(self):
        def operation():
            collection = self.client.collection('build_cache')
            for document in collection.stream():
                collection.document(document.id).delete()
        return self._call(operation)

    def create_group_message_task(self, task_id, data):
        task_data = dict(data)
        task_data.setdefault(
            'created_at', self._firestore.SERVER_TIMESTAMP)
        task_data.setdefault(
            'updated_at', self._firestore.SERVER_TIMESTAMP)
        def operation():
            self.client.collection(
                'group_message_tasks').document(task_id).set(task_data)
        return self._call(operation)

    def get_group_message_task(self, task_id):
        def operation():
            document = self.client.collection(
                'group_message_tasks').document(task_id).get()
            if not document.exists:
                return None
            return self._normalize_task_datetimes(document.to_dict())
        return self._call(operation)

    def update_group_message_task(self, task_id, update_builder):
        def operation():
            document_ref = self.client.collection(
                'group_message_tasks').document(task_id)
            transaction = self.client.transaction()

            @self._firestore.transactional
            def update_in_transaction(current_transaction):
                document = document_ref.get(
                    transaction=current_transaction)
                if not document.exists:
                    return False
                current_data = self._normalize_task_datetimes(
                    document.to_dict())
                update_data = dict(update_builder(current_data))
                update_data.setdefault(
                    'updated_at', self._firestore.SERVER_TIMESTAMP)
                current_transaction.update(document_ref, update_data)
                return True

            return update_in_transaction(transaction)
        return self._call(operation, conflict=True)

    def get_recent_group_message_tasks(self, bot_name, limit):
        def operation():
            from google.cloud.firestore_v1.base_query import FieldFilter

            # bot_name の等値 + created_at 降順の複合 index が必要（README の GCP 準備を参照）
            query = self.client.collection('group_message_tasks').where(
                filter=FieldFilter('bot_name', '==', bot_name)
            ).order_by(
                'created_at', direction=self._firestore.Query.DESCENDING
            ).limit(limit)
            tasks = []
            for document in query.stream():
                task = self._normalize_task_datetimes(document.to_dict())
                task['id'] = document.id
                tasks.append(task)
            return tasks
        return self._call(operation)
