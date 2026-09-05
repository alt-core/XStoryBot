# coding: utf-8
"""LINE へ送る JSON（wire）が、実績のある旧実装と同じであることを固定する。

- golden: tests/fixtures/line/wire_golden.json は、旧 line-bot-sdk 2.4.3 を使っていた
  commit 899550d の plugin/line に同じ reaction を通した出力。fixture は直さず、差が出たら
  新コード側を直す。
- oracle: 開発環境に line-bot-sdk（v3）があれば、同じ message を v3 モデルで組んだ JSON とも
  比較する。LINE の仕様変更で v3 の出力が変わったときに差分として気づくための検査で、
  無ければ skip する。
"""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import commands
import common_commands
import hub
from plugin.line import api, default_commands, interface as line_interface
from plugin.line import messages as line_messages


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'line' / 'wire_golden.json'
OPTIONS = {'reset_keyword': '!reset', 'timezone': 'Asia/Tokyo'}


def _load_fixture():
    with open(FIXTURE, encoding='utf-8') as handle:
        return json.load(handle)


class _LinePluginTestCase(unittest.TestCase):
    """実 hub／commands に LINE plugin を登録し、実 interface で response を組む。"""

    @classmethod
    def setUpClass(cls):
        cls.fixture = _load_fixture()
        meta = cls.fixture['_meta']
        cls.params = {
            **OPTIONS,
            'alt_text': meta['alt_text'],
            'sender_icon_urls': meta['sender_icon_urls'],
            'reply_fallback_message': meta['reply_fallback_message'],
            'line_access_token': 'token',
            'line_channel_secret': 'secret',
        }
        hub.clear()
        commands.clear()
        common_commands.setup(OPTIONS)
        default_commands.inner_load_plugin(cls.params)
        cls.interface = line_interface.LinePlugin_Interface('bot', cls.params)

    @classmethod
    def tearDownClass(cls):
        hub.clear()
        commands.clear()

    def _context(self, event=None):
        return SimpleNamespace(
            service_name='line', version=3, event=event,
            status=SimpleNamespace(action_token=self.fixture['_meta']['action_token']),
            source_type='user', source_id='U1',
            get_interface=lambda name: self.interface if name == 'line' else None)


class WireGoldenTest(_LinePluginTestCase):
    def test_全caseのmessageが旧実装の出力と一致する(self):
        for case in self.fixture['cases']:
            with self.subTest(case=case['name']):
                reactions = [(tuple(reaction), children) for reaction, children in case['reactions']]
                with patch.object(default_commands.logging, 'error'), \
                        patch.object(default_commands.logging, 'warning'):
                    messages = self.interface._construct_responses(self._context(), reactions)
                self.assertEqual(case['messages'], json.loads(json.dumps(messages)))

    def test_replyとpushのrequestが旧実装と一致する(self):
        posts = {}

        def post(url, headers=None, data=None, timeout=None):
            posts[url.replace(api.API_ENDPOINT, '')] = {
                'body': json.loads(data),
                'headers': {key: value for key, value in headers.items() if key != 'Authorization'},
            }
            return SimpleNamespace(status_code=200, headers={}, json=dict, text='')

        messages = self.interface._construct_responses(self._context(), [((None, 'こんにちは'), None)])
        with patch.object(api.requests, 'post', post):
            self.interface._reply_message(self._context(event={'type': 'message', 'replyToken': 'REPLY-TOKEN'}), messages)
            self.interface._reply_message(self._context(event=None), messages, retry_key='RETRY-KEY')

        for path, expected in self.fixture['requests'].items():
            with self.subTest(path=path):
                self.assertEqual(expected['body'], posts[path]['body'])
                # User-Agent（旧: line-bot-sdk-python/2.4.3）以外の header は同じ
                expected_headers = {k: v for k, v in expected['headers'].items() if k != 'User-Agent'}
                self.assertEqual(expected_headers, posts[path]['headers'])


def _v3_available():
    try:
        import linebot.v3.messaging  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_v3_available(), 'line-bot-sdk v3（開発用 oracle）が無い')
class V3OracleTest(unittest.TestCase):
    """同じ message を公式 SDK v3 のモデルで組み、こちらの builder と JSON が一致することを確かめる。"""

    @classmethod
    def setUpClass(cls):
        from linebot.v3 import messaging
        cls.v3 = messaging

    def _same(self, ours, model):
        self.assertEqual(json.loads(model.to_json()), json.loads(json.dumps(ours)))

    def test_text_with_sender_and_quick_reply(self):
        v3 = self.v3
        ours = line_messages.text('本文', sender=line_messages.sender('案内人', 'https://example.test/icon.png'))
        ours['quickReply'] = line_messages.quick_reply([
            line_messages.message_action('A', 'A'),
            line_messages.postback_action('D', '#d@@AAAAAAAA', display_text='D'),
            line_messages.postback_action('F', '*f@@AAAAAAAA'),
            line_messages.uri_action('C', 'https://example.test/c'),
        ])
        self._same(ours, v3.TextMessage(
            text='本文',
            sender=v3.Sender(name='案内人', icon_url='https://example.test/icon.png'),
            quick_reply=v3.QuickReply(items=[
                v3.QuickReplyItem(action=v3.MessageAction(label='A', text='A')),
                v3.QuickReplyItem(action=v3.PostbackAction(label='D', data='#d@@AAAAAAAA', display_text='D')),
                v3.QuickReplyItem(action=v3.PostbackAction(label='F', data='*f@@AAAAAAAA')),
                v3.QuickReplyItem(action=v3.URIAction(label='C', uri='https://example.test/c')),
            ])))

    def test_media_messages(self):
        v3 = self.v3
        self._same(
            line_messages.image('https://x/o.png', 'https://x/p.png'),
            v3.ImageMessage(original_content_url='https://x/o.png', preview_image_url='https://x/p.png'))
        self._same(
            line_messages.video('https://x/v.mp4', 'https://x/t.png', tracking_id='v1.x'),
            v3.VideoMessage(original_content_url='https://x/v.mp4', preview_image_url='https://x/t.png', tracking_id='v1.x'))
        self._same(
            line_messages.audio('https://x/a.m4a', 12345),
            v3.AudioMessage(original_content_url='https://x/a.m4a', duration=12345))

    def test_template_messages(self):
        v3 = self.v3
        actions = [line_messages.message_action('はい', 'はい'), line_messages.postback_action('いいえ', '#no', display_text='いいえ')]
        v3_actions = [v3.MessageAction(label='はい', text='はい'), v3.PostbackAction(label='いいえ', data='#no', display_text='いいえ')]
        self._same(
            line_messages.template('alt', line_messages.confirm_template('いいですか？', actions)),
            v3.TemplateMessage(alt_text='alt', template=v3.ConfirmTemplate(text='いいですか？', actions=v3_actions)))
        self._same(
            line_messages.template('alt', line_messages.buttons_template('本文', actions, title='題', thumbnail_image_url='https://x/t.png')),
            v3.TemplateMessage(alt_text='alt', template=v3.ButtonsTemplate(
                text='本文', actions=v3_actions, title='題', thumbnail_image_url='https://x/t.png')))
        self._same(
            line_messages.template('alt', line_messages.carousel_template([
                line_messages.carousel_column('p1', actions, title='t1', thumbnail_image_url='https://x/p1.png')])),
            v3.TemplateMessage(alt_text='alt', template=v3.CarouselTemplate(columns=[
                v3.CarouselColumn(text='p1', actions=v3_actions, title='t1', thumbnail_image_url='https://x/p1.png')])))

    def test_imagemap_and_flex(self):
        v3 = self.v3
        area = line_messages.imagemap_area(0, 0, 520, 520)
        self._same(
            line_messages.imagemap('https://x/map', 'alt', 1040, 520, [
                line_messages.imagemap_message_action('ひだり', area),
                line_messages.imagemap_uri_action('https://example.test/r', line_messages.imagemap_area(520, 0, 520, 520))]),
            v3.ImagemapMessage(base_url='https://x/map', alt_text='alt', base_size=v3.ImagemapBaseSize(width=1040, height=520), actions=[
                v3.MessageImagemapAction(text='ひだり', area=v3.ImagemapArea(x=0, y=0, width=520, height=520)),
                v3.URIImagemapAction(link_uri='https://example.test/r', area=v3.ImagemapArea(x=520, y=0, width=520, height=520))]))
        bubble = {'type': 'bubble', 'body': {'type': 'box', 'layout': 'vertical', 'contents': [{'type': 'text', 'text': '本文'}]}}
        self._same(
            line_messages.flex('alt', bubble),
            v3.FlexMessage(alt_text='alt', contents=v3.FlexContainer.from_dict(bubble)))


if __name__ == '__main__':
    unittest.main()
