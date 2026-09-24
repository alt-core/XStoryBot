# coding: utf-8
import logging
import time
import json

from bottle import request, response, Bottle, HTTPResponse
import requests

import auth
import utility
import main
import users

app = Bottle()


def abort_json(code, msg):
    raise HTTPResponse(
        utility.make_error_json(code, msg), status=code,
        headers=dict(response.headers), content_type='application/json; charset=UTF-8')


def _set_cors(interface):
    allowed = interface.allow_origin
    if allowed == '*':
        response.headers['Access-Control-Allow-Origin'] = '*'
        return
    response.headers['Vary'] = 'Origin'
    origin = request.headers.get('Origin')
    origins = allowed if isinstance(allowed, list) else [allowed]
    if origin in origins:
        response.headers['Access-Control-Allow-Origin'] = origin
    elif origin:
        abort_json(403, 'Origin is not allowed')


def _line_json(url, **kwargs):
    try:
        result = requests.get(url, timeout=10, allow_redirects=False, **kwargs)
    except requests.RequestException:
        # verifyのURLにはtokenが入るため、例外本文やrequest URLをログへ出さない。
        abort_json(502, 'LINE authentication request failed')
    if result.status_code != 200:
        logging.warning('LINE authentication failed: HTTP %s', result.status_code)
        abort_json(401 if result.status_code in (400, 401, 403) else 502,
                   'LINE authentication failed')
    try:
        value = result.json()
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except ValueError:
        abort_json(502, 'Invalid LINE authentication response')


@app.route('/liff/<bot_name>/message', method=['OPTIONS'])
def cors(bot_name):
    bot = main.get_bot(bot_name)
    if not bot:
        abort_json(404, 'not found')

    interface = bot.get_interface('liff')
    if interface is None:
        abort_json(404, 'not found')

    _set_cors(interface)
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS' # 許可するHTTPメソッド
    response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type' # 許可するヘッダー
    response.headers['Access-Control-Max-Age'] = '3600' # ブラウザがプリフライトレスポンスをキャッシュする時間（秒）
    return ''


@app.post('/liff/<bot_name>/message')
def send_message(bot_name):
    response.content_type = 'text/plain; charset=UTF-8'

    bot = main.get_bot(bot_name)
    if not bot:
        abort_json(404, 'not found')

    interface = bot.get_interface('liff')
    if interface is None:
        abort_json(404, 'not found')

    _set_cors(interface)

    auth_header = request.headers.get('Authorization')
    header_parts = auth_header.split() if auth_header else []
    access_token = header_parts[1] if len(header_parts) == 2 and header_parts[0].lower() == 'bearer' else None

    if not access_token:
        response.status = 400
        return "Access token is required"

    data = request.json
    if not isinstance(data, dict) or not isinstance(data.get('action'), str):
        response.status = 400
        return 'Bad Request'

    if not interface.login_channel_id:
        abort_json(503, 'LIFF login_channel_id is not configured')
    verified = _line_json('https://api.line.me/oauth2/v2.1/verify',
                          params={'access_token': access_token})
    expires = verified.get('expires_in')
    if (verified.get('client_id') != interface.login_channel_id
            or type(expires) not in (int, float) or not expires > 0):
        abort_json(401, 'Invalid access token channel or expiry')
    profile_json = _line_json(
        'https://api.line.me/v2/profile',
        headers={
            'Content-Type': 'application/json; charset=UTF-8',
            'Authorization': f'Bearer {access_token}',
        },
    )
    user_id = profile_json.get('userId')
    if not isinstance(user_id, str) or not user_id:
        abort_json(502, 'Invalid LINE profile response')

    bot.check_reload()

    attrs = {}
    action = interface.action_prefix + data['action']

    user = users.User("line", f'user,{user_id}')

    logging.info(f'LIFF send_message: {user_id} {action}')

    context = interface.create_context(user, action, attrs)
    if context is not None:
        result = bot.handle_action(context)
        logging.info(f'LIFF result: {result}')
        if result is not None:
            return utility.make_ok_json(result)
        else:
            return utility.make_ng_json('Error occurred')
    else:
        return utility.make_ng_json('Failed to create context')
