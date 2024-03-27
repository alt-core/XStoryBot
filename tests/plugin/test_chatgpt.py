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
        'chatgpt': {
            'api_key': '<<CHATGPT_APIKEY>>',
            'model': 'gpt-3.5-turbo',
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


class MainTestCase(BotTestCaseBase):
    def test_chatgpt(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_table([
            [u'/(.*)/', u'@chatgpt', u'関西弁のさっぱりした性格の女子高校生としてロールプレイをして返事をしてください。', u'{0}'],
        ], options={'force': True}, version=1)
        self.send_reset()
        self.send_message(u'じゃんけんしよう')
        self.assertEqual(len(self.messages), 1)
        print(self.messages[0])
        self.send_message(u'ぱー')
        self.assertEqual(len(self.messages), 1)
        print(self.messages[0])

    def test_chatgpt_history(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_table([
            [u'/(.*)/', u'@chatgpt', u'関西弁のさっぱりした性格の女子高校生としてロールプレイをして返事をしてください。', u'{0}', u'$history'],
        ], options={'force': True}, version=1)
        self.send_reset()
        self.send_message(u'じゃんけんしよう')
        self.assertEqual(len(self.messages), 1)
        print(self.messages[0])
        self.send_message(u'ぱー')
        self.assertEqual(len(self.messages), 1)
        print(self.messages[0])


if __name__ == '__main__':
    unittest.main()
