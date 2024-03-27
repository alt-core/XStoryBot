# coding: utf-8
import logging
import time
import json

from bottle import request, response, Bottle, abort

import auth
import utility
import main
import users

# import hmac
# import hashlib
# import base64

from google.appengine.api import urlfetch


app = Bottle()


def abort_json(code, msg):
    abort(code, utility.make_error_json(code, msg))


@app.route('/liff/<bot_name>/message', method=['OPTIONS'])
def cors(bot_name):
    bot = main.get_bot(bot_name)
    if not bot:
        abort_json(404, u'not found')

    interface = bot.get_interface('liff')
    if interface is None:
        abort_json(404, u'not found')

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
        abort_json(404, u'not found')

    interface = bot.get_interface('liff')
    if interface is None:
        abort_json(404, u'not found')

    response.headers['Access-Control-Allow-Origin'] = interface.allow_origin # liff と API サーバが異なる CORS 対応

    auth_header = request.headers.get('Authorization')
    access_token = auth_header.split(' ')[1] if auth_header else None
    # access_token = request.query.access_token
    
    if not access_token:
        response.status = 400
        return "Access token is required"

    profile_response = urlfetch.fetch(
        'https://api.line.me/v2/profile',
        method=urlfetch.GET,
        #payload=json.dumps(data, ensure_ascii=False).encode('utf-8'),
        headers={
            'Content-Type': 'application/json; charset=UTF-8',
            'Authorization': 'Bearer {}'.format(access_token),
        },
        deadline=120,
    )
    if profile_response.status_code != 200:
        logging.error(u'Failed to request LINE API: {0}'.format(profile_response.status_code))
        response.status = profile_response.status_code
        return 'Failed to request LINE API'
    profile_json = json.loads(profile_response.content)
    try:
        user_id = profile_json['userId']
    except Exception:
        logging.error(u'Failed to parse response of LINE API: {0}'.format(profile_json))
        response.status = 500
        return 'Failed to parse response of LINE API'

    bot.check_reload()

    data = request.json
    if data is None or data.get('action') is None:
        response.status = 400
        return 'Bad Request'

    attrs = {}
    action = "##liff." + utility.sanitize_action(data['action'])

    user = users.User("line", 'user' + ',' + user_id)

    logging.info(u'LIFF send_message: {} {}'.format(user_id, action))

    context = interface.create_context(user, action, attrs)
    if context is not None:
        result = bot.handle_action(context)
        logging.info(u'LIFF result: {}'.format(result))
        return utility.make_ok_json(result)
    else:
        return utility.make_ng_json(u'Failed to create context')


