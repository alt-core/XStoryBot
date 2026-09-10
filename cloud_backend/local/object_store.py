"""Scenarioと媒体をローカルの公開・非公開領域へ分けて保存する。"""

import hashlib
import json
import re
from urllib.parse import quote, urlsplit
import uuid

from cloud_backend.contracts import (
    InvalidObjectReferenceError, ObjectNotFoundError, ObjectStore, ObjectStoreError,
)
from cloud_backend.local.storage import atomic_write, checked_path, prepare_root


class LocalObjectStore(ObjectStore):
    def __init__(self, storage_root, public_base_url):
        parsed = urlsplit(public_base_url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or parsed.port is None or not 1 <= parsed.port <= 65535
                or public_base_url != f'http://127.0.0.1:{parsed.port}/local-media'):
            raise ValueError('local.public_base_urlはhttp://127.0.0.1:<port>/local-mediaで指定してください')
        self.storage_root = prepare_root(storage_root)
        self.public_base_url = public_base_url
        for area in ('metadata', 'scenario', 'public', 'private'):
            checked_path(self.storage_root, area).mkdir(exist_ok=True, mode=0o700)
        metadata_path = checked_path(self.storage_root, 'metadata/store.json')
        metadata = {
            'schema_version': 1,
            'store_id': uuid.uuid4().hex,
            'public_base_url': public_base_url,
        }
        try:
            atomic_write(metadata_path, json.dumps(metadata), exclusive=True)
        except FileExistsError:
            pass
        try:
            metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise ObjectStoreError('ローカル保存のmetadataを読み込めません') from error
        if (not isinstance(metadata, dict)
                or set(metadata) != {'schema_version', 'store_id', 'public_base_url'}
                or type(metadata['schema_version']) is not int
                or metadata['schema_version'] != 1
                or not isinstance(metadata['store_id'], str)
                or not re.fullmatch('[0-9a-f]{32}', metadata['store_id'])):
            raise ObjectStoreError('ローカル保存のmetadata形式に対応していません')
        if metadata['public_base_url'] != public_base_url:
            raise ObjectStoreError('同じ保存rootのpublic_base_urlは変更できません')
        self.store_id = metadata['store_id']

    @staticmethod
    def _scenario_key(key):
        if not isinstance(key, str) or not re.fullmatch('scenario/[0-9a-f]{32}', key):
            raise InvalidObjectReferenceError('ローカルScenarioのkeyが不正です')
        return key.rsplit('/', 1)[1]

    def _path(self, area, key):
        if not isinstance(key, str) or not key:
            raise InvalidObjectReferenceError('ローカル保存のkeyが不正です')
        return checked_path(self.storage_root, f'{area}/{key}')

    @staticmethod
    def _read(path):
        try:
            return path.read_bytes()
        except FileNotFoundError as error:
            raise ObjectNotFoundError('ローカル保存ファイルがありません') from error
        except OSError as error:
            raise ObjectStoreError('ローカル保存ファイルを読み込めません') from error

    @staticmethod
    def _write(path, data):
        try:
            atomic_write(path, data)
        except OSError as error:
            raise ObjectStoreError('ローカル保存ファイルを書き込めません') from error

    def store_scenario(self, key, data):
        digest = self._scenario_key(key)
        if hashlib.md5(data).hexdigest() != digest:
            raise ObjectStoreError('Scenario内容とkeyのダイジェストが一致しません')
        self._write(checked_path(self.storage_root, key), data)
        return f'local://{self.store_id}/{key}'

    def load_scenario(self, reference):
        if not isinstance(reference, str):
            raise InvalidObjectReferenceError('ローカルScenarioの参照が不正です')
        prefix = f'local://{self.store_id}/'
        if not reference.startswith(prefix):
            raise InvalidObjectReferenceError('この保存rootのScenarioではありません')
        key = reference[len(prefix):]
        digest = self._scenario_key(key)
        data = self._read(checked_path(self.storage_root, key))
        if hashlib.md5(data).hexdigest() != digest:
            raise ObjectStoreError('読み込んだScenarioの検証に失敗しました')
        return data

    def public_url(self, key):
        self._path('public', key)
        return f'{self.public_base_url}/{self.store_id}/{quote(key, safe="/")}'

    def _mime_path(self, key):
        digest = hashlib.sha256(key.encode('utf-8')).hexdigest()
        return checked_path(self.storage_root, f'metadata/content-types/{digest}.json')

    @staticmethod
    def _content_type(value):
        if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
            raise ObjectStoreError('公開媒体のContent-Typeが不正です')
        return value

    def store_public(self, key, data, content_type):
        path = self._path('public', key)
        content_type = self._content_type(content_type)
        self._write(path, data)
        self._write(self._mime_path(key), json.dumps({'content_type': content_type}))
        return self.public_url(key)

    def public_file(self, key):
        path = self._path('public', key)
        if not path.is_file():
            raise ObjectNotFoundError('公開媒体がありません')
        try:
            metadata = json.loads(self._read(self._mime_path(key)))
            content_type = self._content_type(metadata['content_type'])
        except (ValueError, TypeError, KeyError) as error:
            raise ObjectStoreError('公開媒体のContent-Typeを読み込めません') from error
        return path, content_type

    def store_private(self, key, data, content_type=None):
        self._write(self._path('private', key), data)
        return f'local://{self.store_id}/private/{quote(key, safe="/")}'

    def load_private(self, key):
        return self._read(self._path('private', key))
