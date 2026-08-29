import datetime
import sys
import types
import unittest
from unittest.mock import patch

from google.api_core import exceptions

from cloud_backend.gcp.state_store import GcpStateStore
from tests.cloud_backend.state_store_contract import StateStoreContractMixin


class _UpdateTime:
    def __init__(self, value):
        self._value = value

    def timestamp_pb(self):
        return types.SimpleNamespace(
            seconds=self._value,
            nanos=0,
        )

    def __eq__(self, other):
        return isinstance(other, _UpdateTime) and self._value == other._value


class _Snapshot:
    def __init__(self, reference, data, update_time):
        self.reference = reference
        self.id = reference.id
        self.exists = data is not None
        self._data = None if data is None else dict(data)
        self.update_time = update_time

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _DocumentReference:
    def __init__(self, client, path):
        self._client = client
        self.path = path
        self.id = path.rsplit('/', 1)[-1]

    def collection(self, name):
        return _CollectionReference(self._client, f'{self.path}/{name}')

    def get(self, transaction=None):
        del transaction
        record = self._client._documents.get(self.path)
        if record is None:
            return _Snapshot(self, None, None)
        return _Snapshot(self, record['data'], record['update_time'])

    def create(self, data):
        if self.path in self._client._documents:
            raise exceptions.AlreadyExists('already exists')
        return self._write(data)

    def set(self, data):
        return self._write(data)

    def update(self, data, option=None):
        record = self._client._documents.get(self.path)
        if record is None:
            raise exceptions.NotFound('missing')
        if option is not None and (
                self._version_parts(record['update_time'])
                != self._version_parts(option)):
            raise exceptions.FailedPrecondition('stale version')
        merged = dict(record['data'])
        merged.update(data)
        return self._write(merged)

    @staticmethod
    def _version_parts(value):
        if hasattr(value, 'timestamp_pb'):
            timestamp = value.timestamp_pb()
            return timestamp.seconds, timestamp.nanos
        if hasattr(value, 'seconds') and hasattr(value, 'nanos'):
            return value.seconds, value.nanos
        return value

    def delete(self):
        self._client._documents.pop(self.path, None)
        return None

    def _write(self, data):
        update_time = self._client._next_update_time()
        self._client._documents[self.path] = {
            'data': self._client._resolve_values(data),
            'update_time': update_time,
        }
        return types.SimpleNamespace(update_time=update_time)


class _CollectionReference:
    def __init__(self, client, path):
        self._client = client
        self.path = path

    def document(self, document_id):
        return _DocumentReference(
            self._client, f'{self.path}/{document_id}')

    def stream(self):
        prefix = f'{self.path}/'
        expected_parts = len(self.path.split('/')) + 1
        documents = []
        for path, record in self._client._documents.items():
            if path.startswith(prefix) and len(path.split('/')) == expected_parts:
                reference = _DocumentReference(self._client, path)
                documents.append(_Snapshot(
                    reference, record['data'], record['update_time']))
        return iter(documents)

    def get(self):
        return list(self.stream())

    def where(self, filter=None):
        return _Query(list(self.stream()), filter=filter)


class _Query:
    def __init__(self, documents, filter=None, limit=None):
        self._documents = list(documents)
        self._filter = filter
        self._limit = limit

    def limit(self, limit):
        return _Query(self._documents, self._filter, limit)

    def stream(self):
        documents = self._documents
        if self._filter is not None:
            field = getattr(
                self._filter, 'field_path',
                getattr(self._filter, 'field_name', None),
            )
            expected = self._filter.value
            documents = [
                document for document in documents
                if document.to_dict().get(field) == expected
            ]
        if self._limit is not None:
            documents = documents[:self._limit]
        return iter(documents)


class _CollectionGroup:
    def __init__(self, client, collection_id):
        self._client = client
        self._collection_id = collection_id

    def get(self):
        documents = []
        for path, record in self._client._documents.items():
            parts = path.split('/')
            if len(parts) >= 2 and parts[-2] == self._collection_id:
                reference = _DocumentReference(self._client, path)
                documents.append(_Snapshot(
                    reference, record['data'], record['update_time']))
        return documents


class _Transaction:
    def set(self, reference, data):
        return reference.set(data)

    def update(self, reference, data):
        return reference.update(data)


class _Batch:
    def __init__(self):
        self._deletes = []

    def delete(self, reference):
        self._deletes.append(reference)

    def commit(self):
        for reference in self._deletes:
            reference.delete()
        return []


class _MemoryFirestoreClient:
    def __init__(self, server_timestamp):
        self._documents = {}
        self._counter = 0
        self._server_timestamp = server_timestamp

    def collection(self, name):
        return _CollectionReference(self, name)

    def collection_group(self, collection_id):
        return _CollectionGroup(self, collection_id)

    def transaction(self):
        return _Transaction()

    def batch(self):
        return _Batch()

    @staticmethod
    def write_option(last_update_time):
        return last_update_time

    def _next_update_time(self):
        self._counter += 1
        return _UpdateTime(self._counter)

    def _resolve_values(self, value):
        if value is self._server_timestamp:
            return datetime.datetime.now(datetime.timezone.utc)
        if isinstance(value, dict):
            return {
                key: self._resolve_values(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._resolve_values(item) for item in value]
        return value


class GcpStateStoreContractTest(StateStoreContractMixin, unittest.TestCase):
    """AWSと同一のMixinを既存GcpStateStoreへ適用する。"""

    def setUp(self):
        self.server_timestamp = object()
        self.firestore = types.SimpleNamespace(
            SERVER_TIMESTAMP=self.server_timestamp,
            transactional=lambda function: function,
        )
        self.client = _MemoryFirestoreClient(self.server_timestamp)
        self.import_module = patch(
            'cloud_backend.gcp.state_store.importlib.import_module',
            return_value=self.firestore,
        )
        self.import_module.start()
        base_query = types.ModuleType(
            'google.cloud.firestore_v1.base_query')

        class FieldFilter:
            def __init__(self, field_path, op_string, value):
                self.field_path = field_path
                self.op_string = op_string
                self.value = value

        base_query.FieldFilter = FieldFilter
        self.firestore_modules = patch.dict(sys.modules, {
            'google.cloud.firestore_v1.base_query': base_query,
        })
        self.firestore_modules.start()
        super().setUp()

    def tearDown(self):
        self.firestore_modules.stop()
        self.import_module.stop()
        super().tearDown()

    def create_contract_store(self):
        return GcpStateStore(client=self.client)


if __name__ == '__main__':
    unittest.main()
