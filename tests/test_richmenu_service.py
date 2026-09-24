"""反映の部分失敗・再実行を人工APIとメモリ上のObjectStoreで検証する。"""
import io
import json
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from cloud_backend.contracts import ObjectNotFoundError
from plugin.line.api import LineApiError, LineApiClient
from richmenu_service import RichmenuService, load_record, build_menu_ids
from tests.test_richmenu_spec import menu_settings


class Store:
    def __init__(self):
        self.values = {}
        self.loads = 0

    def load_private(self, key):
        self.loads += 1
        if key not in self.values:
            raise ObjectNotFoundError('not found')
        return self.values[key]

    def store_private(self, key, data, content_type=None):
        self.values[key] = data


class RichmenuServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image = Image.new('RGB', (800, 400), 'white')
        out = io.BytesIO(); image.save(out, format='PNG')
        cls.image = out.getvalue()

    def setUp(self):
        self.store = Store()
        self.api = Mock()
        self.api.create_rich_menu.return_value = 'richmenu-' + 'a' * 32
        self.api.get_rich_menu.return_value = {'name': 'exists'}
        self.api.get_default_rich_menu_id.return_value = None
        self.bot = types.SimpleNamespace(name='bot', get_interface=lambda service: types.SimpleNamespace(api=self.api) if service == 'line' else None)
        self.source = menu_settings()
        self.service = RichmenuService(self.bot, self.source, {'menu_url': 'https://pages.example.test/'}, self.store)
        response = Mock(status_code=200)
        response.iter_content.return_value = [self.image]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        patcher = patch('richmenu_service._ImageSession.get', return_value=response)
        self.get = patcher.start(); self.addCleanup(patcher.stop)

    def test_反映と再実行と再作成と既定設定(self):
        self.assertEqual('create', self.service.plan('main')['status'])
        self.assertEqual({}, self.store.values)
        with self.assertLogs(level='INFO') as logs:
            result = self.service.apply('main')
        logged = json.loads(logs.records[-1].getMessage())
        self.assertEqual('richmenu-' + 'a' * 32, logged['richmenu_id'])
        self.assertEqual(64, len(logged['hash']))
        self.assertEqual('create', result['status'])
        self.api.create_rich_menu.assert_called_once()
        self.assertEqual('unchanged', self.service.apply('main')['status'])
        self.api.get_rich_menu.return_value = None
        self.assertEqual('recreate', self.service.plan('main')['status'])
        self.api.get_rich_menu.return_value = {}
        self.api.get_rich_menu.return_value = {'name': 'exists'}
        self.api.get_default_rich_menu_id.side_effect = LineApiError(403, 'other channel')
        self.assertTrue(self.service.default_plan()['external'])
        self.service.apply_default()
        self.api.set_default_rich_menu.assert_called_once()
        self.assertEqual('main', load_record(self.store, 'bot')['default'])

    def test_upload失敗は未記録で再実行できる(self):
        self.api.upload_rich_menu_image.side_effect = LineApiError(500, 'failure')
        with self.assertRaises(LineApiError):
            self.service.apply('main')
        self.assertEqual({}, self.store.values)
        self.api.upload_rich_menu_image.side_effect = None
        self.service.apply('main')
        self.assertEqual(2, self.api.create_rich_menu.call_count)

    def test_二件目の失敗後も一件目を再作成しない(self):
        self.service.menus.menus['extra'] = self.service.menus.menus['main']
        self.api.create_rich_menu.side_effect = ['richmenu-' + char * 32 for char in 'abc']
        self.api.upload_rich_menu_image.side_effect = [None, LineApiError(500, 'failure'), None]
        self.service.apply('main')
        with self.assertRaises(LineApiError):
            self.service.apply('extra')
        self.assertEqual({'main'}, set(load_record(self.store, 'bot')['menus']))
        self.assertEqual('unchanged', self.service.apply('main')['status'])
        self.service.apply('extra')
        self.assertEqual({'main', 'extra'}, set(load_record(self.store, 'bot')['menus']))
        self.assertEqual(3, self.api.create_rich_menu.call_count)

    def test_画像取得でnetrcの認証情報を送らない(self):
        import requests
        from richmenu_service import _ImageSession
        with _ImageSession() as session, patch('requests.sessions.get_netrc_auth', side_effect=AssertionError()):
            prepared = session.prepare_request(requests.Request('GET', 'https://media.example.test/image'))
            self.assertNotIn('Authorization', prepared.headers)
            prepared.headers['Authorization'] = 'artificial'
            session.rebuild_auth(prepared, Mock())
            self.assertNotIn('Authorization', prepared.headers)

    def test_画像不正と破損した記録ではLINEへ書き込まない(self):
        self.get.return_value.iter_content.return_value = [b'x' * (1024 * 1024 + 1)]
        with self.assertRaises(ValueError):
            self.service.apply('main')
        self.api.create_rich_menu.assert_not_called()
        self.get.return_value.iter_content.return_value = [self.image]
        self.store.values['richmenu/bot.json'] = b'{}'
        with self.assertRaises(ValueError):
            self.service.plan('main')
        self.api.get_rich_menu.assert_not_called()

    def test_ビルドの読込みは一回でlocalは不要(self):
        self.service.apply('main'); self.store.loads = 0
        with patch('richmenu_service.get_provider', return_value='aws'), patch('richmenu_service.create_object_store', return_value=self.store):
            result = build_menu_ids(self.bot, {'main'})
        self.assertEqual('richmenu-' + 'a' * 32, result['main'])
        self.assertEqual(1, self.store.loads)
        with patch('richmenu_service.get_provider', return_value='local'), patch('richmenu_service.create_object_store', side_effect=AssertionError()):
            self.assertEqual({'main': 'xsb-local-main'}, build_menu_ids(self.bot, {'main'}))

    def test_LINE管理APIの送信先と404(self):
        api = LineApiClient('artificial', endpoint='https://api.example.test', data_endpoint='https://data.example.test')
        response = Mock(status_code=200, content=b'{}', headers={})
        response.json.return_value = {'richMenuId': 'id'}
        with patch('plugin.line.api.requests.request', return_value=response) as request:
            self.assertEqual('id', api.create_rich_menu({'name': 'menu'}))
            api.upload_rich_menu_image('id', b'png', 'image/png')
            self.assertEqual('https://data.example.test/v2/bot/richmenu/id/content', request.call_args.args[1])
            self.assertEqual('image/png', request.call_args.kwargs['headers']['Content-Type'])
            response.status_code = 404
            self.assertIsNone(api.get_rich_menu('id'))
            self.assertIsNone(api.get_default_rich_menu_id())
