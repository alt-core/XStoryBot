# coding: utf-8
from __future__ import absolute_import
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
#tb.init_datastore_v3_stub()
#tb.init_memcache_stub()
#tb.init_app_identity_stub()
tb.init_all_stubs()
#tb.deactivate()

BOT_SETTINGS = {
    'OPTIONS': {
        'api_token': u'test_api_token',
        'reset_keyword': u'強制リセット'
    },

    'PLUGINS': {
        'line': {
            'alt_text': u'LINEアプリで確認してください。',
            'allow_special_action_text_for_debug': True,
        },
        'line.more': {
            'command': [u'▽'],
            'image_url': 'https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png',
            'message': u'「続きを読む」',
            'action_pattern': None,
            'ignore_pattern': ur'^「|^リセット$',
            'please_push_more_button_label': u'##please_push_more_button',
        },
        'line.image_text': {
            'more_message': u'「続きを読む」',
            'more_image_url': 'https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png',
        },
        'google_sheets': {},
    },

    'BOTS': {
        'testbot': {
            'interfaces': [{
                'type': 'line',
                'params': {
                    'line_access_token': '<<LINE_ACCESS_TOKEN>>',
                    'line_channel_secret': '00000000000000000000000000000000',
                }
            }],
            'scenario': {
                'type': 'google_sheets',
                'params': {
                    'sheet_id': "<<sheet_id>>",
                    'key_file_json': 'path_to_keyfile_sheets_prod.json',
                }
            }
        },
        'testbot2': {
            'interfaces': [{
                'type': 'line',
                'params': {
                    'line_access_token': '<<LINE_ACCESS_TOKEN>>',
                    'line_channel_secret': '00000000000000000000000000000000',
                }
            }],
            'scenario': {
                'type': 'google_sheets',
                'params': {
                    'sheet_id': "<<sheet_id>>",
                    'key_file_json': 'path_to_keyfile_sheets_prod.json',
                }
            }
        },
    }
}

import settings
import main

import auth
import common_commands
import webapi
import plugin.line.webapi
from scenario import Scenario, ScenarioSyntaxError, ScenarioBuilder


from tests.test import DummyScenarioLoader, dummy_send_request_factory, reinitialize_bot


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

    def push_message(self, to, messages, retry_key=None):
        self.to = to
        if not isinstance(messages, (list, tuple)):
            messages = [messages]
        self.messages = messages


class LinePluginTestCaseBase(unittest.TestCase):

    def setUp(self, bot_settings=BOT_SETTINGS):
        reinitialize_bot(bot_settings)
        self.app = TestApp(webapi.app)
        self.app_line = TestApp(plugin.line.webapi.app)
        self.orig_send_request = common_commands.send_request
        common_commands.send_request = dummy_send_request_factory(self, self.app)
        self.bot = DummyLineBotApi()
        self.test_bot_loader = DummyScenarioLoader()
        self.test_bot = main.get_bot('testbot')
        self.test_bot.scenario_loader = self.test_bot_loader
        self.test_bot.get_interface('line').line_bot_api = self.bot
        self.bot2 = DummyLineBotApi()
        self.test_bot2_loader = DummyScenarioLoader()
        self.test_bot2 = main.get_bot('testbot2')
        self.test_bot2.scenario_loader = self.test_bot2_loader
        self.test_bot2.get_interface('line').line_bot_api = self.bot2
        self.forwarded_messages = []
        import bottle
        bottle.debug(True)

    def tearDown(self):
        common_commands.send_request = self.orig_send_request

    def gen_signature(self, body):
        return base64.b64encode(hmac.new(
            self.test_bot.get_interface('line').line_channel_secret,
            body.encode('utf-8'),
            hashlib.sha256
        ).digest())

    def send_reset(self):
        msg = settings.OPTIONS['reset_keyword']
        data = u'{"events":[{"type":"message","replyToken":"ffffffffffffffffffffffffffffffff","source":{"userId":"Uffffffffffffffffffffffffffffffff","type":"user"},"timestamp":1478027519455,"message":{"type":"text","id":"1234567890123","text":"' + msg + u'"}}]}'
        sign = self.gen_signature(data)
        res = self.app_line.post('/line/callback/testbot', data, content_type='application/json', headers={'X-Line-Signature': sign})
        self.assertEqual(res.status, "200 OK")
        self.assertEqual(res.headers["Content-Type"], u"text/plain; charset=UTF-8")
        res_json = json.loads(res.text)
        self.assertEqual(res_json[u"code"], 200)
        self.assertEqual(res_json[u"result"], u"Success")
        return res_json[u"message"]

    def send_message(self, msg):
        self.bot.messages = []
        data = u'{"events":[{"type":"message","replyToken":"ffffffffffffffffffffffffffffffff","source":{"userId":"Uffffffffffffffffffffffffffffffff","type":"user"},"timestamp":1478027519455,"message":{"type":"text","id":"1234567890123","text":"' + msg + u'"}}]}'
        sign = self.gen_signature(data)
        res = self.app_line.post('/line/callback/testbot', data, content_type='application/json', headers={'X-Line-Signature': sign})
        self.assertEqual(res.status, "200 OK")
        self.assertEqual(res.headers["Content-Type"], u"text/plain; charset=UTF-8")
        res_json = json.loads(res.text)
        self.assertEqual(self.bot.reply_token, 'ffffffffffffffffffffffffffffffff')
        self.assertEqual(res_json[u"code"], 200)
        self.assertEqual(res_json[u"result"], u"Success")
        return res_json[u"message"]

    def send_postback(self, postback_data):
        self.bot.messages = []
        data = u'{"events":[{"type":"postback","replyToken":"ffffffffffffffffffffffffffffffff","source":{"userId":"Uffffffffffffffffffffffffffffffff","type":"user"},"timestamp":1478027519455,"postback":{"data":"' + postback_data + u'"}}]}'
        sign = self.gen_signature(data)
        res = self.app_line.post('/line/callback/testbot', data, content_type='application/json', headers={'X-Line-Signature': sign})
        self.assertEqual(res.status, "200 OK")
        self.assertEqual(res.headers["Content-Type"], u"text/plain; charset=UTF-8")
        res_json = json.loads(res.text)
        self.assertEqual(self.bot.reply_token, 'ffffffffffffffffffffffffffffffff')
        self.assertEqual(res_json[u"code"], 200)
        self.assertEqual(res_json[u"result"], u"Success")
        return res_json[u"message"]

    def send_event(self, event_type):
        self.bot.messages = []
        data = u'{"events":[{"type":"' + event_type + u'","replyToken":"ffffffffffffffffffffffffffffffff","source":{"userId":"Uffffffffffffffffffffffffffffffff","type":"user"},"timestamp":1478027519455}]}'
        sign = self.gen_signature(data)
        res = self.app_line.post('/line/callback/testbot', data, content_type='application/json', headers={'X-Line-Signature': sign})
        self.assertEqual(res.status, "200 OK")
        self.assertEqual(res.headers["Content-Type"], u"text/plain; charset=UTF-8")
        res_json = json.loads(res.text)
        self.assertEqual(self.bot.reply_token, 'ffffffffffffffffffffffffffffffff')
        self.assertEqual(res_json[u"code"], 200)
        self.assertEqual(res_json[u"result"], u"Success")
        return res_json[u"message"]

    def send_api_send(self, group, action):
        self.bot.messages = []
        res = self.app.get('/api/v1/bots/testbot/action?user=group:'+group+'&action='+action+'&token='+auth.api_token)
        self.assertEqual(res.status, "200 OK")
        self.assertEqual(res.headers["Content-Type"], u"text/plain; charset=UTF-8")
        res_json = json.loads(res.text)
        self.assertEqual(res_json[u"code"], 200)
        self.assertEqual(res_json[u"result"], u"Success")
        return res_json[u"message"]

    def reset_bot2(self):
        self.bot2.messages = []


class LineTestCase(LinePluginTestCaseBase):
    def test_scenario(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_table([
            [u'test', u'てすと'],
            [u'', u'てすと？'],
            [u'test_or', u'@or'],
            [u'ｔｅｓｔ＿ｏｒ', u'＠ｏｒ'],
            [u'#', u'この行はコメントです。'],
            [u'dummy_or', u'or ok'],
            [u'#jump', u'jumped'],
            [u'jump_test', u'before jump'],
            [u'', u'#jump'],
            [u'image', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'confirm', u'@confirm', u'Ｃｏｎｆｉｒｍの説明文'],
            [u'', u'', u'選択肢１', u'テキストメッセージのアクション'],
            [u'', u'', u'選択肢２', u'http://example.com/action2'],
            [u'button', u'＠ボタン', u'ボタンの説明文', u'ボタンのタイトル', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
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
            [u'next_button'],
            [u'', u'「メッセージ１」'],
            [u'', u'「メッセージ２」'],
            [u'', u'▽'],
            [u'', u'「メッセージ３」'],
            [u'', u'▽'],
            [u'', u'「メッセージ４」'],
            [u'', u'「メッセージ５」'],
            [u'', u'「メッセージ６」'],
            [u'panel', u'＠パネル'],
            [u'', u'', u'パネル１の説明', u'パネル１', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'http://example.com/panel_action2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション'],
            [u'', u'', u'パネル２の説明', u'パネル２', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'', u'', u'パネル３の説明', u'パネル３', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'', u'', u'パネル４の説明', u'パネル４', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'', u'', u'パネル５の説明', u'パネル５', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'imagemap', u'@imagemap', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'0,0,1040,1040', u'イメージマップアクション１'],
            [u'', u'', u'100,200,300,400', u'http://example.com/imagemap_action'],
            [u'##line.follow', u'follow', u''],
            [u'##line.unfollow', u'unfollow', u''],
            [u'##line.join', u'join', u''],
            [u'##line.leave', u'leave', u''],
            [u'', u'', u''],
            [u'', u'', u''],
            [u'/(.*)/', u'{0}'],
        ], options={'force': True})
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
        self.assertEqual(self.bot.messages[0].original_content_url, u"https://storage.googleapis.com/app_default_bucket/image/80fa4bcab0351fdccb69c66fb55dcd00_1024.png")
        self.assertEqual(self.bot.messages[0].preview_image_url, u"https://storage.googleapis.com/app_default_bucket/image/80fa4bcab0351fdccb69c66fb55dcd00_240.png")
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
        self.send_message(u'next_button')
        self.assertEqual(len(self.bot.messages), 3)
        self.assertEqual(self.bot.messages[0].text, u"「メッセージ１」")
        self.assertEqual(self.bot.messages[1].text, u"「メッセージ２」")
        self.send_message(self.bot.messages[2].actions[0].text)
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"「メッセージ３」")
        self.send_message(self.bot.messages[1].actions[0].text)
#        self.assertEqual(len(self.bot.messages), 2)
#        self.assertEqual(self.bot.messages[0].text, u"「メッセージ１」")
#        self.assertEqual(self.bot.messages[1].template.text, u"「メッセージ２」")
#        self.assertEqual(self.bot.messages[1].template.actions[0].label, u'▽')
#        self.send_postback(self.bot.messages[1].template.actions[0].data)
#        self.assertEqual(len(self.bot.messages), 1)
#        self.assertEqual(self.bot.messages[0].template.text, u"「メッセージ３」")
#        self.assertEqual(self.bot.messages[0].template.actions[0].label, u'▽')
#        self.send_postback(self.bot.messages[0].template.actions[0].data)
        self.assertEqual(len(self.bot.messages), 3)
        self.assertEqual(self.bot.messages[0].text, u"「メッセージ４」")
        self.assertEqual(self.bot.messages[1].text, u"「メッセージ５」")
        self.assertEqual(self.bot.messages[2].text, u"「メッセージ６」")
        self.send_message(u'panel')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.columns[0].text, u"パネル１の説明")
        self.assertEqual(len(self.bot.messages[0].template.columns), 5)
        self.assertEqual(len(self.bot.messages[0].template.columns[0].actions), 3)
        self.assertEqual(self.bot.messages[0].template.columns[0].actions[0].text, u"選択肢１のアクション")
        self.assertEqual(self.bot.messages[0].template.columns[0].actions[0].data.split(u'@@')[0], u"#label1")
        self.send_message(u'imagemap')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].base_url, u"https://storage.googleapis.com/app_default_bucket/imagemap/80fa4bcab0351fdccb69c66fb55dcd00.png")
        self.assertEqual(len(self.bot.messages[0].actions), 2)
        self.assertEqual(self.bot.messages[0].actions[0].text, u'イメージマップアクション１')
        self.assertEqual(self.bot.messages[0].actions[1].link_uri, u'http://example.com/imagemap_action')
        self.assertEqual(self.bot.messages[0].actions[1].area.x, 100)
        self.assertEqual(self.bot.messages[0].actions[1].area.y, 200)
        self.assertEqual(self.bot.messages[0].actions[1].area.width, 300)
        self.assertEqual(self.bot.messages[0].actions[1].area.height, 400)
        self.send_event(u'follow')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"follow")
        self.send_event(u'unfollow')
        self.assertEqual(len(self.bot.messages), 0)
        self.send_event(u'join')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"join")
        self.send_event(u'leave')
        self.assertEqual(len(self.bot.messages), 0)

    def test_sender(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_table([
            [u'test', u'Sender:\nてすと'],
            [u'', u'センダー：\nてすと？'],
            [u'image', u'Sender:\n@image', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'confirm', u'Sender:\n@confirm', u'Ｃｏｎｆｉｒｍの説明文'],
            [u'', u'', u'選択肢１', u'テキストメッセージのアクション'],
            [u'', u'', u'選択肢２', u'http://example.com/action2'],
            [u'button', u'Sender:\n＠ボタン', u'ボタンの説明文', u'ボタンのタイトル', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'選択肢２', u'選択肢２のアクション', u'＃label２'],
            [u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label３'],
            [u'', u'', u'選択肢４', u'選択肢４のアクション', u'#label4'],
            [u'#label1', u'選択肢１のリアクション'],
            [u'＃label２', u'選択肢２のリアクション'],
            [u'#label３', u'選択肢３のリアクション'],
            [u'＃label４', u'選択肢４のリアクション'],
            [u'panel', u'Sender:\n＠パネル'],
            [u'', u'', u'パネル１の説明', u'パネル１', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'http://example.com/panel_action2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション'],
            [u'', u'', u'パネル２の説明', u'パネル２', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'', u'', u'パネル３の説明', u'パネル３', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'', u'', u'パネル４の説明', u'パネル４', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'', u'', u'パネル５の説明', u'パネル５', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'imagemap', u'Sender:\n@imagemap', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'0,0,1040,1040', u'イメージマップアクション１'],
            [u'', u'', u'100,200,300,400', u'http://example.com/imagemap_action'],
            [u'', u'', u''],
            [u'', u'', u''],
            [u'/(.*)/', u'{0}'],
        ], options={'force': True})
        self.send_reset()
        self.send_message(u'test')
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"てすと")
        self.assertEqual(self.bot.messages[0].sender.name, u"Sender")
        self.assertEqual(self.bot.messages[1].text, u"てすと？")
        self.assertEqual(self.bot.messages[1].sender.name, u"センダー")
        self.send_message(u'image')
        self.assertEqual(self.bot.messages[0].original_content_url, u"https://storage.googleapis.com/app_default_bucket/image/80fa4bcab0351fdccb69c66fb55dcd00_1024.png")
        self.assertEqual(self.bot.messages[0].preview_image_url, u"https://storage.googleapis.com/app_default_bucket/image/80fa4bcab0351fdccb69c66fb55dcd00_240.png")
        self.assertEqual(self.bot.messages[0].sender.name, u"Sender")
        self.send_message(u'confirm')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.text, u"Ｃｏｎｆｉｒｍの説明文")
        self.assertEqual(len(self.bot.messages[0].template.actions), 2)
        self.assertEqual(self.bot.messages[0].template.actions[0].type, "message")
        self.assertEqual(self.bot.messages[0].template.actions[0].text, u"テキストメッセージのアクション")
        self.assertEqual(self.bot.messages[0].template.actions[1].type, "uri")
        self.assertEqual(self.bot.messages[0].template.actions[1].uri, u"http://example.com/action2")
        self.assertEqual(self.bot.messages[0].sender.name, u"Sender")
        self.send_message(u'button')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.text, u"ボタンの説明文")
        self.assertEqual(self.bot.messages[0].sender.name, u"Sender")
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
        self.send_message(u'panel')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.columns[0].text, u"パネル１の説明")
        self.assertEqual(self.bot.messages[0].sender.name, u"Sender")
        self.assertEqual(len(self.bot.messages[0].template.columns), 5)
        self.assertEqual(len(self.bot.messages[0].template.columns[0].actions), 3)
        self.assertEqual(self.bot.messages[0].template.columns[0].actions[0].text, u"選択肢１のアクション")
        self.assertEqual(self.bot.messages[0].template.columns[0].actions[0].data.split(u'@@')[0], u"#label1")
        self.send_message(u'imagemap')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].base_url, u"https://storage.googleapis.com/app_default_bucket/imagemap/80fa4bcab0351fdccb69c66fb55dcd00.png")
        self.assertEqual(self.bot.messages[0].sender.name, u"Sender")
        self.assertEqual(len(self.bot.messages[0].actions), 2)
        self.assertEqual(self.bot.messages[0].actions[0].text, u'イメージマップアクション１')
        self.assertEqual(self.bot.messages[0].actions[1].link_uri, u'http://example.com/imagemap_action')
        self.assertEqual(self.bot.messages[0].actions[1].area.x, 100)
        self.assertEqual(self.bot.messages[0].actions[1].area.y, 200)
        self.assertEqual(self.bot.messages[0].actions[1].area.width, 300)
        self.assertEqual(self.bot.messages[0].actions[1].area.height, 400)

    def test_scenario_build_with_skip_image_option(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_table([
            [u'image', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'confirm', u'@confirm', u'Ｃｏｎｆｉｒｍの説明文'],
            [u'', u'', u'選択肢１', u'テキストメッセージのアクション'],
            [u'', u'', u'選択肢２', u'http://example.com/action2'],
            [u'button', u'＠ボタン', u'ボタンの説明文', u'ボタンのタイトル', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
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
            [u'panel', u'＠パネル'],
            [u'', u'', u'パネル１の説明', u'パネル１', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'http://example.com/panel_action2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション'],
            [u'', u'', u'パネル２の説明', u'パネル２', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'', u'', u'パネル３の説明', u'パネル３', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'', u'', u'パネル４の説明', u'パネル４', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'', u'', u'パネル５の説明', u'パネル５', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'', u'選択肢１', u'選択肢１のアクション', u'#label1'],
            [u'', u'', u'', u'選択肢２', u'選択肢２のアクション', u'#label2'],
            [u'', u'', u'', u'選択肢３', u'選択肢３のアクション', u'#label3'],
            [u'imagemap', u'@imagemap', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'0,0,1040,1040', u'イメージマップアクション１'],
            [u'', u'', u'100,200,300,400', u'http://example.com/imagemap_action'],
            [u'', u'', u''],
            [u'', u'', u''],
            [u'/(.*)/', u'{0}'],
        ], options={'skip_image': True})
        self.send_reset()
        self.send_message(u'image')
        self.assertEqual(self.bot.messages[0].original_content_url, u"https://storage.googleapis.com/app_default_bucket/image/80fa4bcab0351fdccb69c66fb55dcd00_1024.png")
        self.assertEqual(self.bot.messages[0].preview_image_url, u"https://storage.googleapis.com/app_default_bucket/image/80fa4bcab0351fdccb69c66fb55dcd00_240.png")
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
        self.send_message(u'panel')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.columns[0].text, u"パネル１の説明")
        self.assertEqual(len(self.bot.messages[0].template.columns), 5)
        self.assertEqual(len(self.bot.messages[0].template.columns[0].actions), 3)
        self.assertEqual(self.bot.messages[0].template.columns[0].actions[0].text, u"選択肢１のアクション")
        self.assertEqual(self.bot.messages[0].template.columns[0].actions[0].data.split(u'@@')[0], u"#label1")
        self.send_message(u'imagemap')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].base_url, u"https://storage.googleapis.com/app_default_bucket/imagemap/80fa4bcab0351fdccb69c66fb55dcd00.png")
        self.assertEqual(len(self.bot.messages[0].actions), 2)
        self.assertEqual(self.bot.messages[0].actions[0].text, u'イメージマップアクション１')
        self.assertEqual(self.bot.messages[0].actions[1].link_uri, u'http://example.com/imagemap_action')
        self.assertEqual(self.bot.messages[0].actions[1].area.x, 100)
        self.assertEqual(self.bot.messages[0].actions[1].area.y, 200)
        self.assertEqual(self.bot.messages[0].actions[1].area.width, 300)
        self.assertEqual(self.bot.messages[0].actions[1].area.height, 400)

    def test_button_and_next_button(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_table([
            [u'##please_push_more_button', u'先に続きを読んでください'],
            [u'next_button'],
            [u'', u'「メッセージ１」'],
            [u'', u'「メッセージ２」'],
            [u'', u'▽'],
            [u'', u'＠ボタン', u'ボタンの説明文', u'ボタンのタイトル', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'選択肢１', u'選択肢１のアクション', u'##'],
            [u'##', u'「メッセージ３」'],
            [u'', u'▽'],
            [u'', u'「メッセージ４」'],
            [u'', u'「メッセージ５」'],
            [u'', u'「メッセージ６」'],
            [u'リセット', u'@clear_next_label'],
            [u'', u'「メッセージ７」'],
        ])
        self.send_reset()
        self.send_message(u'next_button')
        self.assertEqual(len(self.bot.messages), 3)
        self.assertEqual(self.bot.messages[0].text, u"「メッセージ１」")
        self.assertEqual(self.bot.messages[1].text, u"「メッセージ２」")
        self.send_message(self.bot.messages[2].actions[0].text)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].template.text, u"ボタンの説明文")
        button_message = self.bot.messages[0].template.actions[0].text
        self.send_postback(self.bot.messages[0].template.actions[0].data)
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"「メッセージ３」")
        next_button = self.bot.messages[1].actions[0].text
        self.send_message(button_message)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"先に続きを読んでください")
        self.send_message(next_button)
        self.assertEqual(len(self.bot.messages), 3)
        self.assertEqual(self.bot.messages[0].text, u"「メッセージ４」")
        self.assertEqual(self.bot.messages[1].text, u"「メッセージ５」")
        self.assertEqual(self.bot.messages[2].text, u"「メッセージ６」")
        self.send_message(u'next_button')
        self.assertEqual(len(self.bot.messages), 3)
        self.assertEqual(self.bot.messages[0].text, u"「メッセージ１」")
        self.assertEqual(self.bot.messages[1].text, u"「メッセージ２」")
        self.send_message(u'next_button')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"先に続きを読んでください")
        self.send_message(u'リセット')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"「メッセージ７」")
        self.send_message(u'next_button')
        self.assertEqual(len(self.bot.messages), 3)
        self.assertEqual(self.bot.messages[0].text, u"「メッセージ１」")
        self.assertEqual(self.bot.messages[1].text, u"「メッセージ２」")
        self.send_message(u'next_button')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"先に続きを読んでください")
        self.send_message(u'リセット')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"「メッセージ７」")

    def test_scene_labels(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
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
        # action_token により実行が無効化される
        self.send_postback(data)
        self.assertEqual(len(self.bot.messages), 0)

    def test_anonymous_label_and_seq(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
            (u'共通', [
                [u'スタート', u'*シーン1/top'],
                [u'*top'],
                [u'切替１', u'シーン１に切り替えます'],
                [u'', u'*シーン1/top'],
                [u'切替２', u'シーン２に切り替えます'],
                [u'', u'*シーン2/top'],
                [u'ノードリセットall', u'@ノードリセット'],
                [u'シーンリセット', u'@reset'],
                [u'', u'リセットしました'],
            ]),
            (u'シーン1', [
                [u'*top'],
                [u'@include', u'*共通/top'],
                [u'シーン確認', u'シーン1'],
                [u'メッセージ', u'@seq', u'##1', u'##2'],
                [u'##', u'シーン1topメッセージ1回目'],
                [u'##', u'シーン1topメッセージ2回目以降'],
                [u'ノードリセット1', u'@ノードリセット', u'シーン1'],
                [u'*1-2'],
                [u'@include', u'*共通/top'],
                [u'シーン確認', u'シーン1-2'],
                [u'メッセージ', u'@順々', u'##1', u'##2'],
                [u'#dummy', u'ダミー'],
                [u'##', u'シーン1-2メッセージ1回目'],
                [u'', u'▽'],
                [u'', u'シーン1-2メッセージ1回目-2'],
                [u'dummy', u'ダミー'],
                [u'##', u'シーン1-2メッセージ2回目以降'],
            ]),
            (u'シーン2', [
                [u'*top'],
                [u'@include', u'*共通/top'],
                [u'シーン確認', u'シーン2'],
                [u'メッセージ', u'@seq', u'##', u'##2'],
                [u'dummy', u'ダミー'],
                [u'##', u'シーン2メッセージ1回目'],
                [u'#dummy', u'ダミー'],
                [u'##', u'シーン2メッセージ2回目以降'],
                [u'ノードリセット2to1', u'@ノードリセット', u'シーン1'],
            ]),
        ])
        #print "\n".join([u','.join(s[0]) for s in self.test_bot.scenario.scenes[u'共通'].lines])
        self.send_reset()
        self.send_message(u'スタート')
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1topメッセージ1回目")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1topメッセージ2回目以降")
        self.send_message(u'*1-2')
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1-2")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"シーン1-2メッセージ1回目")
        self.send_message(self.bot.messages[1].actions[0].text)
#        self.assertEqual(len(self.bot.messages), 1)
#        self.assertEqual(self.bot.messages[0].template.text, u"シーン1-2メッセージ1回目")
#        self.assertEqual(self.bot.messages[0].template.actions[0].label, u'▽')
#        self.send_postback(self.bot.messages[0].template.actions[0].data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1-2メッセージ1回目-2")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1-2メッセージ2回目以降")
        self.send_message(u'切替１')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン１に切り替えます")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1topメッセージ2回目以降")
        self.send_message(u'ノードリセット1')
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1topメッセージ1回目")
        self.send_message(u'*1-2')
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.messages[0].text, u"シーン1-2メッセージ1回目")
        self.send_message(self.bot.messages[1].actions[0].text)
#        self.assertEqual(len(self.bot.messages), 1)
#        self.assertEqual(self.bot.messages[0].template.text, u"シーン1-2メッセージ1回目")
#        self.assertEqual(self.bot.messages[0].template.actions[0].label, u'▽')
#        self.send_postback(self.bot.messages[0].template.actions[0].data)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1-2メッセージ1回目-2")
        self.send_message(u'切替２')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン２に切り替えます")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン2メッセージ1回目")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン2メッセージ2回目以降")
        self.send_message(u'ノードリセット2to1')
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン2メッセージ2回目以降")
        self.send_message(u'切替１')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン１に切り替えます")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1topメッセージ1回目")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1topメッセージ2回目以降")
        self.send_message(u'切替２')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン２に切り替えます")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン2メッセージ2回目以降")
        self.send_message(u'ノードリセットall')
        self.send_message(u'切替１')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン１に切り替えます")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1topメッセージ1回目")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン1topメッセージ2回目以降")
        self.send_message(u'切替２')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン２に切り替えます")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン2メッセージ1回目")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"シーン2メッセージ2回目以降")

    def test_forward(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
            (u'default', [
                [u'forward', u'@転送', u'testbot2', u'#転送'],
                [u'', u'転送しました。'],
            ]),
        ])
        self.test_bot2.scenario = ScenarioBuilder.build_from_tables([
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
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
            (u'default', [
                [u'group_add', u'@group_add', u'all'],
                [u'', u'追加しました。'],
                [u'group_del', u'@group_del', u'all'],
                [u'', u'削除しました。'],
                [u'group_clear', u'@group_clear', u'all'],
                [u'', u'クリアしました。'],
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
        self.send_message(u'group_add')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"追加しました。")
        self.send_api_send(u'all', u'from_api')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"WebAPI経由での呼び出し")
        self.send_message(u'group_clear')
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0].text, u"クリアしました。")
        self.send_api_send(u'all', u'from_api')
        self.assertEqual(len(self.bot.messages), 0)

    def test_image_text(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
            (u'default', [
                [u'imagetext', u'@imagetext', u"あ/b\nｃ"],
                [u'long_imagetext', u'@imagetext',
                 u"０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９"
                 + u"０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９"
                 + u"０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９"
                 + u"０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９０１２３４５６７８９"
                 ],
            ]),
        ])
        self.send_reset()
        self.send_message(u'imagetext')
        self.assertEqual(len(self.bot.messages), 1)
#        self.assertEqual(self.bot.messages[0].base_url, u"https://testbed.example.com/line/image/https%3A%2F%2Fstorage.googleapis.com%2Fapp_default_bucket%2Fimagetext%2Ff8a64e127730444bfc3b53b21de8bae0.png")
        self.send_message(u'long_imagetext')
        self.assertEqual(len(self.bot.messages), 2)
#        self.assertEqual(self.bot.messages[0].base_url, u"https://testbed.example.com/line/image/https%3A%2F%2Fstorage.googleapis.com%2Fapp_default_bucket%2Fimagetext%2Fa987d0bad7daa220ecd9c5b942165099.png")
        self.send_message(self.bot.messages[1].actions[0].text)
        self.assertEqual(len(self.bot.messages), 1)
#        self.assertEqual(self.bot.messages[0].base_url, u"https://testbed.example.com/line/image/https%3A%2F%2Fstorage.googleapis.com%2Fapp_default_bucket%2Fimagetext%2Fe785e8fa3b6b59748ddd377d2f41e7dd.png")

    def try_lint(self, table):
        try:
            ScenarioBuilder.build_from_table(table)
            return None
        except ScenarioSyntaxError as e:
            return unicode(e)

    def test_lint1(self):
        self.assertIsNone(self.try_lint([
            [u'test', u'1'],
            [u'', u'2'],
            [u'', u'3'],
            [u'', u'4'],
            [u'', u'5'],
        ]))
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
            [u'test', u'＠ボタン', u'012345678901234567890123456789012345678901234567890123456789', u'', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'選択肢１'],
            [u'', u'', u'選択肢２'],
            [u'', u'', u'選択肢３'],
            [u'', u'', u'選択肢４'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠ボタン', u'012345678901234567890123456789012345678901234567890123456789_', u'', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
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
            [u'', u'', u'パネル３', u'', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
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
            [u'test', u'＠イメージマップ', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
        ]))
        self.assertIsNone(self.try_lint([
            [u'test', u'＠イメージマップ', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠イメージマップ', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
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
            [u'test', u'＠イメージマップ', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'0,0,1040,1040', u'#tag'],
        ]))
        self.assertIsNotNone(self.try_lint([
            [u'test', u'＠イメージマップ', u'=IMAGE("https://www.google.co.jp/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png")'],
            [u'', u'', u'0,0,1040,1040', u'選択肢１', u'#tag'],
        ]))


if __name__ == '__main__':
    unittest.main()
