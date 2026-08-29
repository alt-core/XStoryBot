# coding: utf-8
import logging
import time
import json

from bottle import request, response, Bottle, abort
import requests

import auth
import utility
import main
import users

app = Bottle()


def abort_json(code, msg):
    abort(code, utility.make_error_json(code, msg))


@app.route('/liff/<bot_name>/message', method=['OPTIONS'])
def cors(bot_name):
    bot = main.get_bot(bot_name)
    if not bot:
        abort_json(404, 'not found')

    interface = bot.get_interface('liff')
    if interface is None:
        abort_json(404, 'not found')

    response.headers['Access-Control-Allow-Origin'] = interface.allow_origin # liff と API サーバが異なる CORS 対応
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

    response.headers['Access-Control-Allow-Origin'] = interface.allow_origin # liff と API サーバが異なる CORS 対応

    auth_header = request.headers.get('Authorization')
    access_token = auth_header.split(' ')[1] if auth_header else None
    # access_token = request.query.access_token

    if not access_token:
        response.status = 400
        return "Access token is required"

    profile_response = requests.get(
        'https://api.line.me/v2/profile',
        headers={
            'Content-Type': 'application/json; charset=UTF-8',
            'Authorization': f'Bearer {access_token}',
        },
        timeout=120,
    )
    if profile_response.status_code != 200:
        logging.error(f'Failed to request LINE API: {profile_response.status_code}')
        response.status = profile_response.status_code
        return 'Failed to request LINE API'
    profile_json = profile_response.json()
    try:
        user_id = profile_json['userId']
    except Exception:
        logging.error(f'Failed to parse response of LINE API: {profile_json}')
        response.status = 500
        return 'Failed to parse response of LINE API'

    bot.check_reload()

    data = request.json
    if data is None or data.get('action') is None:
        response.status = 400
        return 'Bad Request'

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
