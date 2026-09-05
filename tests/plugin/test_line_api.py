# coding: utf-8
"""plugin.line.api（LINE Messaging API の呼出し）の契約。"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from plugin.line import api


def _response(status_code, body=None, headers=None, text=''):
    def json_():
        if body is None:
            raise ValueError('not json')
        return body
    return SimpleNamespace(
        status_code=status_code, headers=headers or {}, json=json_,
        text=text if body is None else json.dumps(body))


class LineApiClientTest(unittest.TestCase):
    def setUp(self):
        self.client = api.LineApiClient('access-token')
        self.posts = []

        def post(url, headers=None, data=None, timeout=None):
            self.posts.append({'url': url, 'headers': headers, 'data': data, 'timeout': timeout})
            return self.responses.pop(0)

        self.responses = [_response(200, {})]
        patcher = patch.object(api.requests, 'post', post)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_replyはreplyTokenとmessagesを送りretry_keyを付けない(self):
        self.client.reply('reply-token', [{'type': 'text', 'text': 'こんにちは'}])

        post = self.posts[0]
        self.assertEqual('https://api.line.me/v2/bot/message/reply', post['url'])
        self.assertEqual({
            'Authorization': 'Bearer access-token',
            'Content-Type': 'application/json',
        }, post['headers'])
        self.assertEqual({
            'replyToken': 'reply-token',
            'messages': [{'type': 'text', 'text': 'こんにちは'}],
            'notificationDisabled': False,
        }, json.loads(post['data']))
        self.assertEqual((30, 30), post['timeout'])

    def test_pushはその呼出しだけにretry_keyを付ける(self):
        self.responses = [_response(200, {}), _response(200, {})]
        self.client.push('U1', [{'type': 'text', 'text': 'a'}], retry_key='key-1')
        self.client.push('U2', [{'type': 'text', 'text': 'b'}])

        self.assertEqual('https://api.line.me/v2/bot/message/push', self.posts[0]['url'])
        self.assertEqual('key-1', self.posts[0]['headers']['X-Line-Retry-Key'])
        self.assertEqual({'to': 'U1', 'messages': [{'type': 'text', 'text': 'a'}],
                          'notificationDisabled': False}, json.loads(self.posts[0]['data']))
        self.assertNotIn('X-Line-Retry-Key', self.posts[1]['headers'])

    def test_rich_menuの紐付けは本文なしでPOSTする(self):
        self.client.link_rich_menu('U1', 'richmenu-1')

        self.assertEqual(
            'https://api.line.me/v2/bot/user/U1/richmenu/richmenu-1', self.posts[0]['url'])
        self.assertIsNone(self.posts[0]['data'])

    def test_2xx以外はLineApiErrorになりstatusとLINEのエラー内容を持つ(self):
        self.responses = [_response(400, {
            'message': 'The request body has 1 error(s)',
            'details': [{'message': 'May not be empty', 'property': 'messages[0].text'}],
        }, headers={'X-Line-Request-Id': 'req-1'})]

        with self.assertRaises(api.LineApiError) as captured:
            self.client.reply('reply-token', [{'type': 'text', 'text': ''}])

        error = captured.exception
        self.assertEqual(400, error.status_code)
        self.assertEqual('The request body has 1 error(s)', error.message)
        self.assertEqual('req-1', error.request_id)
        self.assertEqual('messages[0].text', error.details[0]['property'])
        self.assertIn('status=400', str(error))
        self.assertIn('req-1', str(error))

    def test_JSONでないエラー本文でもstatusを保つ(self):
        self.responses = [_response(502, None, text='<html>Bad Gateway</html>')]

        with self.assertRaises(api.LineApiError) as captured:
            self.client.push('U1', [])

        self.assertEqual(502, captured.exception.status_code)
        self.assertEqual('<html>Bad Gateway</html>', captured.exception.message)
        self.assertEqual([], captured.exception.details)

    def test_通信例外はrequestsの例外のまま伝わる(self):
        with patch.object(api.requests, 'post', side_effect=requests.ConnectionError('down')):
            with self.assertRaises(requests.ConnectionError):
                self.client.reply('reply-token', [])

    def test_endpointとtimeoutは差し替えられる(self):
        client = api.LineApiClient('token', timeout=(5, 10), endpoint='http://127.0.0.1:8080')
        client.reply('reply-token', [])
        self.assertEqual('http://127.0.0.1:8080/v2/bot/message/reply', self.posts[0]['url'])
        self.assertEqual((5, 10), self.posts[0]['timeout'])


if __name__ == '__main__':
    unittest.main()
