"""Amazon S3とCloudFrontを利用するObjectStore実装。"""

import hashlib
import re
from urllib.parse import quote, unquote, urlsplit

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from cloud_backend.contracts import (
    InvalidObjectReferenceError,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
)


class AwsObjectStore(ObjectStore):
    """非公開S3オブジェクトとCloudFront公開URLを扱う。"""

    _SCENARIO_KEY_PATTERN = re.compile(r'^scenario/[0-9a-f]{32}$')

    def __init__(self, aws_settings=None, client=None, client_factory=None):
        if aws_settings is None:
            import settings
            aws_settings = settings.BACKEND_SETTINGS

        object_settings = aws_settings.get('object_store', {})
        self._region = aws_settings.get('region') or None
        self._private_bucket = object_settings.get('private_bucket', '')
        self._media_bucket = object_settings.get('media_bucket', '')
        self._public_media_base_url = self._validate_settings(
            object_settings.get('public_media_base_url', ''))

        self._client_instance = client
        self._client_factory = client_factory or boto3.client

    def _validate_settings(self, public_media_base_url):
        if not self._private_bucket:
            raise ValueError('AWS private bucketが設定されていません')
        if not self._media_bucket:
            raise ValueError('AWS media bucketが設定されていません')
        if self._private_bucket == self._media_bucket:
            raise ValueError('AWS private bucketとmedia bucketは分けてください')

        parsed = urlsplit(public_media_base_url)
        if (
                parsed.scheme != 'https'
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or '?' in public_media_base_url
                or '#' in public_media_base_url):
            raise ValueError('AWS公開メディアURLにはqueryなしのHTTPS URLを指定してください')
        return public_media_base_url.rstrip('/')

    @staticmethod
    def _as_bytes(data):
        if isinstance(data, str):
            return data.encode('utf-8')
        if isinstance(data, bytearray):
            return bytes(data)
        return data

    @staticmethod
    def _raise_store_error(error, allow_not_found=False):
        if isinstance(error, ClientError):
            code = str(error.response.get('Error', {}).get('Code', ''))
            if allow_not_found and code in ('NoSuchKey', '404', 'NotFound'):
                raise ObjectNotFoundError(str(error)) from error
            raise ObjectStoreError(str(error)) from error
        if isinstance(error, BotoCoreError):
            raise ObjectStoreError(str(error)) from error
        if type(error).__module__.startswith(('boto3.', 'botocore.')):
            raise ObjectStoreError(str(error)) from error
        raise error

    def _client(self):
        if self._client_instance is None:
            try:
                options = {}
                if self._region is not None:
                    options['region_name'] = self._region
                self._client_instance = self._client_factory('s3', **options)
            except Exception as error:
                self._raise_store_error(error)
        return self._client_instance

    @staticmethod
    def _scenario_digest(key):
        if not AwsObjectStore._SCENARIO_KEY_PATTERN.fullmatch(key):
            raise InvalidObjectReferenceError('AWSのシナリオ参照が不正です')
        return key.rsplit('/', 1)[1]

    def _scenario_reference(self, key):
        return f's3://{self._private_bucket}/{quote(key, safe="/")}'

    def _parse_scenario_reference(self, reference):
        parsed = urlsplit(reference)
        if (
                parsed.scheme != 's3'
                or parsed.netloc != self._private_bucket
                or parsed.query
                or parsed.fragment):
            raise InvalidObjectReferenceError('AWSのシナリオ参照が不正です')
        key = unquote(parsed.path.lstrip('/'))
        self._scenario_digest(key)
        return key

    def _put_object(self, bucket, key, data, content_type=None):
        request = {
            'Bucket': bucket,
            'Key': key,
            'Body': self._as_bytes(data),
        }
        if content_type is not None:
            request['ContentType'] = content_type
        try:
            self._client().put_object(**request)
        except Exception as error:
            self._raise_store_error(error)

    def _get_object(self, bucket, key):
        try:
            response = self._client().get_object(Bucket=bucket, Key=key)
            body = response['Body']
            data = body.read() if hasattr(body, 'read') else body
            return self._as_bytes(data)
        except Exception as error:
            self._raise_store_error(error, allow_not_found=True)

    def store_scenario(self, key, data):
        expected_digest = self._scenario_digest(key)
        body = self._as_bytes(data)
        if hashlib.md5(body).hexdigest() != expected_digest:
            raise ObjectStoreError('シナリオ内容とキーのダイジェストが一致しません')

        self._put_object(
            self._private_bucket,
            key,
            body,
            content_type='application/octet-stream',
        )
        stored_data = self._get_object(self._private_bucket, key)
        if hashlib.md5(stored_data).hexdigest() != expected_digest:
            raise ObjectStoreError('保存したシナリオの検証に失敗しました')
        return self._scenario_reference(key)

    def load_scenario(self, reference):
        key = self._parse_scenario_reference(reference)
        expected_digest = self._scenario_digest(key)
        data = self._get_object(self._private_bucket, key)
        if hashlib.md5(data).hexdigest() != expected_digest:
            raise ObjectStoreError('読み込んだシナリオの検証に失敗しました')
        return data

    def store_public(self, key, data, content_type):
        self._put_object(self._media_bucket, key, data, content_type)
        return self.public_url(key)

    def public_url(self, key):
        return f'{self._public_media_base_url}/{quote(key, safe="/")}'

    def store_private(self, key, data, content_type=None):
        self._put_object(self._private_bucket, key, data, content_type)
        return f's3://{self._private_bucket}/{quote(key, safe="/")}'

    def load_private(self, key):
        return self._get_object(self._private_bucket, key)
