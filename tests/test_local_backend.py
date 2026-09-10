"""外部サービスなしでローカル保存の契約と再接続を確認する。"""

import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from cloud_backend.contracts import (
    CredentialData, CredentialSourceError, InvalidObjectReferenceError,
    ObjectNotFoundError, ObjectStoreError, StateConflictError, TaskQueueError,
)
from cloud_backend.local.credential_source import LocalCredentialSource
from cloud_backend.local.object_store import LocalObjectStore
from cloud_backend.local.state_store import LocalStateStore
from cloud_backend.local.task_queue import LocalTaskQueue
from tests.cloud_backend.state_store_contract import StateStoreContractMixin


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_BASE = 'http://127.0.0.1:8765/local-media'


class LocalStateStoreTest(StateStoreContractMixin, unittest.TestCase):
    def create_contract_store(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return LocalStateStore(directory.name)

    def test_再接続でも型を保持し取得値の変更は保存まで反映しない(self):
        data = {'bytes': b'\x00\xff', 'tuple': (1, 2), 'nested': {'items': [1]},
                'time': datetime.datetime.now(datetime.timezone.utc)}
        store = self.contract_store
        store.force_put_player_status('player', data)
        reopened = LocalStateStore(store.storage_root)
        loaded = reopened.load_player_status('player')
        self.assertEqual(data, loaded.data)
        loaded.data['nested']['items'].append(2)
        self.assertEqual(data, store.load_player_status('player').data)

    def test_別processから保存済みPlayerを再開できる(self):
        store = self.contract_store
        store.force_put_player_status('player', {'scene': '開始'})
        process = subprocess.run(
            [sys.executable, '-B', '-c', '''
import sys
from cloud_backend.local.state_store import LocalStateStore
store = LocalStateStore(sys.argv[1])
state = store.load_player_status('player')
assert state.data == {'scene': '開始'}
store.update_player_status('player', {'scene': '続き'}, state.version)
''', str(store.storage_root)], cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=20)
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertEqual({'scene': '続き'}, store.load_player_status('player').data)

    def test_二接続の競合と削除後再作成は古いversionを拒否する(self):
        store = self.contract_store
        other = LocalStateStore(store.storage_root)
        version = store.create_player_status('player', {'value': 1})
        other.update_player_status('player', {'value': 2}, version)
        with self.assertRaises(StateConflictError):
            store.update_player_status('player', {'value': 3}, version)
        store.delete_player_status('player')
        recreated = other.create_player_status('player', {'value': 4})
        self.assertNotEqual(version, recreated)
        with self.assertRaises(StateConflictError):
            store.update_player_status('player', {'value': 5}, version)
        self.assertEqual({'value': 4}, store.load_player_status('player').data)

    def test_Task更新失敗はrollbackし次の更新を妨げない(self):
        store = self.contract_store
        store.create_group_message_task('task', {'bot_name': 'bot', 'count': 1})

        def fail(data):
            data['count'] = 99
            raise ValueError('更新失敗')

        with self.assertRaises(ValueError):
            store.update_group_message_task('task', fail)
        self.assertEqual(1, store.get_group_message_task('task')['count'])
        store.update_group_message_task('task', lambda data: {'count': data['count'] + 1})
        self.assertEqual(2, store.get_group_message_task('task')['count'])

    def test_別processのread_modify_writeは更新を失わない(self):
        store = self.contract_store
        store.create_group_message_task('task', {'bot_name': 'bot', 'count': 0})
        program = '''
import sys
from cloud_backend.local.state_store import LocalStateStore
store = LocalStateStore(sys.argv[1])
for _ in range(20):
    store.update_group_message_task('task', lambda data: {'count': data['count'] + 1})
'''
        processes = [subprocess.Popen(
            [sys.executable, '-B', '-c', program, str(store.storage_root)],
            cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)]
        try:
            for process in processes:
                output, error = process.communicate(timeout=20)
                self.assertEqual(0, process.returncode, error)
                self.assertEqual('', output)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual(40, store.get_group_message_task('task')['count'])

    def test_cache期限はawareと端末時刻のnaive双方を判定する(self):
        store = self.contract_store
        for expiration in (datetime.datetime(2026, 9, 10, 12),
                           datetime.datetime(2026, 9, 10, 12, tzinfo=datetime.timezone.utc)):
            with self.subTest(expiration=expiration):
                store.set_build_cache('cache', b'value', expiration)
                with patch('cloud_backend.local.state_store.time.time', return_value=expiration.timestamp() - 1):
                    self.assertEqual(b'value', store.get_build_cache('cache'))
                with patch('cloud_backend.local.state_store.time.time', return_value=expiration.timestamp()):
                    self.assertIsNone(store.get_build_cache('cache'))
        store.set_build_cache('cache', b'new')
        self.assertEqual(b'new', store.get_build_cache('cache'))

    def test_DBをsymlinkへすり替えた場合は開かない(self):
        store = self.contract_store
        store.database_path.unlink()
        store.database_path.symlink_to(store.storage_root / 'outside.sqlite3')
        with self.assertRaises(InvalidObjectReferenceError):
            store.get_global_bot_variables('bot')


class LocalObjectStoreTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = LocalObjectStore(self.root / 'storage', PUBLIC_BASE)

    def test_Scenarioは固定URIで再読込し他rootや改変を拒否する(self):
        body = b'scenario-pickle'
        key = 'scenario/' + hashlib.md5(body).hexdigest()
        uri = self.store.store_scenario(key, body)
        reopened = LocalObjectStore(self.store.storage_root, PUBLIC_BASE)
        self.assertEqual(f'local://{self.store.store_id}/{key}', uri)
        self.assertEqual(body, reopened.load_scenario(uri))
        other = LocalObjectStore(self.root / 'other', PUBLIC_BASE)
        for invalid in (uri.replace(self.store.store_id, other.store_id),
                        'file:///tmp/scenario', 'https://example.invalid/scenario', uri + '?query'):
            with self.subTest(invalid=invalid), self.assertRaises(InvalidObjectReferenceError):
                self.store.load_scenario(invalid)
        with self.assertRaises(ObjectStoreError):
            self.store.store_scenario(key, b'wrong')
        (self.store.storage_root / key).write_bytes(b'wrong')
        with self.assertRaises(ObjectStoreError):
            self.store.load_scenario(uri)

    def test_公開と非公開を分け拡張子なし媒体のMIMEを再読込する(self):
        key = 'imagemap/digest.png/1040'
        url = self.store.store_public(key, b'png', 'image/png')
        self.store.store_private(key, 'private-value')
        reopened = LocalObjectStore(self.store.storage_root, PUBLIC_BASE)
        path, mime = reopened.public_file(key)
        self.assertEqual(b'png', path.read_bytes())
        self.assertEqual('image/png', mime)
        self.assertEqual(f'{PUBLIC_BASE}/{self.store.store_id}/{key}', url)
        self.assertEqual(b'private-value', reopened.load_private(key))
        for key in ('private/' + key, 'state.sqlite3', 'metadata/store.json'):
            with self.assertRaises(ObjectNotFoundError):
                reopened.public_file(key)

    def test_公開本文とMIMEの片方だけでは配信しない(self):
        self.store.store_public('image', b'png', 'image/png')
        self.store._mime_path('image').unlink()
        with self.assertRaises(ObjectNotFoundError):
            self.store.public_file('image')
        self.store.store_public('image', b'png', 'image/png')
        (self.store.storage_root / 'public/image').unlink()
        with self.assertRaises(ObjectNotFoundError):
            self.store.public_file('image')

    def test_危険なkeyと領域を跨ぐsymlinkを読み書きで拒否する(self):
        for key in ('/absolute', '../private/key', 'a/../key', 'a//key',
                    'a\\key', '.', '', 'key\x00'):
            for operation in (self.store.public_url, self.store.public_file, self.store.load_private,
                              lambda key: self.store.store_private(key, b'value')):
                with self.subTest(key=key, operation=operation), self.assertRaises(InvalidObjectReferenceError):
                    operation(key)
        (self.store.storage_root / 'public/inside').symlink_to(self.store.storage_root / 'private')
        (self.store.storage_root / 'private/outside').symlink_to(self.root)
        for operation in (lambda: self.store.public_file('inside/value'),
                          lambda: self.store.store_public('inside/value', b'value', 'text/plain'),
                          lambda: self.store.load_private('outside/value'),
                          lambda: self.store.store_private('outside/value', b'value')):
            with self.assertRaises(InvalidObjectReferenceError):
                operation()

    def test_URL変更と未対応metadataを拒否する(self):
        with self.assertRaisesRegex(ObjectStoreError, 'public_base_url'):
            LocalObjectStore(self.store.storage_root, 'http://127.0.0.1:9876/local-media')
        path = self.store.storage_root / 'metadata/store.json'
        metadata = json.loads(path.read_text())
        metadata['schema_version'] = 2
        path.write_text(json.dumps(metadata))
        with self.assertRaises(ObjectStoreError):
            LocalObjectStore(self.store.storage_root, PUBLIC_BASE)

    def test_公開URLは固定loopback形式だけを許可する(self):
        for url in ('http://localhost:8765/local-media', 'http://0.0.0.0:8765/local-media',
                    'https://127.0.0.1:8765/local-media', 'http://127.0.0.1/local-media',
                    PUBLIC_BASE + '/', PUBLIC_BASE + '?', 'http://user@127.0.0.1:8765/local-media'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LocalObjectStore(self.root / 'bad', url)


class LocalBoundariesTest(unittest.TestCase):
    def test_内部State保存先だけを分けてObjectStoreを共有できる(self):
        from cloud_backend.local import create_object_store, create_state_store
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            local = {'storage_root': str(root / 'objects'), 'public_base_url': PUBLIC_BASE}
            settings = types.SimpleNamespace(BACKEND_SETTINGS=local)
            with patch.dict(sys.modules, {'settings': settings}):
                common = create_state_store()
                common.put_image_text_stat('text', {'url': 'shared-image'})
                objects = create_object_store()
                local['state_storage_root'] = str(root / 'case-a')
                first = create_state_store()
                first.create_player_status('player', {'value': 'a'})
                first.set_next_label('player', 'next', '続き')
                local['state_storage_root'] = str(root / 'case-b')
                second = create_state_store()
                self.assertIsNone(second.load_player_status('player'))
                self.assertEqual((None, None), second.get_next_label('player'))
                self.assertIsNone(second.get_image_text_stat('text'))
                self.assertIsNone(common.load_player_status('player'))
                self.assertEqual({'url': 'shared-image'}, common.get_image_text_stat('text'))
                self.assertEqual(objects.store_id, create_object_store().store_id)
                self.assertEqual(root / 'objects', objects.storage_root)
                self.assertEqual(root / 'case-a', first.storage_root)
                self.assertEqual(root / 'case-b', second.storage_root)

    def test_実settingsとfactoryからSDKなしでlocal保存を生成できる(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'*': {'cloud': {'provider': 'local'}, 'auth': {}, 'bots': {},
                            'local': {'storage_root': str(Path(directory) / 'storage'),
                                      'public_base_url': PUBLIC_BASE}}}
            settings_path = Path(directory) / 'input.yaml'
            settings_path.write_text(json.dumps(config), encoding='utf-8')
            environment = dict(os.environ, XSBOT_SETTINGS_FILE=str(settings_path),
                               XSBOT_CLOUD_PROVIDER='local', XSBOT_DEPLOY_ENV='')
            process = subprocess.run([sys.executable, '-B', '-c', '''
import sys
class RejectCloudSDK:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('boto3', 'botocore', 'google.cloud', 'google.auth')):
            raise AssertionError('クラウドSDKを必要としません')
sys.meta_path.insert(0, RejectCloudSDK())
import settings
from cloud_backend import create_state_store, create_object_store, create_task_queue, create_credential_source
state = create_state_store()
state.save_global_bot_variables('bot', 'local-reference')
assert create_state_store().get_global_bot_variables('bot') == {'scenario_uri': 'local-reference'}
objects = create_object_store()
assert objects.storage_root == state.storage_root
assert create_task_queue().initialize({}) is None
assert create_credential_source().get_google_service_account('/synthetic/account.json').file_path == '/synthetic/account.json'
'''], cwd=PROJECT_ROOT, env=environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, process.returncode, process.stderr)

    def test_TaskQueueは登録を成功扱いしない(self):
        queue = LocalTaskQueue()
        self.assertIsNone(queue.initialize({}))
        with self.assertRaises(TaskQueueError):
            queue.create_task('action-queue', '/action', {})

    def test_資格情報は明示環境変数とファイルだけを使う(self):
        source = LocalCredentialSource({'admin_auth_json_env': 'AUTH'}, {'AUTH': '{"users":[]}'})
        self.assertEqual('{"users":[]}', source.get_admin_auth_json())
        self.assertEqual(CredentialData(file_path='/synthetic/account.json'),
                         source.get_google_service_account('/synthetic/account.json'))
        for reference in (None, '', {}, '{"key":"value"}', CredentialData(use_default=True)):
            with self.subTest(reference=reference), self.assertRaises(CredentialSourceError):
                source.get_google_service_account(reference, allow_default=True)
        with self.assertRaises(CredentialSourceError):
            LocalCredentialSource(environ={}).get_admin_auth_json()


if __name__ == '__main__':
    unittest.main()
