# coding: utf-8
"""LINE Messaging API の呼出し。reply、push、rich menuの管理と紐付けを扱う。

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
    def __init__(self, channel_access_token, timeout=DEFAULT_TIMEOUT, endpoint=API_ENDPOINT, data_endpoint='https://api-data.line.me'):
        self._headers = {
            'Authorization': f'Bearer {channel_access_token}',
            'Content-Type': 'application/json',
        }
        self._timeout = timeout
        self._endpoint = endpoint
        self._data_endpoint = data_endpoint

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


    def _request(self, method, url, *, body=None, data=None, content_type=None, timeout=None):
        headers = dict(self._headers)
        if content_type:
            headers['Content-Type'] = content_type
        response = requests.request(method, url, headers=headers,
                                    data=json.dumps(body) if body is not None else data,
                                    timeout=timeout or self._timeout)
        if not 200 <= response.status_code < 300:
            raise LineApiError.from_response(response)
        return response.json() if response.content else {}

    def create_rich_menu(self, menu, timeout=None):
        return self._request('POST', self._endpoint + '/v2/bot/richmenu',
                             body=menu, timeout=timeout)['richMenuId']

    def upload_rich_menu_image(self, rich_menu_id, data, content_type, timeout=None):
        self._request('POST', self._data_endpoint + f'/v2/bot/richmenu/{rich_menu_id}/content',
                      data=data, content_type=content_type, timeout=timeout)

    def get_rich_menu(self, rich_menu_id, timeout=None):
        try:
            return self._request('GET', self._endpoint + f'/v2/bot/richmenu/{rich_menu_id}', timeout=timeout)
        except LineApiError as error:
            if error.status_code == 404:
                return None
            raise

    def get_default_rich_menu_id(self, timeout=None):
        try:
            return self._request('GET', self._endpoint + '/v2/bot/user/all/richmenu', timeout=timeout)['richMenuId']
        except LineApiError as error:
            if error.status_code == 404:
                return None
            raise

    def set_default_rich_menu(self, rich_menu_id, timeout=None):
        self._request('POST', self._endpoint + f'/v2/bot/user/all/richmenu/{rich_menu_id}', timeout=timeout)
