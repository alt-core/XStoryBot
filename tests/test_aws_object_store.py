import hashlib
from io import BytesIO
import sys
import types
import unittest
from unittest.mock import Mock, call, patch

from botocore.exceptions import ClientError, NoCredentialsError

from cloud_backend import aws as aws_backend
from cloud_backend.aws.object_store import AwsObjectStore
from cloud_backend.contracts import (
    InvalidObjectReferenceError,
    ObjectNotFoundError,
    ObjectStoreError,
)
from tests.cloud_backend.object_store_contract import ObjectStoreContractMixin


AWS_SETTINGS = {
    'region': 'test-region-1',
    'object_store': {
        'private_bucket': 'private-bucket',
        'media_bucket': 'media-bucket',
        'public_media_base_url': 'https://example.cloudfront.net',
    },
}


class _MemoryS3Client:
    def __init__(self):
        self._objects = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        del ContentType
        self._objects[(Bucket, Key)] = bytes(Body)
        return {}

    def get_object(self, Bucket, Key):
        identity = (Bucket, Key)
        if identity not in self._objects:
            raise ClientError(
                {'Error': {'Code': 'NoSuchKey', 'Message': 'missing'}},
                'GetObject',
            )
        return {'Body': BytesIO(self._objects[identity])}


class AwsObjectStoreContractTest(ObjectStoreContractMixin, unittest.TestCase):
    def create_contract_store(self):
        return AwsObjectStore(AWS_SETTINGS, client=_MemoryS3Client())

    def foreign_scenario_reference(self, key):
        return f's3://foreign-bucket/{key}'


class AwsObjectStoreTest(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.store = AwsObjectStore(AWS_SETTINGS, client=self.client)

    def test_clientは最初のS3操作まで生成しない(self):
        client = Mock()
        client_factory = Mock(return_value=client)
        store = AwsObjectStore(AWS_SETTINGS, client_factory=client_factory)

        self.assertEqual(
            'https://example.cloudfront.net/image/a%20b.png',
            store.public_url('image/a b.png'),
        )
        client_factory.assert_not_called()

        client.get_object.return_value = {'Body': BytesIO(b'[]')}
        self.assertEqual(b'[]', store.load_private('group_tasks/task/members.json'))
        client_factory.assert_called_once_with(
            's3', region_name='test-region-1')

    def test_provider内ではObjectStoreを共有する(self):
        object_store = Mock()
        original = aws_backend._object_store
        aws_backend._object_store = None
        try:
            with (
                patch.dict(
                    sys.modules,
                    {'settings': types.SimpleNamespace(
                        BACKEND_SETTINGS=AWS_SETTINGS)},
                ),
                patch(
                    'cloud_backend.aws.object_store.AwsObjectStore',
                    return_value=object_store,
                ) as constructor,
            ):
                first = aws_backend.create_object_store()
                second = aws_backend.create_object_store()
        finally:
            aws_backend._object_store = original

        self.assertIs(first, object_store)
        self.assertIs(second, object_store)
        constructor.assert_called_once_with(AWS_SETTINGS)

    def test_bucketと公開URL設定を検証する(self):
        invalid_settings = (
            {'private_bucket': '', 'media_bucket': 'media',
             'public_media_base_url': 'https://example.cloudfront.net'},
            {'private_bucket': 'private', 'media_bucket': '',
             'public_media_base_url': 'https://example.cloudfront.net'},
            {'private_bucket': 'same', 'media_bucket': 'same',
             'public_media_base_url': 'https://example.cloudfront.net'},
            {'private_bucket': 'private', 'media_bucket': 'media',
             'public_media_base_url': 'http://example.cloudfront.net'},
            {'private_bucket': 'private', 'media_bucket': 'media',
             'public_media_base_url': 'https://example.cloudfront.net?'},
            {'private_bucket': 'private', 'media_bucket': 'media',
             'public_media_base_url': 'https://example.cloudfront.net#'},
            {'private_bucket': 'private', 'media_bucket': 'media',
             'public_media_base_url': 'https://example.cloudfront.net/?token=x'},
        )

        for object_settings in invalid_settings:
            with self.subTest(object_settings=object_settings):
                with self.assertRaises(ValueError):
                    AwsObjectStore({'object_store': object_settings}, client=Mock())

    def test_scenarioを非公開保存して読み戻し検証後に参照を返す(self):
        data = b'pickle-data'
        digest = hashlib.md5(data).hexdigest()
        key = f'scenario/{digest}'
        self.client.get_object.return_value = {'Body': BytesIO(data)}

        reference = self.store.store_scenario(key, data)

        self.assertEqual(f's3://private-bucket/{key}', reference)
        self.assertEqual(
            self.client.method_calls,
            [
                call.put_object(
                    Bucket='private-bucket',
                    Key=key,
                    Body=data,
                    ContentType='application/octet-stream',
                ),
                call.get_object(Bucket='private-bucket', Key=key),
            ],
        )

    def test_scenarioのkeyと保存内容が違えばS3へ書かない(self):
        with self.assertRaises(ObjectStoreError):
            self.store.store_scenario(
                'scenario/00000000000000000000000000000000',
                b'pickle-data',
            )

        self.client.put_object.assert_not_called()

    def test_scenarioの保存後検証と読込時検証を行う(self):
        data = b'pickle-data'
        digest = hashlib.md5(data).hexdigest()
        key = f'scenario/{digest}'
        reference = f's3://private-bucket/{key}'
        self.client.get_object.return_value = {'Body': BytesIO(b'changed')}

        with self.assertRaises(ObjectStoreError):
            self.store.store_scenario(key, data)
        with self.assertRaises(ObjectStoreError):
            self.store.load_scenario(reference)

    def test_scenarioは別bucketと不正keyをS3呼出前に拒否する(self):
        invalid_references = (
            'https://example.com/scenario/00000000000000000000000000000000',
            's3://other-bucket/scenario/00000000000000000000000000000000',
            's3://private-bucket/group_tasks/task/members.json',
            's3://private-bucket/scenario/not-a-digest',
        )

        for reference in invalid_references:
            with self.subTest(reference=reference):
                with self.assertRaises(InvalidObjectReferenceError):
                    self.store.load_scenario(reference)

        self.client.get_object.assert_not_called()

    def test_public_mediaはACLなしで保存しCloudFront_URLを返す(self):
        url = self.store.store_public(
            'imagemap/digest image.png/1040', b'image', 'image/png')

        self.client.put_object.assert_called_once_with(
            Bucket='media-bucket',
            Key='imagemap/digest image.png/1040',
            Body=b'image',
            ContentType='image/png',
        )
        self.assertNotIn('ACL', self.client.put_object.call_args.kwargs)
        self.assertEqual(
            'https://example.cloudfront.net/imagemap/digest%20image.png/1040',
            url,
        )

    def test_private文字列をUTF8で保存しopaque参照を返す(self):
        reference = self.store.store_private(
            'group_tasks/task/members.json', '["ユーザー"]')

        self.client.put_object.assert_called_once_with(
            Bucket='private-bucket',
            Key='group_tasks/task/members.json',
            Body='["ユーザー"]'.encode('utf-8'),
        )
        self.assertEqual(
            's3://private-bucket/group_tasks/task/members.json', reference)

    def test_private読込はbytesを返す(self):
        self.client.get_object.return_value = {'Body': BytesIO('値'.encode('utf-8'))}

        result = self.store.load_private('group_tasks/task/members.json')

        self.assertEqual('値'.encode('utf-8'), result)

    def test_not_foundだけをObjectNotFoundへ変換する(self):
        self.client.get_object.side_effect = ClientError(
            {'Error': {'Code': 'NoSuchKey', 'Message': 'missing'}},
            'GetObject',
        )

        with self.assertRaises(ObjectNotFoundError):
            self.store.load_private('missing')

        self.client.put_object.side_effect = ClientError(
            {'Error': {'Code': 'NoSuchBucket', 'Message': 'missing'}},
            'PutObject',
        )
        with self.assertRaises(ObjectStoreError):
            self.store.store_private('key', b'value')

    def test_AWS_SDK例外だけを共通例外へ変換する(self):
        self.client.put_object.side_effect = NoCredentialsError()
        with self.assertRaises(ObjectStoreError):
            self.store.store_private('key', b'value')

        application_error = RuntimeError('application error')
        self.client.put_object.side_effect = application_error
        with self.assertRaises(RuntimeError) as raised:
            self.store.store_private('key', b'value')
        self.assertIs(application_error, raised.exception)


if __name__ == '__main__':
    unittest.main()
