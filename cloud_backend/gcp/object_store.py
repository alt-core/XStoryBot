"""Google Cloud Storageを利用するObjectStore実装。"""

import logging
import re

from google.cloud import exceptions, storage
from google.oauth2 import service_account

from cloud_backend.contracts import (
    InvalidObjectReferenceError,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
)


class GcpObjectStore(ObjectStore):
    """既存のGCSキー、ACL、URI形式を維持する。"""

    def __init__(
            self, gcp_settings=None, client=None,
            private_client_factory=None):
        if gcp_settings is None:
            import settings
            gcp_settings = settings.GCP_SETTINGS

        self._bucket_name = gcp_settings['storage_bucket']
        if client is None:
            credentials = service_account.Credentials.from_service_account_file(
                gcp_settings['credentials_path']
            )
            client = storage.Client(
                project=gcp_settings['project_id'],
                credentials=credentials,
            )
        self._client = client
        self._private_client_factory = (
            private_client_factory or storage.Client)

    @staticmethod
    def _raise_store_error(error):
        if isinstance(error, exceptions.NotFound):
            raise ObjectNotFoundError(str(error)) from error
        raise ObjectStoreError(str(error)) from error

    def _bucket(self):
        return self._client.bucket(self._bucket_name)

    def _private_bucket(self):
        # group JSONは従来どおり、操作ごとにADCのClientを生成する。
        if not self._bucket_name:
            raise ValueError('Storage bucket not configured')
        return self._private_client_factory().bucket(self._bucket_name)

    def store_scenario(self, key, data):
        try:
            bucket = self._bucket()
            blob = bucket.blob(key)
            blob.upload_from_string(
                data,
                content_type='application/octet-stream',
            )
            # GCP版の既存シナリオ参照との互換性を維持する。
            blob.make_public()
            return f'https://storage.googleapis.com/{bucket.name}/{key}'
        except exceptions.GoogleCloudError as error:
            self._raise_store_error(error)

    def load_scenario(self, reference):
        match = re.match(
            r'^https://storage.googleapis.com/([^/]+)/(.+)$', reference)
        if not match:
            raise InvalidObjectReferenceError(
                'CloudStorage のファイルではありません')

        bucket_name = match.group(1)
        key = match.group(2)
        if bucket_name != self._bucket_name:
            raise InvalidObjectReferenceError(
                '設定済みのCloud Storage bucketではありません')

        try:
            logging.info(f'load scenario file: {reference}')
            bucket = self._client.bucket(bucket_name)
            blob = bucket.blob(key)
            return blob.download_as_bytes()
        except exceptions.GoogleCloudError as error:
            self._raise_store_error(error)

    def store_public(self, key, data, content_type):
        try:
            blob = self._bucket().blob(key)
            blob.upload_from_string(data, content_type=content_type)
            blob.make_public()
            return self.public_url(key)
        except exceptions.GoogleCloudError as error:
            self._raise_store_error(error)

    def public_url(self, key):
        return f'https://storage.googleapis.com/{self._bucket_name}/{key}'

    def store_private(self, key, data, content_type=None):
        try:
            blob = self._private_bucket().blob(key)
            if content_type is None:
                blob.upload_from_string(data)
            else:
                blob.upload_from_string(data, content_type=content_type)
            return f'gs://{self._bucket_name}/{key}'
        except exceptions.GoogleCloudError as error:
            self._raise_store_error(error)

    def load_private(self, key):
        try:
            return self._private_bucket().blob(key).download_as_bytes()
        except exceptions.GoogleCloudError as error:
            self._raise_store_error(error)
