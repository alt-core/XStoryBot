# coding: utf-8
import unittest
import hmac
import hashlib
import base64
from pprint import pprint

from webtest import TestApp

import os, sys, subprocess, json
gcloud_info = json.loads(subprocess.check_output(['gcloud', 'info', '--format=json']))
sdk_path = os.path.join(gcloud_info["installation"]["sdk_root"], 'platform', 'google_appengine')
sys.path.append(sdk_path)
sys.path.append(os.path.join(sdk_path, 'lib', 'yaml', 'lib'))
sys.path.insert(0, './lib')
#sys.path.append(os.path.join(sdk_path, 'platform/google_appengine/lib'))

from google.appengine.ext import testbed
tb = testbed.Testbed()
tb.activate()
tb.init_datastore_v3_stub()
tb.init_memcache_stub()
#tb.deactivate()

from linebot.models import TextSendMessage

import settings
settings.STARTUP_LOAD_SHEET = False
import linecallback
from scenario import Scenario, ScenarioSyntaxError


class DummyLineBotApi(object):
    def __init__(self):
        self.reply_token = ""
        self.to = ""
        self.messages = []

    def reply_message(self, reply_token, messages):
        self.reply_token = reply_token
        if not isinstance(messages, (list, tuple)):
            messages = [messages]
        self.messages = messages

    def push_message(self, to, messages):
        self.to = to
        if not isinstance(messages, (list, tuple)):
            messages = [messages]
        self.messages = messages


def dummy_respond_message(action, flags, source):
    if 'prev_msg' in flags:
        msg = u'前回は' + flags['prev_msg'] + u'って言ってましたよね。'
    else:
        msg = u'覚えました！'
    flags['prev_msg'] = action
    return TextSendMessage(text=msg)


class LinebotTestCase(unittest.TestCase):

    def setUp(self):
        self.app = TestApp(linecallback.app)
        self.bot = DummyLineBotApi()
        self.test_bot = linecallback.LineBot('testbot', '', '00000000000000000000000000000000', '')
        self.test_bot.line_bot_api = self.bot
        self.bot2 = DummyLineBotApi()
        self.test_bot2 = linecallback.LineBot('testbot2', '', '00000000000000000000000000000000', '')
        self.test_bot2.line_bot_api = self.bot2
        linecallback.bot_dict = {'testbot': self.test_bot, 'testbot2': self.test_bot2}
        import bottle
        bottle.debug(True)

    def tearDown(self):
        pass

    def gen_signature(self, body):
        return base64.b64encode(hmac.new(
            self.test_bot.line_channel_secret,
            body.encode('utf-8'),
            hashlib.sha256
        ).digest())

    def send_reset(self):
        msg = u'リセット'
        data = u'{"events":[{"type":"message","replyToken":"ffffffffffffffffffffffffffffffff","source":{"userId":"Uffffffffffffffffffffffffffffffff","type":"user"},"timestamp":1478027519455,"message":{"type":"text","id":"1234567890123","text":"' + msg + u'"}}]}'
        sign = self.gen_signature(data)
        res = self.app.post('/line/callback/testbot', data, content_type='application/json', headers={'X-Line-Signature': sign})
        self.assertEqual(str(res), "Response: 200 OK\nContent-Type: text/html; charset=UTF-8\nOK")
        return res

    def send_message(self, msg):
        self.bot.messages = []
        data = u'{"events":[{"type":"message","replyToken":"ffffffffffffffffffffffffffffffff","source":{"userId":"Uffffffffffffffffffffffffffffffff","type":"user"},"timestamp":1478027519455,"message":{"type":"text","id":"1234567890123","text":"' + msg + u'"}}]}'
        sign = self.gen_signature(data)
        res = self.app.post('/line/callback/testbot', data, content_type='application/json', headers={'X-Line-Signature': sign})
        self.assertEqual(str(res), "Response: 200 OK\nContent-Type: text/html; charset=UTF-8\nOK")
        self.assertEqual(self.bot.reply_token, 'ffffffffffffffffffffffffffffffff')
        return res

    def send_postback(self, postback_data):
        self.bot.messages = []
        data = u'{"events":[{"type":"postback","replyToken":"ffffffffffffffffffffffffffffffff","source":{"userId":"Uffffffffffffffffffffffffffffffff","type":"user"},"timestamp":1478027519455,"postback":{"data":"' + postback_data + u'"}}]}'
        sign = self.gen_signature(data)
        res = self.app.post('/line/callback/testbot', data, content_type='application/json', headers={'X-Line-Signature': sign})
        self.assertEqual(str(res), "Response: 200 OK\nContent-Type: text/html; charset=UTF-8\nOK")
        self.assertEqual(self.bot.reply_token, 'ffffffffffffffffffffffffffffffff')
        return res

    def send_event(self, event_type):
        self.bot.messages = []
        data = u'{"events":[{"type":"' + event_type + u'","replyToken":"ffffffffffffffffffffffffffffffff","source":{"userId":"Uffffffffffffffffffffffffffffffff","type":"user"},"timestamp":1478027519455}]}'
        sign = self.gen_signature(data)
        res = self.app.post('/line/callback/testbot', data, content_type='application/json', headers={'X-Line-Signature': sign})
        self.assertEqual(str(res), "Response: 200 OK\nContent-Type: text/html; charset=UTF-8\nOK")
        self.assertEqual(self.bot.reply_token, 'ffffffffffffffffffffffffffffffff')
        return res

    def send_api_send(self, group, action):
        self.bot.messages = []
        res = self.app.get('/line/api/send/testbot?group='+group+'&action='+action)
        self.assertEqual(str(res), "Response: 200 OK\nContent-Type: text/html; charset=UTF-8\nOK")
        return res

    def reset_bot2(self):
        self.bot2.messages = []

    def test_flagdb(self):
        orig_respond_message = self.test_bot.respond_message
        self.test_bot.respond_message = dummy_respond_message
        self.send_message('test')
        self.assertEqual(self.bot.messages[0].text, u"覚えました！")
        self.send_message('test')
        self.assertEqual(self.bot.messages[0].text, u"前回はtestって言ってましたよね。")
        self.test_bot.respond_message = orig_respond_message

    def test_scenario(self):
        self.test_bot.scenario = Scenario.from_table([
            [u'test', u'てすと'],
            [u'', u'てすと？'],
            [u'test_or', u'@or'],
            [u'ｔｅｓｔ＿ｏｒ', u'＠ｏｒ'],
            [u'#', u'この行はコメントです。'],
            [u'dummy_or', u'or ok'],
            [u'#jump', u'jumped'],
            [u'jump_test', u'before jump'],
            [u'', u'#jump'],
            [u'image', u'=IMAGE("https://example.com/image.png")'],
            [u'confirm', u'@confirm', u'Ｃｏｎｆｉｒｍの説明文'],
            [u'', u'', u'選択肢１', u'テキストメッセージのアクション'],
            [u'', u'', u'選択肢２', u'http://example.com/action2'],
            [u'button', u'＠ボタン', u'ボタンの説明文', u'ボタンのタイトル', u'=IMAGE("https://example.com/image.png")'],
            [u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'選択肢２', u'選択肢２のアクション', u'＃label２'],
            [u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label３'],
            [u'', u'', u'選択肢４', u'選択肢４のアクション', u'#label4'],
            [u'#label1', u'選択肢１のリアクション'],
            [u'＃label２', u'選択肢２のリアクション'],
            [u'#label３', u'選択肢３のリアクション'],
            [u'＃label４', u'選択肢４のリアクション'],
            [u'action', u'@button', u'Action のテスト'],
            [u'', u'', u'https scheme', u'https://example.com/https'],
            [u'', u'', u'tel scheme', u'tel:0123456789'],
            [u'', u'', u'data only', u'', u'#data_only'],
            [u'', u'', u'data only2', u'#data_only2'],
            [u'more', u'＠続きを読む'],
            [u'', u'', u'メッセージ１の１', u'メッセージ１の２', u'メッセージ１の３'],
            [u'', u'', u'タイトル指定：'],
            [u'', u'', u'メッセージ２'],
            [u'', u'', u'メッセージ３'],
            [u'', u'', u'少年：'],
            [u'', u'', u'メッセージ４'],
            [u'more2', u'＠続きを読む', u'#続き'],
            [u'', u'', u'メッセージ１'],
            [u'#続き', u'続きのメッセージ'],
            [u'more3', u'＠続きを読む', u'#続き２'],
            [u'', u'', u'少女：'],
            [u'', u'', u'「メッセージ１」'],
            [u'', u'', u'少年：'],
            [u'', u'', u'「返事」'],
            [u'', u'', u'少女：'],
            [u'', u'', u'「メッセージ３」'],
            [u'', u'', u'少年：'],
            [u'', u'', u'「メッセージ４」'],
            [u'', u'', u'少年：'],
            [u'', u'', u'「メッセージ５」'],
            [u'#続き２', u'続きのメッセージ'],
            [u'panel', u'＠パネル'],
            [u'', u'', u'パネル１の説明', u'パネル１', u'=IMAGE("https://example.com/image.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'http://example.com/panel_action2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション'],
            [u'', u'', u'パネル２の説明', u'パネル２', u'=IMAGE("https://example.com/image.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'', u'', u'パネル３の説明', u'パネル３', u'=IMAGE("https://example.com/image.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'', u'', u'パネル４の説明', u'パネル４', u'=IMAGE("https://example.com/image.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'', u'', u'パネル５の説明', u'パネル５', u'=IMAGE("https://example.com/image.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'imagemap', u'@imagemap', u'=IMAGE("https://example.com/image.png")'],
            [u'', u'', u'0,0,1040,1040', u'イメージマップアクション１'],
            [u'', u'', u'100,200,300,400', u'http://example.com/imagemap_action'],
            [u'##follow', u'フォローしました。', u''],
            [u'', u'', u''],
            [u'', u'', u''],
            [u'/(.*)/', u'{0}'],
        ])
        self.send_reset()
        self.send_message(u'test')
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"てすと")
        self.assertEqual(self.bot.messages[1].text, u"てすと？")
        self.send_message(u'hoge')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"hoge")
        self.send_message(u'test_or')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"or ok")
        self.send_message(u'ｔｅｓｔ＿ｏｒ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"or ok")
        self.send_message(u'jump_test')
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"before jump")
        self.assertEqual(self.bot.messages[1].text, u"jumped")
        self.send_message(u'image')
        self.assertEqual(self.bot.messages[0].original_content_url, u"https://testbed.example.com/line/image/https%3A%2F%2Fexample.com%2Fimage.png")
        self.assertEqual(self.bot.messages[0].preview_image_url, u"https://testbed.example.com/line/image/https%3A%2F%2Fexample.com%2Fimage.png/preview")
        self.send_message(u'confirm')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.text, u"Ｃｏｎｆｉｒｍの説明文")
        self.assertEqual(len(self.bot.messages[0].template.actions), 2)
        self.assertEqual(self.bot.messages[0].template.actions[0].type, "message")
        self.assertEqual(self.bot.messages[0].template.actions[0].text, u"テキストメッセージのアクション")
        self.assertEqual(self.bot.messages[0].template.actions[1].type, "uri")
        self.assertEqual(self.bot.messages[0].template.actions[1].uri, u"http://example.com/action2")
        self.send_message(u'button')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.text, u"ボタンの説明文")
        actions = self.bot.messages[0].template.actions
        self.assertEqual(len(actions), 4)
        self.send_postback(actions[0].data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"選択肢１のリアクション")
        self.send_postback(actions[1].data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"選択肢２のリアクション")
        self.send_postback(actions[2].data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"選択肢３のリアクション")
        self.send_postback(actions[3].data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"選択肢４のリアクション")
        self.send_message(u'action')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].type, "template")
        self.assertEqual(self.bot.messages[0].template.text, u"Action のテスト")
        self.assertEqual(len(self.bot.messages[0].template.actions), 4)
        self.assertEqual(self.bot.messages[0].template.actions[0].type, "uri")
        self.assertEqual(self.bot.messages[0].template.actions[0].uri, u"https://example.com/https")
        self.assertEqual(self.bot.messages[0].template.actions[1].type, "uri")
        self.assertEqual(self.bot.messages[0].template.actions[1].uri, u"tel:0123456789")
        self.assertEqual(self.bot.messages[0].template.actions[2].type, "postback")
        self.assertEqual(self.bot.messages[0].template.actions[2].data.split(u'@@')[0], u"#data_only")
        self.assertIsNone(self.bot.messages[0].template.actions[2].text)
        self.assertEqual(self.bot.messages[0].template.actions[3].type, "postback")
        self.assertEqual(self.bot.messages[0].template.actions[3].data.split(u'@@')[0], u"#data_only2")
        self.assertIsNone(self.bot.messages[0].template.actions[3].text)
        self.send_message(u'more')
        self.assertEqual(len(self.bot.messages), 3)
        self.assertEqual(self.bot.messages[0].text, u"メッセージ１の１")
        self.assertEqual(self.bot.messages[1].text, u"メッセージ１の２")
        self.assertEqual(self.bot.messages[2].template.text, u"メッセージ１の３")
        self.send_postback(self.bot.messages[2].template.actions[0].data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.title, u"タイトル指定")
        self.assertEqual(self.bot.messages[0].template.text, u"メッセージ２\nメッセージ３")
        self.send_postback(self.bot.messages[0].template.actions[0].data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"メッセージ４")
        self.send_message(u'more2')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.text, u"メッセージ１")
        self.send_postback(self.bot.messages[0].template.actions[0].data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"続きのメッセージ")
        self.send_message(u'more3')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.title, u"少女")
        self.assertEqual(self.bot.messages[0].template.text, u"「メッセージ１」")
        self.assertEqual(self.bot.messages[0].template.actions[0].label, u'▽')
        self.send_postback(self.bot.messages[0].template.actions[0].data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.title, u"少年")
        self.assertEqual(self.bot.messages[0].template.text, u"「返事」")
        self.assertEqual(self.bot.messages[0].template.actions[0].label, u'▽')
        self.send_postback(self.bot.messages[0].template.actions[0].data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.title, u"少女")
        self.assertEqual(self.bot.messages[0].template.text, u"「メッセージ３」")
        self.assertEqual(self.bot.messages[0].template.actions[0].label, u'▽')
        self.send_postback(self.bot.messages[0].template.actions[0].data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.title, u"少年")
        self.assertEqual(self.bot.messages[0].template.text, u"「メッセージ４」")
        self.assertEqual(self.bot.messages[0].template.actions[0].label, u'▽')
        self.send_postback(self.bot.messages[0].template.actions[0].data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.title, u"少年")
        self.assertEqual(self.bot.messages[0].template.text, u"「メッセージ５」")
        self.assertEqual(self.bot.messages[0].template.actions[0].label, u'▽')
        self.send_postback(self.bot.messages[0].template.actions[0].data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"続きのメッセージ")
        self.send_message(u'panel')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.columns[0].text, u"パネル１の説明")
        self.assertEqual(len(self.bot.messages[0].template.columns), 5)
        self.assertEqual(len(self.bot.messages[0].template.columns[0].actions), 3)
        self.assertEqual(self.bot.messages[0].template.columns[0].actions[0].text, u"選択肢１のアクション")
        self.assertEqual(self.bot.messages[0].template.columns[0].actions[0].data.split(u'@@')[0], u"#label1")
        self.send_message(u'imagemap')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].base_url, u"https://testbed.example.com/line/image/https%3A%2F%2Fexample.com%2Fimage.png")
        self.assertEqual(len(self.bot.messages[0].actions), 2)
        self.assertEqual(self.bot.messages[0].actions[0].text, u'イメージマップアクション１')
        self.assertEqual(self.bot.messages[0].actions[1].link_uri, u'http://example.com/imagemap_action')
        self.assertEqual(self.bot.messages[0].actions[1].area.x, 100)
        self.assertEqual(self.bot.messages[0].actions[1].area.y, 200)
        self.assertEqual(self.bot.messages[0].actions[1].area.width, 300)
        self.assertEqual(self.bot.messages[0].actions[1].area.height, 400)
        self.send_event(u'follow')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"フォローしました。")

    def test_scene_labels(self):
        self.test_bot.scenario = Scenario.from_tables([
            (u'default', [
                [u'next', u'*1'],
                [u'*1', u'1'],
                [u'next', u'*2'],
                [u'*2', u'2'],
                [u'next', u'*3'],
                [u'choice', u'@confirm', u'which?'],
                [u'', u'', u'1', u'*1'],
                [u'', u'', u'3', u'*3'],
                [u'*3', u'3'],
                [u'next', u'*1'],
            ]),
        ])
        #print "\n".join([u','.join(s[0]) for s in self.test_bot.scenario.scenes[u'default/共通'].lines])
        self.send_reset()
        self.send_message(u'next')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"1")
        self.send_message(u'next')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"2")
        self.send_message(u'next')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"3")
        self.send_message(u'next')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"1")
        self.send_message(u'next')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"2")
        self.send_message(u'choice')
        data = self.bot.messages[0].template.actions[0].data
        self.send_postback(data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"1")
        # visit_id のテストを一時的に削除
#        self.send_postback(data)
#        self.assertEqual(len(self.bot.messages), 0)


    def test_default_scene(self):
        self.test_bot.scenario = Scenario.from_tables([
            (u'default', [
                [u'1', u'*1'],
                [u'2', u'*from_default'],
                [u'3', u'*3'],
                [u'tab1', u'*tab1/1'],
                [u'*from_default', u'from_default'],
                [u'//', u'from_default'],
                [u'*1', u'1'],
                [u'2', u'*2'],
                [u'*2', u'2'],
            ]),
            (u'tab1', [
                [u'1', u'*1'],
                [u'2', u'*from_default'],
                [u'3', u'*3'],
                [u'*from_default', u'tab1_from_default'],
                [u'//', u'tab1_from_default'],
                [u'*1', u'tab1_1'],
                [u'2', u'*2'],
                [u'*2', u'tab1_2'],
            ]),
        ])
        #print "\n".join([u','.join(s[0]) for s in self.test_bot.scenario.scenes[u'default/共通'].lines])
        self.send_reset()
        self.send_message(u'1')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"1")
        self.send_message(u'2')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"2")
        self.send_message(u'3')
        self.assertEqual(len(self.bot.messages), 0)
        self.send_message(u'2')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"from_default")
        self.send_message(u'3')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"from_default")
        self.send_reset()
        self.send_message(u'tab1')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"tab1_1")
        self.send_message(u'2')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"tab1_2")
        self.send_message(u'3')
        self.assertEqual(len(self.bot.messages), 0)
        self.send_message(u'2')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"tab1_from_default")
        self.send_message(u'3')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"tab1_from_default")


    def test_includes(self):
        self.test_bot.scenario = Scenario.from_tables([
            (u'default', [
                [u'@include', u'*共通'],
                [u'シーン確認', u'シーン1'],
                [u'*共通'],
                [u'共通１', u'共通１メッセージ'],
                [u'切替１', u'シーン１に切り替えます'],
                [u'', u'*default/'],
                [u'切替２', u'シーン２に切り替えます'],
                [u'', u'*シーン2'],
                [u'@include', u'*共通'],
                [u'切替３', u'シーン３に切り替えます'],
                [u'', u'*シーン3'],
                [u'シーンリセット', u'@reset'],
                [u'', u'リセットしました'],
                [u'*シーン2'],
                [u'@include', u'*共通'],
                [u'シーン確認', u'シーン2'],
                [u'*シーン3'],
                [u'@include', u'*共通'],
                [u'シーン確認', u'シーン3'],
            ]),
        ])
        #print "\n".join([u','.join(s[0]) for s in self.test_bot.scenario.scenes[u'default/共通'].lines])
        self.send_reset()
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1")
        self.send_message(u'共通１')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"共通１メッセージ")
        self.send_message(u'切替２')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン２に切り替えます")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン2")
        self.send_message(u'共通１')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"共通１メッセージ")
        self.send_message(u'切替３')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン３に切り替えます")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン3")
        self.send_message(u'シーンリセット')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"リセットしました")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1")

    def test_scenes_with_multitable(self):
        self.test_bot.scenario = Scenario.from_tables([
            (u'シーン1', [
                [u'@include', u'*共通/top'],
                [u'シーン確認', u'シーン1'],
            ]),
            (u'共通', [
                [u'*top'],
                [u'共通１', u'共通１メッセージ'],
                [u'切替１', u'@scene', u'シーン1/top'],
                [u'', u'シーン１に切り替えました'],
                [u'切替２', u'@scene', u'シーン2/top'],
                [u'', u'シーン２に切り替えました'],
                [u'@include', u'*共通/top'],
                [u'切替３', u'シーン３に切り替えます'],
                [u'', u'@scene', u'シーン3/top'],
                [u'シーンリセット', u'@reset'],
                [u'', u'リセットしました'],
            ]),
            (u'シーン2', [
                [u'*top'],
                [u'@include', u'*共通/top'],
                [u'シーン確認', u'シーン2'],
            ]),
            (u'シーン3', [
                [u'*top'],
                [u'@include', u'*共通/top'],
                [u'シーン確認', u'シーン3'],
            ]),
        ])
        #print "\n".join([u','.join(s[0]) for s in self.test_bot.scenario.scenes[u'共通'].lines])
        self.send_reset()
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1")
        self.send_message(u'共通１')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"共通１メッセージ")
        self.send_message(u'切替２')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン２に切り替えました")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン2")
        self.send_message(u'共通１')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"共通１メッセージ")
        self.send_message(u'切替３')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン３に切り替えます")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン3")
        self.send_message(u'シーンリセット')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"リセットしました")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1")

    def test_scenes_complex(self):
        self.test_bot.scenario = Scenario.from_tables([
            (u'base', [
                [u'タブ共通', u'タブ1共通'],
                [u'共通０', u'共通０メッセージ'],
                [u'@include', u'*共通/top'],
                [u'シーン確認', u'シーン1'],
            ]),
            (u'共通', [
                [u'*top'],
                [u'共通１', u'共通１メッセージ'],
                [u'切替１', u'シーン１に切り替えます'],
                [u'', u'*シーン1/top'],
                [u'切替２', u'シーン２に切り替えます'],
                [u'', u'*シーン2/top'],
                [u'切替２の２', u'シーン２カット２に切り替えます'],
                [u'', u'*シーン２／カット２'],
                [u'切替２の２のアクション２', u'シーン２カット２アクション２に切り替えます'],
                [u'', u'*シーン２／カット２＃アクション２'],
                [u'@include', u'*共通/top'],
                [u'切替３', u'シーン３に切り替えます'],
                [u'', u'*シーン3/top'],
                [u'切替３のアクション２', u'シーン３アクション２に切り替えます'],
                [u'', u'*シーン3/top#アクション2'],
                [u'シーンリセット', u'@reset'],
                [u'', u'リセットしました'],
                [u'戻る', u'**戻る'],
            ]),
            (u'シーン2', [
                [u'タブ共通', u'タブ2共通'],
                [u'*top'],
                [u'@include', u'*共通/top'],
                [u'シーン確認', u'シーン2'],
                [u'*カット2', u'カット2に来ました'],
                [u'@include', u'*共通/top'],
                [u'シーン確認', u'シーン2カット2'],
                [u'#アクション2', u'シーン2カット2アクション2'],
            ]),
            (u'シーン3', [
                [u'タブ共通', u'タブ3共通'],
                [u'*top'],
                [u'@include', u'*共通/top'],
                [u'シーン確認', u'シーン3'],
                [u'#アクション2', u'シーン3アクション2'],
            ]),
        ])
        #print "\n".join([u','.join(s[0]) for s in self.test_bot.scenario.scenes[u'共通'].lines])
        self.send_reset()
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1")
        self.send_message(u'タブ共通')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"タブ1共通")
        self.send_message(u'共通０')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"共通０メッセージ")
        self.send_message(u'共通１')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"共通１メッセージ")
        self.send_message(u'切替２')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン２に切り替えます")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン2")
        self.send_message(u'タブ共通')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"タブ2共通")
        self.send_message(u'共通０')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"共通０メッセージ")
        self.send_message(u'共通１')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"共通１メッセージ")
        self.send_message(u'切替３')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン３に切り替えます")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン3")
        self.send_message(u'タブ共通')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"タブ3共通")
        self.send_message(u'切替２の２')
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"シーン２カット２に切り替えます")
        self.assertEqual(self.bot.messages[1].text, u"カット2に来ました")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン2カット2")
        self.send_message(u'タブ共通')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"タブ2共通")
        self.send_message(u'切替３のアクション２')
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"シーン３アクション２に切り替えます")
        self.assertEqual(self.bot.messages[1].text, u"シーン3アクション2")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン3")
        self.send_message(u'タブ共通')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"タブ3共通")
        self.send_message(u'切替２の２のアクション２')
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"シーン２カット２アクション２に切り替えます")
        self.assertEqual(self.bot.messages[1].text, u"シーン2カット2アクション2")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン2カット2")
        self.send_message(u'タブ共通')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"タブ2共通")
        self.send_message(u'戻る')
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン3")
        self.send_message(u'戻る')
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン2カット2")
        self.send_message(u'戻る')
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン3")
        self.send_message(u'戻る')
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン2")
        self.send_message(u'戻る')
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1")

    def test_ai(self):
        self.test_bot.scenario = Scenario.from_tables([
            (u'base', [
                [u'AIリセット', u'@AIリセット', u'AI'],
                [u'//', u'@AI', u'AI'],
                [u'#hoge', u'ふが'],
                [u'*別シーン', u'別シーンに来ました。']
            ]),
            (u'AI', [
                [u'こんにちは', u'こんばんは'],
                [u'', u'{w1}'],
                [u'乗'],
                [u'新幹線'],
                [u'', u'{w2}いいですよね。', u'心が洗われます。'],
                [u'シーケンス'],
                [u'', u'@順々'],
                [u'', u'', u'順番に'],
                [u'', u'', u'メッセージが'],
                [u'', u'', u'再生され', u'ます'],
                [u'ほげ'],
                [u'', u'ほげ', u'#hoge', u'foo'],
                [u'モード離脱'],
                [u'', u'*別シーン'],
                [u'@優先度', u'中', u'中'],
                [u'@常時'],
                [u'', u'さてさて'],
            ]),
        ])
        #print "\n".join([u','.join(s[0]) for s in self.test_bot.scenario.scenes[u'共通'].lines])
        self.send_reset()
        self.send_message(u'こんにちは')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"こんにちは")
        self.send_message(u'こんばんは')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"こんばんは")
        self.send_message(u'新幹線に乗っています。')
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"新幹線いいですよね。")
        self.assertEqual(self.bot.messages[1].text, u"心が洗われます。")
        self.send_message(u'新幹線に乗っています。')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"さてさて")
        self.send_message(u'シーケンス')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"順番に")
        self.send_message(u'シーケンス')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"メッセージが")
        self.send_message(u'シーケンス')
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"再生され")
        self.assertEqual(self.bot.messages[1].text, u"ます")
        self.send_message(u'シーケンス')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"さてさて")
        self.send_message(u'AIリセット')
        self.send_message(u'新幹線に乗っています。')
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"新幹線いいですよね。")
        self.assertEqual(self.bot.messages[1].text, u"心が洗われます。")
        self.send_message(u'シーケンス')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"順番に")
        self.send_message(u'ほげ')
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"ほげ")
        self.assertEqual(self.bot.messages[1].text, u"ふが")
        self.send_message(u'モード離脱')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"別シーンに来ました。")

    def test_ai_memory(self):
        self.test_bot.scenario = Scenario.from_tables([
            (u'base', [
                [u'AIリセット', u'@AIリセット', u'AI'],
                [u'//', u'@AI', u'AI'],
            ]),
            (u'AI', [
                [u'@定義', u'%挨拶', u'こんにちは', u'こんばんは', u'お元気です'],
                [u'@定義', u'%挨拶', u'おはー', u'ばんわー', u'どもー'],
                [u'@定義', u'%乗り物', u'電車', u'タクシー', u'地下鉄', u'飛行機'],
                [u'@定義', u'%乗り物', u'新幹線'],
                [u'@定義', u'%%キーワード', u'館', u'%乗り物'],
                [u'%挨拶'],
                [u'', u'{w1}'],
                [u'乗'],
                [u'%乗り物'],
                [u'', u'{w2}いいですよね。', u'心が洗われます。'],
                [u'好'],
                [u'%乗り物'],
                [u'', u'{w2}は大好きです！'],
                [u'@優先度', u'中', u'中'],
                [u'@常時'],
                [u'', u'さてさて'],
            ]),
        ])
        #print "\n".join([u','.join(s[0]) for s in self.test_bot.scenario.scenes[u'共通'].lines])
        self.send_reset()
        self.send_message(u'こんにちは')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"こんにちは")
        self.send_message(u'こんばんは')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"こんばんは")
        self.send_message(u'どもー')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"どもー")
        self.send_message(u'新幹線に乗っています。')
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"新幹線いいですよね。")
        self.assertEqual(self.bot.messages[1].text, u"心が洗われます。")
        self.send_message(u'好きなんですか？')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"新幹線は大好きです！")
        self.send_message(u'新幹線に乗っています。')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"さてさて")
        self.send_message(u'AIリセット')
        self.send_message(u'飛行機に乗っています。')
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"飛行機いいですよね。")
        self.assertEqual(self.bot.messages[1].text, u"心が洗われます。")

    def test_set(self):
        self.test_bot.scenario = Scenario.from_table([
            [u'/^(on|ｏｎ|off|ｏｆｆ)$/', u'@set', u'$flag', u'{1}'],
            [u'', u'コマンド＝{1}, フラグ＝{$flag}'],
            [u'[$ｆｌａｇ!=on]フラグ確認', u'フラグOFF'],
            [u'[$flag==on]フラグ確認', u'フラグON'],
            [u'フラグリセット', u'@reset'],
            [u'フラグ2オン', u'@set', u'$フラグ2', u'on'],
            [u'フラグ2オフ', u'@set', u'$フラグ2', u'off'],
            [u'［$ｆｌａｇ＝＝ｏｆｆ，$フラグ２==ｏｎ］複合フラグ確認', u'ON and OFF'],
        ])
        self.send_reset()
        self.send_message(u'フラグ確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"フラグOFF")
        self.send_message(u'ｏｎ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"コマンド＝ｏｎ, フラグ＝on")
        self.send_message(u'フラグ確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"フラグON")
        self.send_message(u'ｏｆｆ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"コマンド＝ｏｆｆ, フラグ＝off")
        self.send_message(u'フラグ確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"フラグOFF")
        self.send_message(u'複合フラグ確認')
        self.assertEqual(len(self.bot.messages), 0)
        self.send_message(u'フラグ2オン')
        self.send_message(u'複合フラグ確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"ON and OFF")

    def test_forward(self):
        self.test_bot.scenario = Scenario.from_tables([
            (u'default', [
                [u'forward', u'@転送', u'testbot2', u'#転送'],
                [u'', u'転送しました。'],
            ]),
        ])
        self.test_bot2.scenario = Scenario.from_tables([
            (u'default', [
                [u'#転送', u'転送されてきました。'],
            ]),
        ])
        self.send_reset()
        self.reset_bot2()
        self.send_message(u'forward')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"転送しました。")
        self.assertEqual(len(self.bot2.messages), 1)
        self.assertEqual(self.bot2.messages[0].text, u"転送されてきました。")

    def test_api_send(self):
        self.test_bot.scenario = Scenario.from_tables([
            (u'default', [
                [u'group_add', u'@group_add', u'all'],
                [u'', u'追加しました。'],
                [u'group_del', u'@group_del', u'all'],
                [u'', u'削除しました。'],
                [u'from_api', u'WebAPI経由での呼び出し'],
            ]),
        ])
        self.send_reset()
        self.send_message(u'group_add')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"追加しました。")
        self.send_api_send(u'all', u'from_api')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"WebAPI経由での呼び出し")
        self.send_message(u'group_del')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"削除しました。")
        self.send_api_send(u'all', u'from_api')
        self.assertEqual(len(self.bot.messages), 0)

    def try_lint(self, table):
        try:
            Scenario.from_table(table)
            return None
        except ScenarioSyntaxError as e:
            return unicode(e)

    def test_lint1(self):
        self.send_reset()
        self.assertIsNotNone(self.try_lint([
            [u'test', u'1'],
            [u'', u'2'],
            [u'', u'3'],
            [u'', u'4'],
            [u'', u'5'],
            [u'', u'6'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠ボタン', u'説明テキスト'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠ボタン', u'説明テキスト'],
            [u'', u'', u'選択肢１'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠ボタン', u'説明テキスト'],
            [u'', u'', u'選択肢１', u'アクション１'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠ボタン', u'説明テキスト'],
            [u'', u'', u'選択肢１', u'#tag'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠ボタン', u'説明テキスト'],
            [u'', u'', u'選択肢１', u'アクション１', u'#tag'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠ボタン', u'説明テキスト'],
            [u'', u'', u'選択肢１', u'http://example.com/'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠ボタン', u'説明テキスト'],
            [u'', u'', u'選択肢１', u'http://example.com/', u'#tag'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠ボタン', u'説明テキスト'],
            [u'', u'', u'選択肢１', u'アクション１', u'http://example.com/'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠ボタン', u'説明テキスト'],
            [u'', u'', u'選択肢１'],
            [u'', u'', u'選択肢２'],
            [u'', u'', u'選択肢３'],
            [u'', u'', u'選択肢４'],
            [u'', u'', u'選択肢５'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠ボタン', u'0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789'],
            [u'', u'', u'選択肢１'],
            [u'', u'', u'選択肢２'],
            [u'', u'', u'選択肢３'],
            [u'', u'', u'選択肢４'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠ボタン', u'0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789_'],
            [u'', u'', u'選択肢１'],
            [u'', u'', u'選択肢２'],
            [u'', u'', u'選択肢３'],
            [u'', u'', u'選択肢４'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠ボタン', u'012345678901234567890123456789012345678901234567890123456789', u'title'],
            [u'', u'', u'選択肢１'],
            [u'', u'', u'選択肢２'],
            [u'', u'', u'選択肢３'],
            [u'', u'', u'選択肢４'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠ボタン', u'012345678901234567890123456789012345678901234567890123456789_', u'title'],
            [u'', u'', u'選択肢１'],
            [u'', u'', u'選択肢２'],
            [u'', u'', u'選択肢３'],
            [u'', u'', u'選択肢４'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠ボタン', u'012345678901234567890123456789012345678901234567890123456789', u'', u'=IMAGE("https://example.com/image.png")'],
            [u'', u'', u'選択肢１'],
            [u'', u'', u'選択肢２'],
            [u'', u'', u'選択肢３'],
            [u'', u'', u'選択肢４'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠ボタン', u'012345678901234567890123456789012345678901234567890123456789_', u'', u'=IMAGE("https://example.com/image.png")'],
            [u'', u'', u'選択肢１'],
            [u'', u'', u'選択肢２'],
            [u'', u'', u'選択肢３'],
            [u'', u'', u'選択肢４'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠ボタン', u'説明テキスト'],
            [u'', u'', u'０１２３４５６７８９０１２３４５６７８９'],
            [u'', u'', u'選択肢２'],
            [u'', u'', u'選択肢３'],
            [u'', u'', u'選択肢４'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠ボタン', u'説明テキスト'],
            [u'', u'', u'０１２３４５６７８９０１２３４５６７８９＿'],
            [u'', u'', u'選択肢２'],
            [u'', u'', u'選択肢３'],
            [u'', u'', u'選択肢４'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠パネル'],
            [u'', u'', u'パネル１'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル２'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル３'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル４'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル５'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル６'],
            [u'', u'', u'', u'選択肢１'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠パネル'],
            [u'', u'', u'パネル１'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル２'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル３'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'', u'選択肢２'],
            [u'', u'', u'パネル４'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル５'],
            [u'', u'', u'', u'選択肢１'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠パネル'],
            [u'', u'', u'パネル１'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル２'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル３', u'タイトル３'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル４'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル５'],
            [u'', u'', u'', u'選択肢１'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠パネル'],
            [u'', u'', u'パネル１'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル２'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル３', u'', u'=IMAGE("https://example.com/image.png")'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル４'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル５'],
            [u'', u'', u'', u'選択肢１'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠パネル'],
            [u'', u'', u'パネル１'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル２'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル３', u'', u''],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル４'],
            [u'', u'', u'', u'選択肢１'],
            [u'', u'', u'パネル５'],
            [u'', u'', u'', u'選択肢１'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠イメージマップ', u'=IMAGE("https://example.com/image.png")'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠イメージマップ', u'=IMAGE("https://example.com/image.png")'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠イメージマップ', u'=IMAGE("https://example.com/image.png")'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠イメージマップ', u'=IMAGE("https://example.com/image.png")'],
            [u'', u'', u'0,0,1040,1040', u'#tag'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠イメージマップ', u'=IMAGE("https://example.com/image.png")'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１', u'#tag'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠イメージマップ', u'=IMAGE("https://example.com/image.png")', u'0.1'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠イメージマップ', u'=IMAGE("https://example.com/image.png")', u'2.0'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠イメージマップ', u'=IMAGE("https://example.com/image.png")', u'0.0'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠イメージマップ', u'=IMAGE("https://example.com/image.png")', u'2.1'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠イメージマップ', u'=IMAGE("https://example.com/image.png")', u'-10'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠イメージマップ', u'=IMAGE("https://example.com/image.png")', u'abc'],
        ]))


if __name__ == '__main__':
    unittest.main()
