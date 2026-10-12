import datetime
import sys
import types
import unittest
from unittest.mock import Mock, patch

from google.api_core import exceptions
from google.api_core.retry import retry_unary

from cloud_backend.gcp.state_store import GcpStateStore, _PLAYER_WRITE_ID, _PlayerWriteRetry
from cloud_backend.contracts import StateConflictError, StateStoreError
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

    def create(self, data, *, retry=None):
        def operation():
            if self.path in self._client._documents:
                raise exceptions.AlreadyExists('already exists')
            return self._write(data)
        return retry(operation)() if retry is not None else operation()

    def set(self, data, *, retry=None):
        operation = lambda: self._write(data)
        return retry(operation)() if retry is not None else operation()

    def update(self, data, option=None, *, retry=None):
        def operation():
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
        return retry(operation)() if retry is not None else operation()

    @staticmethod
    def _version_parts(value):
        if hasattr(value, 'timestamp_pb'):
            timestamp = value.timestamp_pb()
            return timestamp.seconds, timestamp.nanos
        if hasattr(value, 'seconds') and hasattr(value, 'nanos'):
            return value.seconds, value.nanos
        return value

    def delete(self, option=None, *, retry=None):
        def operation():
            record = self._client._documents.get(self.path)
            if option is not None and (record is None or (
                    self._version_parts(record['update_time'])
                    != self._version_parts(option))):
                raise exceptions.FailedPrecondition('stale version')
            self._client._documents.pop(self.path, None)
        return retry(operation)() if retry is not None else operation()

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
    def __init__(self, documents, filter=None, limit=None, order=None):
        self._documents = list(documents)
        self._filter = filter
        self._limit = limit
        self._order = order

    def limit(self, limit):
        return _Query(self._documents, self._filter, limit, self._order)

    def order_by(self, field, direction='ASCENDING'):
        return _Query(self._documents, self._filter, self._limit, (field, direction))

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
        if self._order is not None:
            field, direction = self._order
            # Firestore と同じく、並べ替え field を持たない document は結果に含めない
            documents = sorted(
                [document for document in documents if field in document.to_dict()],
                key=lambda document: document.to_dict()[field],
                reverse=(direction == 'DESCENDING'))
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
            Query=types.SimpleNamespace(DESCENDING='DESCENDING'),
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

    def test_Player保存はSDK再送を維持し正常時の確認読取を増やさない(self):
        create, update, delete, set_value = (
            _DocumentReference.create, _DocumentReference.update,
            _DocumentReference.delete, _DocumentReference.set)
        store = self.contract_store
        with patch.object(_DocumentReference, 'get', autospec=True, side_effect=_DocumentReference.get) as read, \
                patch.object(_DocumentReference, 'create', autospec=True, side_effect=create) as created, \
                patch.object(_DocumentReference, 'update', autospec=True, side_effect=update) as updated, \
                patch.object(_DocumentReference, 'delete', autospec=True, side_effect=delete) as deleted, \
                patch.object(_DocumentReference, 'set', autospec=True, side_effect=set_value) as forced:
            version = store.create_player_status('status', {'scene': 'one'})
            version = store.update_player_status('status', {'scene': 'two'}, version)
            store.delete_player_status('status', version)
            store.force_put_player_status('forced', {'scene': 'forced'})
            store.delete_player_status('forced')
            read.assert_not_called()
            store.save_global_bot_variables('bot', 'scenario')
            store.append_group_member('group', 'shard', 'line:user,one')
        for retry in (created.call_args.kwargs['retry'], updated.call_args.kwargs['retry'],
                      deleted.call_args_list[0].kwargs['retry']):
            self.assertIsInstance(retry, _PlayerWriteRetry)
            self.assertEqual(1, retry.attempts)
        self.assertEqual({}, deleted.call_args_list[1].kwargs)
        player_force = [call for call in forced.call_args_list if call.args[0].path.startswith('player_status/')]
        self.assertIsInstance(player_force[0].kwargs['retry'], _PlayerWriteRetry)
        self.assertTrue(all(not call.kwargs for call in forced.call_args_list if call not in player_force))

    def test_Playerの保存ACK喪失はSDK再送後に自分のUUIDで成功を確認する(self):
        store = self.contract_store
        for create in (True, False):
            with self.subTest(create=create):
                status_id = 'create' if create else 'update'
                previous = None if create else store.create_player_status(status_id, {'scene': 'old'})
                write = _DocumentReference._write
                def committed_without_ack(reference, data):
                    write(reference, data)
                    raise exceptions.ServiceUnavailable('保存後の応答喪失')
                with patch.object(_DocumentReference, '_write', committed_without_ack), \
                        patch.object(retry_unary.time, 'sleep'), \
                        patch.object(_DocumentReference, 'get', autospec=True, side_effect=_DocumentReference.get) as read:
                    version = (store.create_player_status(status_id, {'scene': 'one'}) if create else
                               store.update_player_status(status_id, {'scene': 'one'}, previous))
                    self.assertEqual(1, read.call_count)
                saved = store.load_player_status(status_id)
                self.assertEqual(version, saved.version)
                self.assertEqual({'scene': 'one'}, saved.data)

    def test_Playerの一時throttleは同じUUIDで再送して保存する(self):
        write = _DocumentReference._write
        ids = []
        def throttled_once(reference, data):
            ids.append(data[_PLAYER_WRITE_ID])
            if len(ids) == 1:
                raise exceptions.ResourceExhausted('一時的な制限')
            return write(reference, data)
        with patch.object(_DocumentReference, '_write', throttled_once), \
                patch.object(retry_unary.time, 'sleep'):
            version = self.contract_store.create_player_status('status', {'scene': 'one'})
        self.assertEqual(2, len(ids))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(version, self.contract_store.load_player_status('status').version)

    def test_同じ内容を書いた他者のUUIDを自分の保存とみなさない(self):
        store = self.contract_store
        previous = store.create_player_status('status', {'scene': 'old'})
        write = _DocumentReference._write
        other_versions = []
        def committed_then_other_writer(reference, data):
            result = write(reference, data)
            if not other_versions:
                other_versions.append('writing')
                other_versions[0] = store.force_put_player_status('status', {'scene': 'new'})
                raise exceptions.ServiceUnavailable('応答喪失')
            return result
        with patch.object(_DocumentReference, '_write', committed_then_other_writer), \
                patch.object(retry_unary.time, 'sleep'), \
                self.assertRaises(StateStoreError) as raised:
            store.update_player_status('status', {'scene': 'new'}, previous)
        self.assertNotIsInstance(raised.exception, StateConflictError)
        saved = store.load_player_status('status')
        self.assertEqual({'scene': 'new'}, saved.data)
        self.assertEqual(other_versions[0], saved.version)
        with self.assertRaises(StateConflictError):
            store.update_player_status('status', {'scene': 'old'}, previous)
        self.assertEqual(saved, store.load_player_status('status'))

    def test_確認読取の失敗や文書欠落から未保存と推測しない(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                def unknown(reference, data, *, retry):
                    def rpc():
                        raise RuntimeError('書込み結果不明')
                    return retry(rpc)()
                read_result = types.SimpleNamespace(exists=False)
                with patch.object(_DocumentReference, 'create', unknown), \
                        patch.object(_DocumentReference, 'get', return_value=read_result,
                                     side_effect=None if missing else exceptions.ServiceUnavailable('読取不能')) as read, \
                        self.assertRaises(StateStoreError) as raised:
                    self.contract_store.create_player_status('status', {'scene': 'new'})
                self.assertNotIsInstance(raised.exception, StateConflictError)
                read.assert_called_once()

    def test_Retryが実行されず履歴不明なら条件拒否でも既知競合にしない(self):
        with patch.object(_DocumentReference, 'create', side_effect=exceptions.AlreadyExists('履歴不明')), \
                self.assertRaises(StateStoreError) as raised:
            self.contract_store.create_player_status('status', {'scene': 'new'})
        self.assertNotIsInstance(raised.exception, StateConflictError)

    def test_UUIDは論理保存ごとに替わり公開dataへ出ず予約field衝突を拒否する(self):
        store = self.contract_store
        ids = []
        version = store.create_player_status('status', {'scene': 'first'})
        ids.append(self.client._documents['player_status/status']['data'][_PLAYER_WRITE_ID])
        version = store.update_player_status('status', {'scene': 'second'}, version)
        ids.append(self.client._documents['player_status/status']['data'][_PLAYER_WRITE_ID])
        store.update_player_status('status', {'scene': 'first'}, version)
        ids.append(self.client._documents['player_status/status']['data'][_PLAYER_WRITE_ID])
        store.force_put_player_status('status', {'scene': 'forced'})
        ids.append(self.client._documents['player_status/status']['data'][_PLAYER_WRITE_ID])
        self.assertEqual(4, len(set(ids)))
        self.assertEqual({'scene': 'forced'}, store.load_player_status('status').data)
        before = store.load_player_status('status')
        with self.assertRaises(ValueError):
            store.force_put_player_status('status', {_PLAYER_WRITE_ID: 'collision'})
        self.assertEqual(before, store.load_player_status('status'))

    def test_条件付き削除のACK喪失は欠落だけ成功とし再作成された他者を消さない(self):
        store = self.contract_store
        delete = _DocumentReference.delete
        for recreate in (True, False):
            with self.subTest(recreate=recreate):
                status_id = 'recreate' if recreate else 'missing'
                version = store.create_player_status(status_id, {'scene': 'one'})
                lost = [False]
                def lost_delete_ack(reference, option=None, *, retry=None):
                    def rpc():
                        result = delete(reference, option=option)
                        if not lost[0]:
                            lost[0] = True
                            if recreate:
                                store.create_player_status(status_id, {'scene': 'other'})
                            raise exceptions.ServiceUnavailable('削除応答喪失')
                        return result
                    return retry(rpc)()
                with patch.object(_DocumentReference, 'delete', lost_delete_ack), \
                        patch.object(retry_unary.time, 'sleep'):
                    if recreate:
                        with self.assertRaises(StateStoreError) as raised:
                            store.delete_player_status(status_id, version)
                        self.assertNotIsInstance(raised.exception, StateConflictError)
                    else:
                        store.delete_player_status(status_id, version)
                saved = store.load_player_status(status_id)
                self.assertEqual({'scene': 'other'}, saved.data) if recreate else self.assertIsNone(saved)

    def test_publicGAX_wrapperでも再送後の条件拒否を複数試行として記録する(self):
        from google.api_core.gapic_v1.method import wrap_method
        retry = _PlayerWriteRetry()
        target = Mock(side_effect=[exceptions.ResourceExhausted('制限'), exceptions.FailedPrecondition('条件拒否')])
        wrapped = wrap_method(target)
        with patch.object(retry_unary.time, 'sleep'), \
                self.assertRaises(exceptions.FailedPrecondition):
            wrapped(retry=retry)
        self.assertEqual(2, retry.attempts)

    def test_公開transactionが再試行されたときは最新世代から採番する(self):
        store = self.contract_store
        self.assertEqual(1, store.save_global_bot_variables('bot', 'first'))
        document = self.client.collection('global_bot_variables').document('bot')

        def retry(publish):
            def run(transaction):
                writes = []
                transaction.set = lambda reference, data: writes.append((reference, data))
                self.assertEqual(2, publish(transaction))
                document.set({'scenario_uri': 'other-writer', 'generation': 2})
                writes.clear()
                generation = publish(transaction)
                for reference, data in writes:
                    reference.set(data)
                return generation
            return run

        with patch.object(self.firestore, 'transactional', retry):
            self.assertEqual(3, store.save_global_bot_variables('bot', 'final'))
        self.assertEqual({'scenario_uri': 'final', 'generation': 3},
                         store.get_global_bot_variables('bot'))

    def test_世代欠損pointerを読んでも正常0にせず初回公開だけ1にする(self):
        document = self.client.collection('global_bot_variables').document('bot')
        document.set({'scenario_uri': 'old'})
        store = self.contract_store
        self.assertEqual({'scenario_uri': 'old', 'generation': None}, store.get_global_bot_variables('bot'))
        self.assertEqual(1, store.save_global_bot_variables('bot', 'new'))

    def test_不正な公開世代はtransaction内で拒否しURIを変更しない(self):
        from cloud_backend.contracts import StateStoreError
        document = self.client.collection('global_bot_variables').document('bot')
        store = self.contract_store
        for value in (0, -1, True, 1.5, '2'):
            with self.subTest(value=value):
                document.set({'scenario_uri': 'old', 'generation': value})
                with self.assertRaises(StateStoreError):
                    store.get_global_bot_variables('bot')
                with self.assertRaises(StateStoreError):
                    store.save_global_bot_variables('bot', 'new')
                self.assertEqual('old', document.get().to_dict()['scenario_uri'])
        document.set({'scenario_uri': 'old', 'generation': None})
        with self.assertRaises(StateStoreError):
            store.save_global_bot_variables('bot', 'new')


if __name__ == '__main__':
    unittest.main()
