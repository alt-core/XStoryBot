"""実Webchatへ渡すローカル設定と公開媒体のHTTP境界を確認する。"""

import copy
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from bottle import Bottle

from cloud_backend import factory
from cloud_backend.local.object_store import LocalObjectStore
from plugin.webchat.errors import BotNotWebCompatible, InvalidWebchatConfiguration
from plugin.webchat.interface import WebchatInterface
from tests.test_api_endpoints import TestApp
from tools import local_webchat


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:8765/local-media'
ORIGIN = 'http://127.0.0.1:8765'
HOST = '127.0.0.1:8765'


class LocalWebchatTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = LocalObjectStore(self.root, BASE)
        data = b'artificial scenario'
        self.uri = self.store.store_scenario('scenario/' + hashlib.md5(data).hexdigest(), data)
        self.media_url = self.store.store_public('imagemap/test.png/1040', b'png-data', 'image/png')
        self.config = {
            'cloud': {'provider': 'local'},
            'local': {'storage_root': str(self.root), 'public_base_url': BASE},
            'auth': {'api_token': 'unused-token'},
            'options': {'reset_keyword': '!reset', 'timezone': 'UTC'},
            'plugins': {
                'webchat': {'signing_key': 'unused-production-key'},
                'line': {'line_access_token': 'unused-line-token'},
                'line.quick_reply': {'command': ['＞'], 'default_reply': '次へ',
                                    'please_select_quick_reply_label': '##please'},
            },
            'bots': {
                'bot': {'interfaces': [{'type': 'line'}, {'type': 'webchat', 'params': {
                    'scenario_compatibility_epoch': 'epoch-1', 'start_action': '##line.follow',
                }}]},
                'other': {'interfaces': [{'type': 'webchat'}]},
            },
        }

    def _prepare(self, assets=None):
        return local_webchat.prepare_settings(self.config, 'bot', self.uri, assets or {})

    def _interface(self, prepared):
        params = prepared['bots']['bot']['interfaces'][0]['params']
        with patch.object(factory, '_provider', 'local'):
            return WebchatInterface('bot', params, local_settings=prepared['local'])

    def test_設定を変更せず選択Botと完成URIだけを渡し鍵を再利用する(self):
        original = copy.deepcopy(self.config)
        with patch.dict(sys.modules, {'settings': None}):
            first = self._prepare()
            second = self._prepare()
        self.assertEqual(original, self.config)
        self.assertEqual(['bot'], list(first['bots']))
        self.assertEqual({}, first['auth'])
        self.assertNotIn('line', first['plugins'])
        params = first['bots']['bot']['interfaces'][0]['params']
        self.assertTrue(params['enabled'])
        self.assertEqual(self.uri, params['scenario_uri'])
        self.assertEqual(self.store.store_id, params['deployment'])
        self.assertEqual('epoch-1', params['scenario_compatibility_epoch'])
        self.assertEqual([ORIGIN], params['allowed_origins'])
        self.assertEqual([], params['external_http_origins'])
        self.assertNotEqual('unused-production-key', params['signing_key'])
        self.assertEqual(params['signing_key'], second['bots']['bot']['interfaces'][0]['params']['signing_key'])

    def test_既存の不正鍵を勝手に上書きしない(self):
        key = self.root / 'private/webchat-signing-key'
        key.write_text('invalid-key', encoding='ascii')
        with self.assertRaises(InvalidWebchatConfiguration):
            self._prepare()
        self.assertEqual('invalid-key', key.read_text(encoding='ascii'))

    def test_宣言媒体は符号化表記が異なっても初期化と出力で解決する(self):
        original_url = 'https://assets.example/音声 file.png'
        encoded_url = 'https://assets.example/%E9%9F%B3%E5%A3%B0%20file.png'
        self.config['plugins']['webchat']['sender_icon_urls'] = {'案内人': original_url}
        prepared = self._prepare({original_url: self.media_url})
        self.assertEqual({encoded_url: self.media_url}, prepared['local']['webchat_asset_urls'])
        interface = self._interface(prepared)
        self.assertEqual(self.media_url, interface.validate_media_url(original_url))
        self.assertEqual(self.media_url, interface.validate_media_url(encoded_url))
        self.assertEqual(self.media_url, interface._presenter._sender('案内人')['icon_url'])
        with self.assertRaisesRegex(ValueError, '重複'):
            self._prepare({original_url: self.media_url, encoded_url: self.media_url})

    def test_HTTP例外はlocalの自rootだけで外部媒体は明示許可する(self):
        prepared = self._prepare()
        interface = self._interface(prepared)
        self.assertTrue(interface.origin_allowed(ORIGIN))
        self.assertFalse(interface.origin_allowed('http://localhost:8765'))
        self.assertEqual(self.media_url, interface.validate_media_url(self.media_url))
        for url in ('https://assets.example/unknown.png',
                    'http://127.0.0.1:8765/local-media/other/image.png',
                    'http://127.0.0.1:8766/local-media/image.png',
                    f'{BASE}/{self.store.store_id}/%2e%2e/private/key'):
            with self.subTest(url=url), self.assertRaises(BotNotWebCompatible):
                interface.validate_media_url(url)
        prepared['local']['allow_external_media'] = True
        params = prepared['bots']['bot']['interfaces'][0]['params']
        params['media_origins'] = ['https://assets.example']
        interface = self._interface(prepared)
        self.assertEqual('https://assets.example/a.png', interface.validate_media_url('https://assets.example/a.png'))
        with self.assertRaises(BotNotWebCompatible):
            interface.validate_media_url('https://other.example/a.png')
        for provider in ('gcp', 'aws'):
            with patch.object(factory, '_provider', provider), self.assertRaises(InvalidWebchatConfiguration):
                WebchatInterface('bot', params, local_settings=prepared['local'])
        with self.assertRaises(InvalidWebchatConfiguration):
            WebchatInterface('bot', params)

    def test_媒体はOriginなしでも読めHEADとRangeを使えprivateは読めない(self):
        app = Bottle()
        app.get('/chat/bot')(lambda: 'UI')
        client = TestApp(local_webchat.create_app(app, self.store))
        headers = {'Host': HOST}
        path = f'/local-media/{self.store.store_id}/imagemap/test.png/1040'
        response = client.get(path, headers=headers)
        self.assertEqual(b'png-data', response.body)
        self.assertEqual('image/png', response.headers['Content-Type'])
        self.assertEqual('nosniff', response.headers['X-Content-Type-Options'])
        self.assertEqual(b'', client.request('HEAD', path, headers=headers).body)
        partial = client.get(path, headers={**headers, 'Range': 'bytes=0-2'})
        self.assertEqual(206, partial.status_int)
        self.assertEqual(b'png', partial.body)
        for path in (f'/local-media/{self.store.store_id}/../private/key',
                     f'/local-media/{self.store.store_id}/state.sqlite3',
                     '/local-media/other/imagemap/test.png/1040', '/private/webchat-signing-key'):
            with self.subTest(path=path):
                self.assertEqual(404, client.get(path, headers=headers, expect_errors=True).status_int)
        for headers in ({'Host': 'localhost:8765'}, {'Host': 'evil.example'},
                        {'Host': HOST, 'Origin': 'https://evil.example'}):
            self.assertEqual(403, client.get('/chat/bot', headers=headers, expect_errors=True).status_int)

    def test_実app_webchatでCSPとAPIのOrigin検査を維持する(self):
        prepared = self._prepare()
        snapshot = self.root / 'serve-settings.yaml'
        snapshot.write_text(json.dumps({'*': prepared}), encoding='utf-8')
        script = '''
import json
import settings
import app_webchat
from cloud_backend.local.object_store import LocalObjectStore
from tests.test_api_endpoints import TestApp
from tools.local_webchat import create_app
local = settings.BACKEND_SETTINGS
store = LocalObjectStore(local['storage_root'], local['public_base_url'])
client = TestApp(create_app(app_webchat.app, store))
host = {'Host': '127.0.0.1:8765'}
page = client.get('/chat/bot', headers=host)
options = client.request('OPTIONS', '/api/webchat/v1/bots/bot/turn',
                        headers={**host, 'Origin': 'http://127.0.0.1:8765'})
rejected = client.post_json('/api/webchat/v1/bots/bot/turn', {},
                           headers=host, expect_errors=True)
print(json.dumps({'page': page.status_int, 'csp': page.headers['Content-Security-Policy'],
                  'options': options.status_int, 'without_origin': rejected.status_int}))
'''
        result = subprocess.run([sys.executable, '-B', '-c', script], cwd=PROJECT_ROOT,
                                env={**os.environ, 'XSBOT_CLOUD_PROVIDER': 'local',
                                     'XSBOT_SETTINGS_FILE': str(snapshot), 'XSBOT_DEPLOY_ENV': 'test'},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        actual = json.loads(result.stdout)
        self.assertEqual((200, 204, 403), (actual['page'], actual['options'], actual['without_origin']))
        for directive in ('img-src', 'media-src'):
            self.assertEqual("'self'", actual['csp'].split(directive, 1)[1].split(';', 1)[0].strip())

    def test_環境未確定ではsettingsを読み込まず停止する(self):
        with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {'settings': None}):
            with self.assertRaises(ValueError):
                local_webchat.run_server()

    def test_起動失敗は短い診断と終了2を返しCtrlCは正常終了する(self):
        for error in (OSError(errno.EADDRINUSE, '人工の非公開情報'),
                      ValueError('人工の非公開情報')):
            with self.subTest(error=type(error).__name__):
                stderr, stdout = io.StringIO(), io.StringIO()
                with patch.object(local_webchat, 'run_server', side_effect=error), \
                        patch('sys.stderr', stderr), patch('sys.stdout', stdout):
                    self.assertEqual(2, local_webchat.main())
                self.assertIn('ローカルWebchatを起動できません', stderr.getvalue())
                self.assertNotIn('人工の非公開情報', stderr.getvalue())
                self.assertNotIn('Traceback', stderr.getvalue())
                self.assertEqual('', stdout.getvalue())
        with patch.object(local_webchat, 'run_server', side_effect=KeyboardInterrupt):
            self.assertEqual(0, local_webchat.main())

    def test_起動通知後の終了要求は現在requestを完了してhandlerを戻す(self):
        ready = self.root / 'ready'
        completed = []
        server = Mock()
        previous = object()
        with patch.dict(os.environ, {'XSBOT_LOCAL_READY_FILE': str(ready)}), \
                patch.object(local_webchat.signal, 'signal', return_value=previous) as signal:
            def handle_request():
                self.assertEqual('ready', ready.read_text(encoding='ascii'))
                signal.call_args_list[0].args[1](local_webchat.signal.SIGTERM, None)
                completed.append(True)

            server.handle_request.side_effect = handle_request
            local_webchat._serve(server)
        self.assertEqual([True], completed)
        server.handle_request.assert_called_once_with()
        self.assertIs(previous, signal.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
