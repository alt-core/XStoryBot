# coding: utf-8
import logging

from bottle import request, response, Bottle, HTTPResponse

import utility
import main
import users
from aws_request_context import get_lambda_deadline
from plugin.liff.id_token import InvalidIdTokenError, IdTokenKeysError, verify_id_token

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
    id_token = header_parts[1] if len(header_parts) == 2 and header_parts[0].lower() == 'bearer' else None

    if not id_token:
        response.status = 400
        return "ID token is required"

    data = request.json
    if not isinstance(data, dict) or not isinstance(data.get('action'), str):
        response.status = 400
        return 'Bad Request'

    if not interface.login_channel_id:
        abort_json(503, 'LIFF login_channel_id is not configured')
    try:
        user_id = verify_id_token(id_token, interface.login_channel_id,
                                  interface.id_token_max_age_seconds)
    except InvalidIdTokenError:
        # tokenや未検証claimsを、応答・ログへ出さない。
        abort_json(401, 'Invalid LINE ID token')
    except IdTokenKeysError:
        abort_json(502, 'LINE authentication keys are unavailable')

    deadline = get_lambda_deadline()

    attrs = {}
    action = interface.action_prefix + data['action']

    user = users.User("line", f'user,{user_id}')

    logging.info(f'LIFF send_message: {user_id} {action}')

    context = interface.create_context(user, action, attrs)
    if context is not None:
        context.action_deadline = deadline
        result = bot.handle_action(context)
        logging.info(f'LIFF result: {result}')
        if result is not None:
            return utility.make_ok_json(result)
        else:
            return utility.make_ng_json('Error occurred')
    else:
        return utility.make_ng_json('Failed to create context')
