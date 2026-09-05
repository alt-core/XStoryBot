# coding: utf-8
"""plugin.line.webhook（署名検証と event 取り出し）の契約。"""

import base64
import hashlib
import hmac
import json
import unittest

from plugin.line import webhook


SECRET = 'channel-secret'


def _sign(body):
    if isinstance(body, str):
        body = body.encode('utf-8')
    return base64.b64encode(
        hmac.new(SECRET.encode('utf-8'), body, hashlib.sha256).digest()).decode('ascii')


class VerifySignatureTest(unittest.TestCase):
    def test_正しい署名はstrでもbytesでも一致する(self):
        body = '{"events":[{"type":"message","message":{"type":"text","text":"日本語"}}]}'
        self.assertTrue(webhook.verify_signature(SECRET, body, _sign(body)))
        self.assertTrue(webhook.verify_signature(SECRET, body.encode('utf-8'), _sign(body)))

    def test_本文や鍵が違えば一致しない(self):
        body = '{"events":[]}'
        self.assertFalse(webhook.verify_signature(SECRET, body + ' ', _sign(body)))
        self.assertFalse(webhook.verify_signature('other-secret', body, _sign(body)))
        self.assertFalse(webhook.verify_signature(SECRET, body, 'invalid'))
        self.assertFalse(webhook.verify_signature(SECRET, body, None))

    def test_非ASCIIの署名はTypeErrorにならず一致しない(self):
        body = '{"events":[]}'
        self.assertFalse(webhook.verify_signature(SECRET, body, 'abé'))
        self.assertFalse(webhook.verify_signature(SECRET, body, _sign(body) + 'é'))

    def test_署名はbytesでも受ける(self):
        body = '{"events":[]}'
        self.assertTrue(webhook.verify_signature(SECRET, body, _sign(body).encode('ascii')))
        self.assertFalse(webhook.verify_signature(SECRET, body, b'invalid'))


class ParseTest(unittest.TestCase):
    def test_署名が一致すればeventsをdictのまま返す(self):
        events = [
            {'type': 'message', 'replyToken': 'r1', 'timestamp': 1,
             'source': {'type': 'user', 'userId': 'U1'},
             'message': {'id': 'm1', 'type': 'text', 'text': 'こんにちは'}},
            {'type': 'membership', 'membership': {'type': 'joined'}},
        ]
        body = json.dumps({'destination': 'Uxxx', 'events': events}, ensure_ascii=False)

        self.assertEqual(events, webhook.parse(SECRET, body, _sign(body)))

    def test_eventsが無い本文は空listになる(self):
        body = '{"destination":"Uxxx"}'
        self.assertEqual([], webhook.parse(SECRET, body, _sign(body)))

    def test_署名が違えばInvalidSignatureErrorで本文を読まない(self):
        with self.assertRaises(webhook.InvalidSignatureError):
            webhook.parse(SECRET, '{"events":[]}', 'invalid')

    def test_非ASCIIの署名もInvalidSignatureErrorになる(self):
        with self.assertRaises(webhook.InvalidSignatureError):
            webhook.parse(SECRET, '{"events":[]}', 'abé')


if __name__ == '__main__':
    unittest.main()
