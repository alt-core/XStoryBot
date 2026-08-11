"""Firestoreを使うStateStore実装。"""

import datetime
import importlib
import uuid

from cloud_backend.contracts import (
    StateConflictError,
    StateStore,
    StateStoreError,
    StateVersion,
    VersionedState,
)


class GcpStateStore(StateStore):
    """既存Firestoreの保存形式と更新意味論を維持する。"""

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
            if type(current).__name__ in cls._CONFLICT_ERROR_NAMES:
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
                    normalized[key] = value.replace(
                        tzinfo=datetime.timezone.utc)
                else:
                    normalized[key] = datetime.datetime.fromtimestamp(
                        value.timestamp(), tz=datetime.timezone.utc)
        return normalized

    def get_global_bot_variables(self, bot_name):
        def operation():
            document = self.client.collection(
                'global_bot_variables').document(bot_name).get()
            return document.to_dict() if document.exists else None
        return self._call(operation)

    def save_global_bot_variables(self, bot_name, scenario_uri):
        return self._call(lambda: self.client.collection(
            'global_bot_variables').document(bot_name).set({
                'scenario_uri': scenario_uri,
            }))

    def load_player_status(self, status_id):
        def operation():
            document = self.client.collection(
                'player_status').document(status_id).get()
            if not document.exists:
                return None
            return VersionedState(
                data=document.to_dict(),
                version=self._encode_version(document.update_time),
            )
        return self._call(operation)

    def create_player_status(self, status_id, data):
        def operation():
            result = self.client.collection(
                'player_status').document(status_id).create(dict(data))
            return self._version_from_write_result(result)
        return self._call(operation, conflict=True)

    def update_player_status(self, status_id, data, version):
        def operation():
            option = self.client.write_option(
                last_update_time=self._decode_version(version))
            result = self.client.collection(
                'player_status').document(status_id).update(
                    dict(data), option=option)
            return self._version_from_write_result(result)
        return self._call(operation, conflict=True)

    def force_put_player_status(self, status_id, data):
        def operation():
            result = self.client.collection(
                'player_status').document(status_id).set(dict(data))
            return self._version_from_write_result(result)
        return self._call(operation)

    def delete_player_status(self, status_id):
        return self._call(lambda: self.client.collection(
            'player_status').document(status_id).delete())

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
            return batch.commit()
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
        return self.client.collection(
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
        return self._call(lambda: self.client.collection(
            'player_next_labels').document(status_id).set({
                'next_label': None,
                'trigger_message': None,
            }))

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
        return self._call(lambda: self.client.collection(
            'build_cache').document(key).set(data))

    def delete_build_cache(self, key):
        return self._call(lambda: self.client.collection(
            'build_cache').document(key).delete())

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
        return self._call(lambda: self.client.collection(
            'group_message_tasks').document(task_id).set(task_data))

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
                update_data = dict(update_builder(document.to_dict()))
                update_data.setdefault(
                    'updated_at', self._firestore.SERVER_TIMESTAMP)
                current_transaction.update(document_ref, update_data)
                return True

            return update_in_transaction(transaction)
        return self._call(operation, conflict=True)

    def get_recent_group_message_tasks(self, bot_name, limit):
        def operation():
            from google.cloud.firestore_v1.base_query import FieldFilter

            query = self.client.collection('group_message_tasks').where(
                filter=FieldFilter('bot_name', '==', bot_name)).limit(limit)
            documents = list(query.stream())

            def get_created_at(document):
                return document.to_dict().get('created_at', 0) or 0

            tasks = []
            for document in sorted(
                    documents, key=get_created_at, reverse=True)[:limit]:
                task = self._normalize_task_datetimes(document.to_dict())
                task['id'] = document.id
                tasks.append(task)
            return tasks
        return self._call(operation)
