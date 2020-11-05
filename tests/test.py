# coding: utf-8
from __future__ import absolute_import
import unittest
from pprint import pprint
import urllib
#import sys
#reload(sys)
#sys.setdefaultencoding('utf-8')

from webtest import TestApp
import logging

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
        'reset_keyword': u'強制リセット',
        'timezone': 'Asia/Tokyo',
    },

    'PLUGINS': {
        'plaintext': {},
        'google_sheets': {},
        'timestamp': {
            'now_format': '%Y/%m/%d %H:%M:%S',
            'timezone': 'Asia/Tokyo',
        },
    },

    'BOTS': {
        'testbot': {
            'interfaces': [{
                'type': 'plaintext',
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
                'type': 'plaintext',
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
from scenario import Scenario, ScenarioSyntaxError, ScenarioBuilder


def reinitialize_bot(bot_settings):
    # settings を上書きして main.initialize() で再設定
    settings.OPTIONS = bot_settings['OPTIONS']
    settings.PLUGINS = bot_settings['PLUGINS']
    settings.BOTS = bot_settings['BOTS']

    main.initialize()


class DummyScenarioLoader(object):
    def __init__(self):
        self.data = []

    def load_scenario(self):
        return self.data


def dummy_send_request_factory(test, app):
    def dummy_send_request(bot_name, user, action, delay_secs=None):
        params = {
            u'user': user.serialize(),
            u'action': action,
            u'token': auth.api_token
        }
        for key, value in params.items():
            if isinstance(value, unicode):
                params[key] = value.encode('utf-8')
        test.forwarded_messages = []
        res = app.post('/api/v1/bots/{}/action'.format(bot_name), params)
        test.assertEqual(res.status, "200 OK")
        test.assertEqual(res.headers["Content-Type"], u"text/plain; charset=UTF-8")
        res_json = json.loads(res.text)
        test.assertEqual(res_json[u"code"], 200)
        test.assertEqual(res_json[u"result"], u"Success")
        msgs = res_json[u"message"].rstrip()
        test.forwarded_messages = msgs.split(u"\n") if msgs else []

    return dummy_send_request


class BotTestCaseBase(unittest.TestCase):

    def setUp(self, bot_settings=BOT_SETTINGS):
        reinitialize_bot(bot_settings)
        self.app = TestApp(webapi.app)
        self.orig_send_request = common_commands.send_request
        common_commands.send_request = dummy_send_request_factory(self, self.app)
        self.test_bot_loader = DummyScenarioLoader()
        self.test_bot = main.get_bot('testbot')
        self.test_bot.scenario_loader = self.test_bot_loader
        self.test_bot2_loader = DummyScenarioLoader()
        self.test_bot2 = main.get_bot('testbot2')
        self.test_bot2.scenario_loader = self.test_bot2_loader
        self.messages = []
        self.forwarded_messages = []
        import bottle
        bottle.debug(True)
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        common_commands.send_request = self.orig_send_request
        logging.disable(logging.NOTSET)

    def send_action_to(self, bot_name, user_id, action):
        res = self.app.get(('/api/v1/bots/'+bot_name+'/action?user='+user_id+'&action='+urllib.quote(action.encode('utf-8'))+'&token='+auth.api_token).encode('utf-8'))
        self.assertEqual(res.status, "200 OK")
        self.assertEqual(res.headers["Content-Type"], u"text/plain; charset=UTF-8")
        res_json = json.loads(res.text)
        self.assertEqual(res_json[u"code"], 200)
        self.assertEqual(res_json[u"result"], u"Success")
        msgs = res_json[u"message"].rstrip()
        self.messages = msgs.split(u"\n") if msgs else []
        return res_json[u"message"]

    def send_message(self, action):
        return self.send_action_to('testbot', 'plaintext:0001', action).split(u"\n")

    def send_group_message(self, group_id, action):
        return self.send_action_to('testbot', 'group:'+group_id, action)

    def send_reset(self):
        return self.send_message(settings.OPTIONS['reset_keyword'])

    def send_reset2(self):
        return self.send_action_to('testbot2', 'plaintext:0001', settings.OPTIONS['reset_keyword'])


class MainTestCase(BotTestCaseBase):
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
            [u'', u'', u''],
            [u'', u'', u''],
            [u'/(.*)/', u'{0}'],
        ], options={'force': True}, version=1)
        self.send_reset()
        self.send_message(u'test')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"てすと")
        self.assertEqual(self.messages[1], u"てすと？")
        self.send_message(u'hoge')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"hoge")
        self.send_message(u'test_or')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"or ok")
        self.send_message(u'ｔｅｓｔ＿ｏｒ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"or ok")
        self.send_message(u'jump_test')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"before jump")
        self.assertEqual(self.messages[1], u"jumped")

    def test_scenario_v2(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_table([
            [u'test', u'てすと'],
            [u'', u'てすと？'],
            [u'CAPItal', u'capital'],
            [u'ＺＥＮKAKu', u'zenkaku'],
            [u'word', u'word'],
            [u'/^regex$/NL', u'regex'],
            [u'/ignorecase/i', u'ignorecase'],
            [u'ABC&HIJ', u'abcdefghij'],
            [u'(123|890)&(opq|stu|xyz)', u'opqrstuvwxyz'],
            [u' (　あ　｜　い　｜　う　)　＆　（α|β） & ！', u'mix'],
            [ur'\(\|\&\\\)', u'escape'],
            [ur'\（\｜\＆￥\）', u'escapezen'],
            [u'(/[あいう]/＆（　/α/ | /β/　）)&?', u'regexmix'],
            [u'(/ab/|/a(.)c/|/d/)&(/(123)/|/4(5)6/)', u'capture:{0}/{1}/{2}'],
            [u'//', u'not found'],
        ], options={'force': True}, version=2)
        self.send_reset()
        self.send_message(u'test')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"てすと")
        self.assertEqual(self.messages[1], u"てすと？")
        self.send_message(u'capITAL')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"capital")
        self.send_message(u'zENKＡＫＵ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"zenkaku")
        self.send_message(u'partialwords')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"word")
        self.send_message(u'abc')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"not found")
        self.send_message(u'ReＧｅx')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"regex")
        self.send_message(u' regex')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"not found")
        self.send_message(u'regex ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"not found")
        self.send_message(u'xxIgnoreCasexx')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"ignorecase")
        self.send_message(u'abcdefghij')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"abcdefghij")
        self.send_message(u'hijabc')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"abcdefghij")
        self.send_message(u'123')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"not found")
        self.send_message(u'xyz')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"not found")
        self.send_message(u'123xyz')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"opqrstuvwxyz")
        self.send_message(u'opqr890')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"opqrstuvwxyz")
        self.send_message(u'あいβγ!?')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"mix")
        self.send_message(u'無関係α！うくすつぬ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"mix")
        self.send_message(u'ぁα!')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"not found")
        self.send_message(u'アα!')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"not found")
        self.send_message(ur'(|&\)')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"escape")
        self.send_message(ur'（｜＆￥）')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"escapezen")
        self.send_message(u'あいβγ?')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"regexmix")
        self.send_message(u'無関係α？うくすつぬ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"regexmix")
        self.send_message(u'123456abcdef')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"capture:abc123/b/123")

    def test_scene_labels(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
            (u'default', [
                [u'next', u'*1'],
                [u'*1', u'1'],
                [u'next', u'*2'],
                [u'*2', u'2'],
                [u'next', u'*3'],
                [u'*3', u'3'],
                [u'next', u'*1'],
            ]),
        ])
        self.send_reset()
        self.send_message(u'next')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"1")
        self.send_message(u'next')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"2")
        self.send_message(u'next')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"3")
        self.send_message(u'next')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"1")
        self.send_message(u'next')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"2")


    def test_default_scene(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
            (u'default', [
                [u'##error_invalid_label', u'invalid label default'],
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
                [u'##error_invalid_label', u'invalid label tab1'],
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
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"1")
        self.send_message(u'2')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"2")
        self.send_message(u'3')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"invalid label default")
        self.send_message(u'2')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"from_default")
        self.send_message(u'3')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"from_default")
        self.send_reset()
        self.send_message(u'tab1')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"tab1_1")
        self.send_message(u'2')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"tab1_2")
        self.send_message(u'3')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"invalid label tab1")
        self.send_message(u'2')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"tab1_from_default")
        self.send_message(u'3')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"tab1_from_default")


    def test_includes(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
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
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1")
        self.send_message(u'共通１')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"共通１メッセージ")
        self.send_message(u'切替２')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン２に切り替えます")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン2")
        self.send_message(u'共通１')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"共通１メッセージ")
        self.send_message(u'切替３')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン３に切り替えます")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン3")
        self.send_message(u'シーンリセット')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"リセットしました")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1")

    def test_scenes_with_multitable(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
            (u'シーン1', [
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
                [u'@include', u'*共通/top'],
                [u'切替３', u'シーン３に切り替えます'],
                [u'', u'*シーン3/top'],
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
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1")
        self.send_message(u'共通１')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"共通１メッセージ")
        self.send_message(u'切替２')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン２に切り替えます")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン2")
        self.send_message(u'共通１')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"共通１メッセージ")
        self.send_message(u'切替３')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン３に切り替えます")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン3")
        self.send_message(u'シーンリセット')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"リセットしました")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1")

        uri = self.test_bot.scenario.save_to_storage()
        self.test_bot.scenario = Scenario.load_from_uri(uri)

        self.send_reset()
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1")
        self.send_message(u'共通１')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"共通１メッセージ")
        self.send_message(u'切替２')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン２に切り替えます")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン2")
        self.send_message(u'共通１')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"共通１メッセージ")
        self.send_message(u'切替３')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン３に切り替えます")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン3")
        self.send_message(u'シーンリセット')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"リセットしました")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1")


    def test_scenes_complex(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
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
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1")
        self.send_message(u'タブ共通')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"タブ1共通")
        self.send_message(u'共通０')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"共通０メッセージ")
        self.send_message(u'共通１')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"共通１メッセージ")
        self.send_message(u'切替２')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン２に切り替えます")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン2")
        self.send_message(u'タブ共通')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"タブ2共通")
        self.send_message(u'共通０')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"共通０メッセージ")
        self.send_message(u'共通１')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"共通１メッセージ")
        self.send_message(u'切替３')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン３に切り替えます")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン3")
        self.send_message(u'タブ共通')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"タブ3共通")
        self.send_message(u'切替２の２')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"シーン２カット２に切り替えます")
        self.assertEqual(self.messages[1], u"カット2に来ました")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン2カット2")
        self.send_message(u'タブ共通')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"タブ2共通")
        self.send_message(u'切替３のアクション２')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"シーン３アクション２に切り替えます")
        self.assertEqual(self.messages[1], u"シーン3アクション2")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン3")
        self.send_message(u'タブ共通')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"タブ3共通")
        self.send_message(u'切替２の２のアクション２')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"シーン２カット２アクション２に切り替えます")
        self.assertEqual(self.messages[1], u"シーン2カット2アクション2")
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン2カット2")
        self.send_message(u'タブ共通')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"タブ2共通")
        self.send_message(u'戻る')
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン3")
        self.send_message(u'戻る')
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン2カット2")
        self.send_message(u'戻る')
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン3")
        self.send_message(u'戻る')
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン2")
        self.send_message(u'戻る')
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1")

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
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1topメッセージ1回目")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1topメッセージ2回目以降")
        self.send_message(u'*1-2')
        self.send_message(u'シーン確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1-2")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"シーン1-2メッセージ1回目")
        self.assertEqual(self.messages[1], u"シーン1-2メッセージ1回目-2")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1-2メッセージ2回目以降")
        self.send_message(u'切替１')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン１に切り替えます")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1topメッセージ2回目以降")
        self.send_message(u'ノードリセット1')
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1topメッセージ1回目")
        self.send_message(u'*1-2')
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"シーン1-2メッセージ1回目")
        self.assertEqual(self.messages[1], u"シーン1-2メッセージ1回目-2")
        self.send_message(u'切替２')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン２に切り替えます")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン2メッセージ1回目")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン2メッセージ2回目以降")
        self.send_message(u'ノードリセット2to1')
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン2メッセージ2回目以降")
        self.send_message(u'切替１')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン１に切り替えます")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1topメッセージ1回目")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1topメッセージ2回目以降")
        self.send_message(u'切替２')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン２に切り替えます")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン2メッセージ2回目以降")
        self.send_message(u'ノードリセットall')
        self.send_message(u'切替１')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン１に切り替えます")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1topメッセージ1回目")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン1topメッセージ2回目以降")
        self.send_message(u'切替２')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン２に切り替えます")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン2メッセージ1回目")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"シーン2メッセージ2回目以降")

    def test_condition_regex_flag(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_table([
            [u'/^(test_123)$/', u'none:{1}'],
            [u'/^(test:i_123)$/i', u'i:{1}'],
            [u'/^(test:n_123)$/N', u'N:{1}'],
            [u'/^(test:l_123)$/L', u'L:{1}'],
            [u'/^(test:in_123)$/iN', u'iN:{1}'],
            [u'/^(test:nl_123)$/NL', u'NL:{1}'],
            [u'/^(test:inl_123)$/iNL', u'iNL:{1}'],
        ])
        self.send_reset()
        self.send_message(u'test_123')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"none:test_123")
        self.send_message(u'hogetest_123')
        self.assertEqual(len(self.messages), 0)
        self.send_message(u'TeSt:i_123')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"i:TeSt:i_123")
        self.send_message(u'ｔest_123')
        self.assertEqual(len(self.messages), 0)
        self.send_message(u'ｔest：n＿1２3')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"N:test:n_123")
        self.send_message(u'Test:n_123')
        self.assertEqual(len(self.messages), 0)
        self.send_message(u'TeSt:l_123')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"L:test:l_123")
        self.send_message(u'ｔest:l_123')
        self.assertEqual(len(self.messages), 0)
        self.send_message(u'TｅＳt：in_12３')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"iN:TeSt:in_123")
        self.send_message(u'TｅＳt:NＬ＿１23')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"NL:test:nl_123")
        self.send_message(u'ＴＥＳＴ：ＩＮＬ＿１２３')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"iNL:test:inl_123")

    def test_set_v1(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_table([
            [u'/^(on|ｏｎ|off|ｏｆｆ)$/N', u'@set', u'$flag', u'{1}'],
            [u'', u'コマンド＝{1}, フラグ＝{$flag}'],
            [u'［$ｆｌａｇ!=on]フラグ確認', u'フラグOFF'],
            [u'[$flag==on]フラグ確認', u'フラグON'],
            [u'フラグリセット', u'@reset'],
            [u'フラグ2オン', u'@set', u'$フラグ2', u'on'],
            [u'フラグ2オフ', u'@set', u'$フラグ2', u'off'],
            [u'［$ｆｌａｇ＝＝ｏｆｆ，$フラグ２==ｏｎ］複合フラグ確認', u'ON and OFF'],
            [u'条件分岐', u'@if', u'$flag==on', u'##1', u'##2'],
            [u'##', u'真'],
            [u'##', u'偽'],
        ], version=1)
        self.send_reset()
        self.send_message(u'フラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"フラグOFF")
        self.send_message(u'ｏｎ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"コマンド＝on, フラグ＝on")
        self.send_message(u'フラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"フラグON")
        self.send_message(u'条件分岐')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"真")
        self.send_message(u'ｏｆｆ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"コマンド＝off, フラグ＝off")
        self.send_message(u'フラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"フラグOFF")
        self.send_message(u'条件分岐')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"偽")
        self.send_message(u'複合フラグ確認')
        self.assertEqual(len(self.messages), 0)
        self.send_message(u'フラグ2オン')
        self.send_message(u'複合フラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"ON and OFF")

    def test_set_v2(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_table([
            [u'/^(on|ｏｎ|off|ｏｆｆ)$/N', u'@set', u'$flag', u'$1'],
            [u'', u'コマンド＝{1}, フラグ＝{$flag}'],
            [u'[$flag!="on"]単独フラグ確認', u'フラグOFF'],
            [u'[$flag=="on"]単独フラグ確認', u'フラグON'],
            [u'フラグリセット', u'@reset'],
            [u'フラグ2オン', u'@set', u'$flag2', u'ON'],
            [u'フラグ2オフ', u'@set', u'$flag2', u'OFF'],
            [u'［$flag=="off"&&$flag2］複合フラグ確認', u'ON and OFF'],
            [u'［$flag!="off"||$flag2!=OFF］ORフラグ確認', u'ON or ON'],
            [u'［!($flag=="off")&&!($flag2==OFF)］NOTフラグ確認', u'ON and ON'],
            [u'フラグ1条件分岐', u'@if', u'$flag=="on"', u'##1', u'##2'],
            [u'##', u'真'],
            [u'##', u'偽'],
            [u'フラグ2条件分岐', u'@if', u'$flag2', u'##1', u'##2'],
            [u'##', u'2真'],
            [u'##', u'2偽'],
            [u'フラグ2NOT条件分岐', u'@if', u'!$flag2', u'##1', u'##2'],
            [u'##', u'2N真'],
            [u'##', u'2N偽'],
            [u'フラグ和確認', u'@set', u'$$tmp', u'($flag=="on")+$flag2+($flag!="off")'],
            [u'', u'和＝{$$tmp}'],
        ], version=2)
        self.send_reset()
        self.send_message(u'単独フラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"フラグOFF")
        self.send_message(u'ｏｎ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"コマンド＝on, フラグ＝on")
        self.send_message(u'単独フラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"フラグON")
        self.send_message(u'フラグ1条件分岐')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"真")
        self.send_message(u'ｏｆｆ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"コマンド＝off, フラグ＝off")
        self.send_message(u'単独フラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"フラグOFF")
        self.send_message(u'フラグ1条件分岐')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"偽")
        self.send_message(u'複合フラグ確認')
        self.assertEqual(len(self.messages), 0)
        self.send_message(u'フラグ2条件分岐')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"2偽")
        self.send_message(u'フラグ2NOT条件分岐')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"2N真")
        self.send_message(u'フラグ和確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"和＝0")
        self.send_message(u'フラグ2オン')
        self.send_message(u'フラグ2条件分岐')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"2真")
        self.send_message(u'フラグ2NOT条件分岐')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"2N偽")
        self.send_message(u'フラグ和確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"和＝1")
        self.send_message(u'複合フラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"ON and OFF")
        self.send_message(u'ORフラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"ON or ON")
        self.send_message(u'NOTフラグ確認')
        self.assertEqual(len(self.messages), 0)
        self.send_message(u'on')
        self.send_message(u'ORフラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"ON or ON")
        self.send_message(u'NOTフラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"ON and ON")
        self.send_message(u'フラグ和確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"和＝3")
        self.send_message(u'フラグ2オフ')
        self.send_message(u'フラグ2条件分岐')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"2偽")
        self.send_message(u'フラグ2NOT条件分岐')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"2N真")
        self.send_message(u'ORフラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"ON or ON")
        self.send_message(u'NOTフラグ確認')
        self.assertEqual(len(self.messages), 0)
        self.send_message(u'フラグ和確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"和＝2")
        self.send_message(u'off')
        self.send_message(u'ORフラグ確認')
        self.assertEqual(len(self.messages), 0)
        self.send_message(u'NOTフラグ確認')
        self.assertEqual(len(self.messages), 0)
        self.send_message(u'フラグ和確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"和＝0")
        self.send_message(u'on')
        self.send_message(u'ORフラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"ON or ON")
        self.send_message(u'NOTフラグ確認')
        self.assertEqual(len(self.messages), 0)
        self.send_message(u'フラグ和確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"和＝2")

    def test_expression_v2(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_table([
            [u'/^on$/', u'@set', u'$$F1', u'on'],
            [u'/^off$/', u'@set', u'$$F1', u'off'],
            [u'/^ON$/', u'@set', u'$$F2', u'ON'],
            [u'/^OFF$/', u'@set', u'$$F2', u'OFF'],
            [u'/^true$/', u'@set', u'$$F3', u'true'],
            [u'/^false$/', u'@set', u'$$F3', u'false'],
            [u'/^TRUE$/', u'@set', u'$$F4', u'TRUE'],
            [u'/^FALSE$/', u'@set', u'$$F4', u'FALSE'],
            [u'/^\d+$/', u'@set', u'$$number_1', u'+$0'],
            [u'/^N2=(\d+)$/', u'@set', u'$$number_2', u'+$1'],
            [u'/^(N3)(=)(\d+)$/', u'@set', u'$$number_3', u'+$3'],
            [u'/^S1=(.*)$/', u'@set', u'$$StrOne', u'$1'],
            [u'/^S2=(.*)$/', u'@set', u'$$StrTwo', u'$1'],
            [u'BOOL1', u'@set', u'$$tmp', u'$$F1&&$$F2&&$$F3&&$$F4'],
            [u'', u'{$$tmp}'],
            [u'', u'@set', u'$$tmp', u'($$F1&&$$F2)&&($$F3&&$$F4)'],
            [u'', u'{$$tmp}'],
            [u'BOOL2', u'@set', u'$$tmp', u'$$F1||$$F2||$$F3||$$F4'],
            [u'', u'{$$tmp}'],
            [u'', u'@set', u'$$tmp', u'$$F1||($$F2||$$F3)||$$F4'],
            [u'', u'{$$tmp}'],
            [u'BOOL3', u'@set', u'$$tmp', u'$$F1+$$F2+$$F3+$$F4'],
            [u'', u'{$$tmp}'],
            [u'', u'@set', u'$$tmp', u'($$F1+($$F2+($$F3+($$F4))))'],
            [u'', u'{$$tmp}'],
            [u'NUMBER1', u'@set', u'$$tmp', u'$$number_1+$$number_2+$$number_3'],
            [u'', u'{$$tmp}'],
            [u'', u'@set', u'$$tmp', u'$$number_1+($$number_2+$$number_3)'],
            [u'', u'{$$tmp}'],
            [u'', u'@set', u'$$tmp', u'+(+($$number_1+$$number_2)+$$number_3)'],
            [u'', u'{$$tmp}'],
            [u'NUMBER2', u'@set', u'$$tmp', u'$$number_1-$$number_2-$$number_3'],
            [u'', u'{$$tmp}'],
            [u'', u'@set', u'$$tmp', u'$$number_1+(-$$number_2-$$number_3)'],
            [u'', u'{$$tmp}'],
            [u'', u'@set', u'$$tmp', u'-(-($$number_1-$$number_2)+$$number_3)'],
            [u'', u'{$$tmp}'],
        ], version=2)
        self.send_reset()
        self.send_message(u'BOOL1')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"0")
        self.assertEqual(self.messages[1], u"0")
        self.send_message(u'BOOL2')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"0")
        self.assertEqual(self.messages[1], u"0")
        self.send_message(u'BOOL3')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"0")
        self.assertEqual(self.messages[1], u"0")
        self.send_message(u'on')
        self.send_message(u'BOOL1')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"0")
        self.assertEqual(self.messages[1], u"0")
        self.send_message(u'BOOL2')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"1")
        self.assertEqual(self.messages[1], u"1")
        self.send_message(u'BOOL3')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"1")
        self.assertEqual(self.messages[1], u"1")
        self.send_message(u'ON')
        self.send_message(u'BOOL1')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"0")
        self.assertEqual(self.messages[1], u"0")
        self.send_message(u'BOOL2')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"1")
        self.assertEqual(self.messages[1], u"1")
        self.send_message(u'BOOL3')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"2")
        self.assertEqual(self.messages[1], u"2")
        self.send_message(u'true')
        self.send_message(u'BOOL1')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"0")
        self.assertEqual(self.messages[1], u"0")
        self.send_message(u'BOOL2')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"1")
        self.assertEqual(self.messages[1], u"1")
        self.send_message(u'BOOL3')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"3")
        self.assertEqual(self.messages[1], u"3")
        self.send_message(u'TRUE')
        self.send_message(u'BOOL1')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"1")
        self.assertEqual(self.messages[1], u"1")
        self.send_message(u'BOOL2')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"1")
        self.assertEqual(self.messages[1], u"1")
        self.send_message(u'BOOL3')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"4")
        self.assertEqual(self.messages[1], u"4")
        self.send_message(u'false')
        self.send_message(u'BOOL1')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"0")
        self.assertEqual(self.messages[1], u"0")
        self.send_message(u'BOOL2')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"1")
        self.assertEqual(self.messages[1], u"1")
        self.send_message(u'BOOL3')
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0], u"3")
        self.assertEqual(self.messages[1], u"3")

        self.send_message(u'NUMBER1')
        self.assertEqual(len(self.messages), 3)
        self.assertEqual(self.messages[0], u"0")
        self.assertEqual(self.messages[1], u"0")
        self.assertEqual(self.messages[2], u"0")
        self.send_message(u'NUMBER2')
        self.assertEqual(len(self.messages), 3)
        self.assertEqual(self.messages[0], u"0")
        self.assertEqual(self.messages[1], u"0")
        self.assertEqual(self.messages[2], u"0")
        self.send_message(u'12')
        self.send_message(u'NUMBER1')
        self.assertEqual(len(self.messages), 3)
        self.assertEqual(self.messages[0], u"12")
        self.assertEqual(self.messages[1], u"12")
        self.assertEqual(self.messages[2], u"12")
        self.send_message(u'NUMBER2')
        self.assertEqual(len(self.messages), 3)
        self.assertEqual(self.messages[0], u"12")
        self.assertEqual(self.messages[1], u"12")
        self.assertEqual(self.messages[2], u"12")
        self.send_message(u'N2=3')
        self.send_message(u'NUMBER1')
        self.assertEqual(len(self.messages), 3)
        self.assertEqual(self.messages[0], u"15")
        self.assertEqual(self.messages[1], u"15")
        self.assertEqual(self.messages[2], u"15")
        self.send_message(u'NUMBER2')
        self.assertEqual(len(self.messages), 3)
        self.assertEqual(self.messages[0], u"9")
        self.assertEqual(self.messages[1], u"9")
        self.assertEqual(self.messages[2], u"9")
        self.send_message(u'N3=10')
        self.send_message(u'NUMBER1')
        self.assertEqual(len(self.messages), 3)
        self.assertEqual(self.messages[0], u"25")
        self.assertEqual(self.messages[1], u"25")
        self.assertEqual(self.messages[2], u"25")
        self.send_message(u'NUMBER2')
        self.assertEqual(len(self.messages), 3)
        self.assertEqual(self.messages[0], u"-1")
        self.assertEqual(self.messages[1], u"-1")
        self.assertEqual(self.messages[2], u"-1")



    def test_new_chapter_v2(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_table([
            [u'フラグON', u'@set', u'$$flag', u'True'],
            [u'', u'フラグ＝{$$flag}'],
            [u'新章', u'@new_chapter'],
            [u'メッセージ', u'@seq', u'##', u'##2'],
            [u'dummy', u'ダミー'],
            [u'##', u'メッセージ1回目'],
            [u'#dummy', u'ダミー'],
            [u'##', u'メッセージ2回目以降'],
            [u'条件分岐', u'@if', u'$$flag', u'##', u'##2'],
            [u'dummy', u'ダミー'],
            [u'##', u'真'],
            [u'#dummy', u'ダミー'],
            [u'##', u'偽'],
            [u'[!$$flag]フラグ確認', u'フラグOFF'],
            [u'[$$flag] フラグ確認', u'フラグON'],
        ], version=2)
        self.send_reset()
        self.send_message(u'フラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"フラグOFF")
        self.send_message(u'条件分岐')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"偽")
        self.send_message(u'フラグON')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"フラグ＝1")
        self.send_message(u'フラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"フラグON")
        self.send_message(u'条件分岐')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"真")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"メッセージ1回目")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"メッセージ2回目以降")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"メッセージ2回目以降")
        self.send_message(u'新章')
        self.send_message(u'フラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"フラグOFF")
        self.send_message(u'条件分岐')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"偽")
        self.send_message(u'フラグON')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"フラグ＝1")
        self.send_message(u'フラグ確認')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"フラグON")
        self.send_message(u'条件分岐')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"真")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"メッセージ1回目")
        self.send_message(u'メッセージ')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"メッセージ2回目以降")

    def test_forward(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
            (u'default', [
                [u'forward', u'@転送', u'testbot2', u'#転送'],
                [u'', u'転送をいたしました。'],
            ]),
        ])
        self.test_bot2.scenario = ScenarioBuilder.build_from_tables([
            (u'default', [
                [u'#転送', u'転送されてきました。'],
            ]),
        ])
        self.send_reset()
        self.send_reset2()
        self.send_message(u'forward')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"転送をいたしました。")
        self.assertEqual(len(self.forwarded_messages), 1)
        self.assertEqual(self.forwarded_messages[0], u"転送されてきました。")

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
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"追加しました。")
        self.send_group_message(u'all', u'from_api')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"WebAPI経由での呼び出し")
        self.send_message(u'group_del')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"削除しました。")
        self.send_group_message(u'all', u'from_api')
        self.assertEqual(len(self.messages), 0)
        self.send_message(u'group_add')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"追加しました。")
        self.send_group_message(u'all', u'from_api')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"WebAPI経由での呼び出し")
        self.send_message(u'group_clear')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"クリアしました。")
        self.send_group_message(u'all', u'from_api')
        self.assertEqual(len(self.messages), 0)

    def test_common_object(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
            (u'default', [
                [u'id', u'ID: {Core.uid}'],
                [u'scene', u'Scene: {Core.scene}'],
                [u'*NewScene', u'Scene: {Core.scene}'],
            ]),
        ])
        self.send_reset()
        self.send_message(u'id')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"ID: plaintext:0001")
        self.send_message(u'*NewScene')
        self.send_message(u'scene')
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], u"Scene: default/NewScene")

    def test_timestamp(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
            (u'default', [
                [u'timestamp', u'DateTime: {TimeStamp.now}'],
                [u'', u'@log', u'DateTime', u'{TimeStamp.datetime}'],
            ]),
        ])
        self.send_reset()
        self.send_message(u'timestamp')
        self.assertEqual(len(self.messages), 1)
        # TODO: テストする
        # self.assertEqual(self.messages[0], u"DateTime:")

    def try_lint(self, table):
        try:
            ScenarioBuilder.build_from_table(table)
            return None
        except ScenarioSyntaxError as e:
            return unicode(e)

    def test_lint1(self):
        pass
        # TODO: 標準コマンドの引数エラー系のテストを追加する


if __name__ == '__main__':
    unittest.main()
