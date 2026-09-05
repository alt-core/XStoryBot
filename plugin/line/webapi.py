# coding: utf-8
import logging
import time
import json

from bottle import request, response, Bottle, abort

import auth
import utility
import main
import users
from plugin.line.webhook import InvalidSignatureError


app = Bottle()


def abort_json(code, msg):
    abort(code, utility.make_error_json(code, msg))


@app.post('/line/callback/<bot_name>')
def callback(bot_name):
    response.content_type = 'text/plain; charset=UTF-8'

    bot = main.get_bot(bot_name)
    if not bot:
        abort_json(404, u'not found')

    interface = bot.get_interface('line')
    if interface is None:
        abort_json(404, u'not found')

    try:
        signature = request.headers.get('X-Line-Signature')
    except UnicodeDecodeError:
        # Bottle は header 値を UTF-8 として読み直す。UTF-8 として不正な値は署名として不正
        signature = None
    if signature is None:
        abort_json(401, u'invalid signature')

    body = request.body.read().decode('utf-8')

    try:
        events = interface.parse_webhook(body, signature)
        logging.info(u'Request body: {}'.format(body))

        bot.check_reload()

        if interface.line_abort_duration_ms > 0 and len(events) > 0:
            # Webhook受信が遅れ、ReplyTokenの期限内に応答できない場合は処理を中断する。
            timestamp = events[0].get('timestamp')
            if timestamp is not None:
                current = int(time.time() * 1000)
                diff = current - timestamp
                #logging.info(u'timestamp: {}, current: {}, diff: {}'.format(timestamp, current, diff))
                if diff > interface.line_abort_duration_ms:
                    if not interface.line_abort_duration_dont_break:
                        logging.warning(u'[LINE] webhook delivery delay exceeded limit; aborted: {}'.format(diff))
                        abort_json(504, u'Timeout')
                    else:
                        logging.warning(u'[LINE] webhook delivery delay exceeded limit; continue: {}'.format(diff))

        for event in events:
            context = interface.create_context_from_line_event(event)
            if context is not None:
                bot.handle_action(context)
    except InvalidSignatureError:
        abort_json(401, u'invalid signature')

    return utility.make_ok_json(u'OK')
