"""メニューの共通定義と定数の解釈を、外部I/Oなしで確認する。"""
import copy
import unittest

from richmenu_spec import parse_bot_settings, content_digest, resolve_line_id, referenced_constants
from utility import normalize_constants


def menu_settings():
    return {'default_richmenu': 'main', 'richmenus': {'main': {
        'image': 'https://media.example.test/menu.png',
        'size': {'width': 800, 'height': 400}, 'chatBarText': 'メニュー',
        'areas': [
            {'bounds': {'x': 0, 'y': 0, 'width': 400, 'height': 400},
             'action': {'type': 'postback', 'data': '#help', 'displayText': 'ヘルプ'}},
            {'bounds': {'x': 400, 'y': 0, 'width': 400, 'height': 400},
             'action': {'type': 'uri', 'uri': '{menu_url}'}}
        ],
    }}}


class RichmenuSpecTest(unittest.TestCase):
    def test_一段の展開とhashとLINEへの送信形式(self):
        source = menu_settings()
        before = copy.deepcopy(source)
        parsed = parse_bot_settings(source, {'menu_url': 'https://pages.example.test/{literal}'})
        menu = parsed.menus['main']
        self.assertEqual(before, source)
        self.assertEqual({'menu_url'}, referenced_constants(source['richmenus']))
        self.assertFalse(menu.data['selected'])
        self.assertEqual('https://pages.example.test/{literal}', menu.data['areas'][1]['action']['uri'])
        digest = content_digest(menu, b'image')
        self.assertEqual(digest, content_digest(menu, b'image'))
        self.assertNotEqual(digest, content_digest(menu, b'changed'))
        line = menu.line_object('bot', 'main', digest)
        self.assertNotIn('image', line)
        self.assertTrue(line['name'].startswith('xsb:bot:main:'))
        self.assertEqual('id', resolve_line_id('ＭＡＩＮ', {'main': 'id'}))
        self.assertEqual('richmenu-old', resolve_line_id('richmenu-old', {}))

    def test_生ID以外の未定義名はAPIに渡さず拒否する(self):
        for value in ('missing', '', 'RICHMENU-old', 'richmenu_old'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '解決できません'):
                resolve_line_id(value, {'main': 'richmenu-known'})
        self.assertEqual({'menu'}, referenced_constants({'text': ['{ＭＥＮＵ}', '{{literal}}', None]}))

    def test_定数名の正規化と層内の衝突(self):
        self.assertEqual({'menu': 'value'}, normalize_constants({'ＭＥＮＵ': 'value'}))
        with self.assertRaises(ValueError):
            normalize_constants({'Menu': 1, 'menu': 1})
        with self.assertLogs(level='WARNING') as captured:
            normalize_constants({'blank': ''})
        self.assertIn('blank', captured.output[0])
        with self.assertRaises(ValueError):
            normalize_constants({'array': []}, scalar_only=True)

    def test_不正な定義と書式を拒否する(self):
        for field, value in [('size', {'width': True, 'height': 400}),
                             ('size', {'width': 2501, 'height': 400}),
                             ('size', {'width': 800, 'height': 600}),
                             ('selected', 'false'), ('chatBarText', 'x' * 15),
                             ('areas', []), ('image', 'http://example.test/a'),
                             ('chatBarText', '{menu_url!s}'), ('chatBarText', '{menu_url.x}'),
                             ('chatBarText', '{missing}')]:
            source = menu_settings()
            source['richmenus']['main'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                parse_bot_settings(source, {'menu_url': 'https://pages.example.test/'})
        for name in ('Main', '_name', 'richmenu-test'):
            source = menu_settings()
            source['richmenus'][name] = source['richmenus'].pop('main')
            with self.assertRaises(ValueError):
                parse_bot_settings(source, {})

    def test_領域の変更でWebchatのrevisionが変わりdataを公開しない(self):
        source = menu_settings()
        first = parse_bot_settings(source, {'menu_url': 'https://pages.example.test/'}).menus['main']
        a = first.webchat_spec('main', first.data['image'])
        source['richmenus']['main']['areas'][0]['action']['data'] = '#different'
        second = parse_bot_settings(source, {'menu_url': 'https://pages.example.test/'}).menus['main']
        b = second.webchat_spec('main', second.data['image'])
        self.assertNotEqual(a['revision'], b['revision'])
        self.assertNotIn('data', a['areas'][0]['action'])
