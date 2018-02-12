# coding: utf-8
from __future__ import absolute_import
import unittest

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
        'twilio': {},
        'google_sheets': {},
    },

    'BOTS': {
        'testbot': {
            'interfaces': [{
                'type': 'twilio',
                'params': {
                    'twilio_sid': '<<TWILIO_SID>>',
                    'twilio_auth_token': '<<TWILIO_AUTH_TOKEN>>',
                    'dial_from': '<<TEL_FOR_DIAL>>',
                    'sms_from': '<<TEL_FOR_SMS_SEND>>',
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
                'type': 'twilio',
                'params': {
                    'twilio_sid': '<<TWILIO_SID>>',
                    'twilio_auth_token': '<<TWILIO_AUTH_TOKEN>>',
                    'dial_from': '<<TEL_FOR_DIAL>>',
                    'sms_from': '<<TEL_FOR_SMS_SEND>>',
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
import common_commands
import auth
import webapi
import plugin.twilio.webapi
from scenario import ScenarioBuilder

from tests.test import reinitialize_bot, DummyScenarioLoader, dummy_send_request_factory


class TwilioPluginTestCase(unittest.TestCase):

    def setUp(self):
        reinitialize_bot(BOT_SETTINGS)
        self.app_twilio = TestApp(plugin.twilio.webapi.app)
        self.app = TestApp(webapi.app)
        common_commands.send_request = dummy_send_request_factory(self, self.app)
        self.test_bot_loader = DummyScenarioLoader()
        self.test_bot = main.get_bot('testbot')
        self.test_bot.scenario_loader = self.test_bot_loader
        self.test_bot2_loader = DummyScenarioLoader()
        self.test_bot2 = main.get_bot('testbot2')
        self.test_bot2.scenario_loader = self.test_bot2_loader
        self.forwarded_messages = []
        import bottle
        bottle.debug(True)

    def tearDown(self):
        pass

    def send_twilio_reset(self, from_tel):
        message = settings.OPTIONS['reset_keyword']
        res = self.app_twilio.post('/twilio/callback/testbot?token=' + auth.api_token, {'From': from_tel, 'To': '+815000000000', 'Body': message.encode('utf-8'), 'MessageSid': 'MEffffffffffffffffffffffffffffffff'})
        return res

    def send_twilio_call(self, from_tel, to_tel):
        res = self.app_twilio.post('/twilio/callback/testbot?token=' + auth.api_token, {'From': from_tel, 'To': to_tel, 'CallSid': 'CAffffffffffffffffffffffffffffffff'})
        return res

    def send_twilio_message(self, from_tel, to_tel, message):
        res = self.app_twilio.post('/twilio/callback/testbot?token=' + auth.api_token, {'From': from_tel, 'To': to_tel, 'Body': message.encode('utf-8'), 'MessageSid': 'MEffffffffffffffffffffffffffffffff'})
        return res

    def send_twilio_api_send_group(self, group, action):
        res = self.app.get('/api/v1/bots/testbot/action?user=group:'+group+'&action='+action+'&token='+auth.api_token)
        self.assertEqual(res.status, "200 OK")
        self.assertEqual(res.headers["Content-Type"], u"text/plain; charset=UTF-8")
        res_json = json.loads(res.text)
        self.assertEqual(res_json[u"code"], 200)
        self.assertEqual(res_json[u"result"], u"Success")
        return res_json[u"message"]

    def send_twilio_api_send_uid(self, from_tel, action):
        res = self.app.get('/api/v1/bots/testbot/action?user=twilio:'+from_tel+'&action='+action+'&token='+auth.api_token)
        self.assertEqual(res.status, "200 OK")
        self.assertEqual(res.headers["Content-Type"], u"text/plain; charset=UTF-8")
        res_json = json.loads(res.text)
        self.assertEqual(res_json[u"code"], 200)
        self.assertEqual(res_json[u"result"], u"Success")
        return res_json[u"message"]

    def assert_twilio_response_body(self, body, response_string):
        self.assertEqual(body, u'<?xml version="1.0" encoding="UTF-8"?><Response>{}</Response>'.format(response_string))

    def assert_twilio_response(self, res, response_string):
        self.assertEqual(str(res).decode('utf-8'), u'Response: 200 OK\nContent-Type: text/xml; charset=UTF-8\n<?xml version="1.0" encoding="UTF-8"?><Response>{}</Response>'.format(response_string))

    def assert_twilio_single_message(self, res, message):
        self.assert_twilio_response(res, u'<Message>{}</Message>'.format(message))

    def test_twilio(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
            (u'default', [
                [u'#tel:+815011111111', u'電話されました'],
                [u'#tel:+815022222222', u'<Reject reason="rejected"></Reject>'],
                [u'テキストメッセージ', u'SMSを受信しました'],
            ]),
        ])
        res = self.send_twilio_call(u'+819012345678', u'+815011111111')
        self.assert_twilio_response(res, u'<Say language="ja-jp" voice="woman">電話されました</Say>')
        res = self.send_twilio_call(u'+819012345678', u'+815022222222')
        self.assert_twilio_response(res, u'<Reject reason="rejected"></Reject>')
        res = self.send_twilio_message(u'+819012345678', u'+815011111111', u'テキストメッセージ')
        self.assert_twilio_single_message(res, u'SMSを受信しました')

    def test_forward(self):
        self.test_bot.scenario = ScenarioBuilder.build_from_tables([
            (u'default', [
                [u'forward', u'@転送', u'testbot2', u'#転送'],
                [u'', u'転送をいたしました。XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX'],
            ]),
        ])
        self.test_bot2.scenario = ScenarioBuilder.build_from_tables([
            (u'default', [
                [u'#転送', u'転送されてきました。'],
            ]),
        ])
        res = self.send_twilio_message(u'+819012345678', u'+815011111111', u'forward')
        self.assert_twilio_response_body(res.text, u'<Message>転送をいたしました。XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX</Message>')
        self.assertEqual(len(self.forwarded_messages), 1)
        self.assert_twilio_response_body(self.forwarded_messages[0], u'<Message>転送されてきました。</Message>')

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
        res = self.send_twilio_message(u'+819012345678', u'+815011111111', u'group_add')
        self.assert_twilio_single_message(res, u"追加しました。")
        body = self.send_twilio_api_send_group(u'all', u'from_api')
        self.assert_twilio_response_body(body, u"<Message>WebAPI経由での呼び出し</Message>")
        res = self.send_twilio_message(u'+819012345678', u'+815011111111', u'group_del')
        self.assert_twilio_single_message(res, u"削除しました。")
        body = self.send_twilio_api_send_group(u'all', u'from_api')
        self.assertEqual(body, u'')
        res = self.send_twilio_message(u'+819012345678', u'+815011111111', u'group_add')
        self.assert_twilio_single_message(res, u"追加しました。")
        body = self.send_twilio_api_send_group(u'all', u'from_api')
        self.assert_twilio_response_body(body, u"<Message>WebAPI経由での呼び出し</Message>")
        res = self.send_twilio_message(u'+819012345678', u'+815011111111', u'group_clear')
        self.assert_twilio_single_message(res, u"クリアしました。")
        body = self.send_twilio_api_send_group(u'all', u'from_api')
        self.assertEqual(body, u'')
        body = self.send_twilio_api_send_uid(u'+819012345678', u'from_api')
        self.assert_twilio_response_body(body, u"<Message>WebAPI経由での呼び出し</Message>")


if __name__ == '__main__':
    unittest.main()
