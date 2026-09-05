# coding: utf-8
"""LINE Messaging API の呼出し。使うのは reply、push、rich menu の紐付けの3つだけ。

https://developers.line.biz/ja/reference/messaging-api/

client は状態を持たないので、1つを複数の thread で共有してよい。
retry key は push の呼出しごとに header で渡す（reply や rich menu には付けない）。
"""
import json

import requests


API_ENDPOINT = 'https://api.line.me'
DEFAULT_TIMEOUT = (30, 30)  # (接続, 読取) 秒


class LineApiError(Exception):
    """LINE が 2xx 以外を返したときの例外。status_code で再試行の可否を判断する。"""

    def __init__(self, status_code, message, request_id=None, details=None):
        super().__init__(
            f'LINE API error: status={status_code} request_id={request_id} '
            f'message={message} details={details}')
        self.status_code = status_code
        self.message = message
        self.request_id = request_id
        self.details = details or []

    @classmethod
    def from_response(cls, response):
        # エラー本文は {"message": "...", "details": [{"message": "...", "property": "..."}]}
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {'message': response.text[:200]}
        return cls(
            response.status_code,
            body.get('message'),
            response.headers.get('X-Line-Request-Id'),
            body.get('details'))


class LineApiClient:
    def __init__(self, channel_access_token, timeout=DEFAULT_TIMEOUT, endpoint=API_ENDPOINT):
        self._headers = {
            'Authorization': f'Bearer {channel_access_token}',
            'Content-Type': 'application/json',
        }
        self._timeout = timeout
        self._endpoint = endpoint

    def reply(self, reply_token, messages):
        self._post('/v2/bot/message/reply', {
            'replyToken': reply_token,
            'messages': messages,
            'notificationDisabled': False,
        })

    def push(self, to, messages, retry_key=None):
        headers = {'X-Line-Retry-Key': retry_key} if retry_key else {}
        self._post('/v2/bot/message/push', {
            'to': to,
            'messages': messages,
            'notificationDisabled': False,
        }, headers)

    def link_rich_menu(self, user_id, rich_menu_id):
        self._post(f'/v2/bot/user/{user_id}/richmenu/{rich_menu_id}')

    def _post(self, path, body=None, headers=None):
        response = requests.post(
            self._endpoint + path,
            headers={**self._headers, **(headers or {})},
            data=json.dumps(body) if body is not None else None,
            timeout=self._timeout)
        if not 200 <= response.status_code < 300:
            raise LineApiError.from_response(response)
