import unittest
from unittest.mock import Mock, call, patch

from google.cloud import exceptions

from cloud_backend.contracts import (
    InvalidObjectReferenceError,
    ObjectNotFoundError,
    ObjectStoreError,
)
from cloud_backend import gcp as gcp_backend
from cloud_backend.gcp import object_store as object_store_module
from cloud_backend.gcp.object_store import GcpObjectStore
from tests.cloud_backend.object_store_contract import ObjectStoreContractMixin


GCP_SETTINGS = {
    'credentials_path': '/unused/test-credentials.json',
    'project_id': 'test-project',
    'storage_bucket': 'trusted-bucket',
}


class _MemoryGcsBlob:
    def __init__(self, objects, bucket_name, key):
        self._objects = objects
        self._identity = (bucket_name, key)

    def upload_from_string(self, data, content_type=None):
        del content_type
        if isinstance(data, str):
            data = data.encode('utf-8')
        self._objects[self._identity] = bytes(data)

    def download_as_bytes(self):
        if self._identity not in self._objects:
            raise exceptions.NotFound('missing')
        return self._objects[self._identity]

    def make_public(self):
        pass


class _MemoryGcsBucket:
    def __init__(self, objects, name):
        self._objects = objects
        self.name = name

    def blob(self, key):
        return _MemoryGcsBlob(self._objects, self.name, key)


class _MemoryGcsClient:
    def __init__(self, objects):
        self._objects = objects

    def bucket(self, name):
        return _MemoryGcsBucket(self._objects, name)


class GcpObjectStoreContractTest(ObjectStoreContractMixin, unittest.TestCase):
    def create_contract_store(self):
        objects = {}
        client = _MemoryGcsClient(objects)
        return GcpObjectStore(
            GCP_SETTINGS,
            client=client,
            private_client_factory=lambda: client,
        )

    def foreign_scenario_reference(self, key):
        return f'https://storage.googleapis.com/foreign-bucket/{key}'


class GcpObjectStoreTest(unittest.TestCase):
    def setUp(self):
        self.public_client = Mock()
        self.public_bucket = Mock()
        self.public_bucket.name = 'trusted-bucket'
        self.public_client.bucket.return_value = self.public_bucket

        self.private_client = Mock()
        self.private_bucket = Mock()
        self.private_client.bucket.return_value = self.private_bucket
        self.private_client_factory = Mock(return_value=self.private_client)

        self.store = GcpObjectStore(
            GCP_SETTINGS,
            client=self.public_client,
            private_client_factory=self.private_client_factory,
        )

    def test_明示credential共有clientとADC都度clientを分ける(self):
        credentials = object()
        public_client = Mock()
        private_client = Mock()
        private_bucket = Mock()
        private_blob = Mock()
        private_client.bucket.return_value = private_bucket
        private_bucket.blob.return_value = private_blob

        with (
            patch.object(
                object_store_module.service_account.Credentials,
                'from_service_account_file',
                return_value=credentials,
            ) as from_file,
            patch.object(
                object_store_module.storage,
                'Client',
                side_effect=[public_client, private_client],
            ) as client_constructor,
        ):
            store = GcpObjectStore(GCP_SETTINGS)
            reference = store.store_private('group_tasks/task/members.json', '[]')

        from_file.assert_called_once_with('/unused/test-credentials.json')
        self.assertEqual(
            client_constructor.call_args_list,
            [
                call(project='test-project', credentials=credentials),
                call(),
            ],
        )
        public_client.bucket.assert_not_called()
        private_client.bucket.assert_called_once_with('trusted-bucket')
        private_bucket.blob.assert_called_once_with(
            'group_tasks/task/members.json')
        private_blob.upload_from_string.assert_called_once_with('[]')
        self.assertEqual(
            reference,
            'gs://trusted-bucket/group_tasks/task/members.json',
        )

    def test_明示credentialのGoogle認証例外を共通例外へ変換する(self):
        google_auth_error = type(
            'DefaultCredentialsError',
            (Exception,),
            {'__module__': 'google.auth.exceptions'},
        )
        with (
            patch.object(
                object_store_module.service_account.Credentials,
                'from_service_account_file',
                side_effect=google_auth_error('credentials unavailable'),
            ),
            self.assertRaises(ObjectStoreError),
        ):
            GcpObjectStore(GCP_SETTINGS)

    def test_provider内ではScenarioとgroupが同じObjectStoreを共有する(self):
        object_store = Mock()
        original = gcp_backend._object_store
        gcp_backend._object_store = None
        try:
            with patch.object(
                    object_store_module, 'GcpObjectStore',
                    return_value=object_store) as constructor:
                first = gcp_backend.create_object_store()
                second = gcp_backend.create_object_store()
        finally:
            gcp_backend._object_store = original

        self.assertIs(first, object_store)
        self.assertIs(second, object_store)
        constructor.assert_called_once_with()

    def test_privateはbucket未設定を操作時まで遅延して検出する(self):
        settings_without_bucket = dict(GCP_SETTINGS, storage_bucket='')
        store = GcpObjectStore(
            settings_without_bucket,
            client=self.public_client,
            private_client_factory=self.private_client_factory,
        )

        with self.assertRaisesRegex(
                ValueError, 'Storage bucket not configured'):
            store.load_private('group_tasks/task/members.json')

        self.private_client_factory.assert_not_called()

    def test_scenarioはMD5keyを公開しGCS_HTTPS参照を返す(self):
        blob = Mock()
        self.public_bucket.blob.return_value = blob

        reference = self.store.store_scenario(
            'scenario/0123456789abcdef', b'pickle-data')

        self.public_client.bucket.assert_called_once_with('trusted-bucket')
        self.public_bucket.blob.assert_called_once_with(
            'scenario/0123456789abcdef')
        blob.upload_from_string.assert_called_once_with(
            b'pickle-data', content_type='application/octet-stream')
        blob.make_public.assert_called_once_with()
        self.private_client_factory.assert_not_called()
        self.assertEqual(
            reference,
            'https://storage.googleapis.com/'
            'trusted-bucket/scenario/0123456789abcdef',
        )

    def test_scenarioは同bucketならprefixを追加検査せず読み込む(self):
        blob = Mock()
        blob.download_as_bytes.return_value = b'pickle-data'
        self.public_bucket.blob.return_value = blob

        data = self.store.load_scenario(
            'https://storage.googleapis.com/trusted-bucket/legacy/object')

        self.assertEqual(data, b'pickle-data')
        self.public_client.bucket.assert_called_once_with('trusted-bucket')
        self.public_bucket.blob.assert_called_once_with('legacy/object')
        blob.download_as_bytes.assert_called_once_with()

    def test_scenarioは異なるbucketとGCS以外をdownload前に拒否する(self):
        invalid_references = (
            'https://storage.googleapis.com/foreign-bucket/scenario/object',
            'gs://trusted-bucket/scenario/object',
            'https://example.com/scenario/object',
        )

        for reference in invalid_references:
            with self.subTest(reference=reference):
                with self.assertRaises(InvalidObjectReferenceError):
                    self.store.load_scenario(reference)

        self.public_client.bucket.assert_not_called()

    def test_public_mediaはcontent_typeとmake_publicを維持する(self):
        cases = (
            ('image/digest_1024.png', b'image', 'image/png'),
            ('imagemap/digest.jpg/1040', b'imagemap', 'image/jpeg'),
            ('video/digest.mp4', b'video', 'video/mp4'),
        )

        for key, data, content_type in cases:
            with self.subTest(key=key):
                blob = Mock()
                self.public_bucket.blob.return_value = blob

                url = self.store.store_public(key, data, content_type)

                self.public_bucket.blob.assert_called_with(key)
                blob.upload_from_string.assert_called_once_with(
                    data, content_type=content_type)
                blob.make_public.assert_called_once_with()
                self.assertEqual(
                    url,
                    f'https://storage.googleapis.com/trusted-bucket/{key}',
                )

        self.private_client_factory.assert_not_called()

    def test_public_urlは存在確認せずkeyから組み立てる(self):
        url = self.store.public_url('imagemap/digest.png')

        self.assertEqual(
            url,
            'https://storage.googleapis.com/trusted-bucket/imagemap/digest.png',
        )
        self.public_client.bucket.assert_not_called()
        self.private_client_factory.assert_not_called()

    def test_privateは非公開でcontent_type未指定を維持する(self):
        blob = Mock()
        self.private_bucket.blob.return_value = blob

        reference = self.store.store_private(
            'group_tasks/task/members.json', '["user"]')

        self.private_client_factory.assert_called_once_with()
        self.private_client.bucket.assert_called_once_with('trusted-bucket')
        self.private_bucket.blob.assert_called_once_with(
            'group_tasks/task/members.json')
        blob.upload_from_string.assert_called_once_with('["user"]')
        blob.make_public.assert_not_called()
        self.public_client.bucket.assert_not_called()
        self.assertEqual(
            reference,
            'gs://trusted-bucket/group_tasks/task/members.json',
        )

    def test_privateは操作ごとにADC_clientを生成する(self):
        first_client = Mock()
        first_bucket = Mock()
        first_client.bucket.return_value = first_bucket
        first_bucket.blob.return_value = Mock()
        second_client = Mock()
        second_bucket = Mock()
        second_blob = Mock()
        second_blob.download_as_bytes.return_value = b'[]'
        second_client.bucket.return_value = second_bucket
        second_bucket.blob.return_value = second_blob
        private_client_factory = Mock(
            side_effect=[first_client, second_client])
        store = GcpObjectStore(
            GCP_SETTINGS,
            client=self.public_client,
            private_client_factory=private_client_factory,
        )

        store.store_private('group_tasks/task/members.json', '[]')
        data = store.load_private('group_tasks/task/members.json')

        self.assertEqual(data, b'[]')
        self.assertEqual(private_client_factory.call_count, 2)
        first_client.bucket.assert_called_once_with('trusted-bucket')
        second_client.bucket.assert_called_once_with('trusted-bucket')

    def test_NotFoundとその他GCS例外を共通例外へ変換する(self):
        blob = Mock()
        self.private_bucket.blob.return_value = blob
        blob.download_as_bytes.side_effect = exceptions.NotFound('missing')

        with self.assertRaises(ObjectNotFoundError):
            self.store.load_private('group_tasks/task/members.json')

        blob.download_as_bytes.side_effect = exceptions.GoogleCloudError('failed')
        with self.assertRaises(ObjectStoreError):
            self.store.load_private('group_tasks/task/members.json')

    def test_ADCのGoogle認証例外を共通例外へ変換する(self):
        google_auth_error = type(
            'DefaultCredentialsError',
            (Exception,),
            {'__module__': 'google.auth.exceptions'},
        )
        private_client_factory = Mock(
            side_effect=google_auth_error('credentials unavailable'))
        store = GcpObjectStore(
            GCP_SETTINGS,
            client=self.public_client,
            private_client_factory=private_client_factory,
        )

        with self.assertRaises(ObjectStoreError):
            store.load_private('group_tasks/task/members.json')

    def test_アプリケーション例外は型を変えない(self):
        error = RuntimeError('application error')
        private_client_factory = Mock(side_effect=error)
        store = GcpObjectStore(
            GCP_SETTINGS,
            client=self.public_client,
            private_client_factory=private_client_factory,
        )

        with self.assertRaises(RuntimeError) as raised:
            store.load_private('group_tasks/task/members.json')

        self.assertIs(raised.exception, error)

    def test_upload失敗時はmake_publicしない(self):
        blob = Mock()
        blob.upload_from_string.side_effect = exceptions.GoogleCloudError(
            'upload failed')
        self.public_bucket.blob.return_value = blob

        with self.assertRaises(ObjectStoreError):
            self.store.store_scenario('scenario/digest', b'pickle-data')

        blob.make_public.assert_not_called()


if __name__ == '__main__':
    unittest.main()
