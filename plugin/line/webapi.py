# coding: utf-8
import logging
import time
import json

from bottle import request, response, Bottle, abort
from linebot.exceptions import InvalidSignatureError

import auth
import utility
import main
import users

# import hmac
# import hashlib
# import base64


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

    signature = request.headers['X-Line-Signature']
    body = request.body.read().decode('utf-8')
    # logging.info(u"Signature: %s" % signature)
    # gen_signature = base64.b64encode(hmac.new(
    #     interface.line_channel_secret,
    #     body.encode('utf-8'),
    #     hashlib.sha256
    # ).digest())
    # logging.info(u"Gen-Signature: %s" % gen_signature)
    logging.info(u'Request body: {}'.format(body))
    #logging.info(u'Headers: {}'.format(repr(request.environ)))

    try:
        events = interface.parser.parse(body, signature)

        bot.check_reload()

        if interface.line_abort_duration_ms > 0 and len(events) > 0:
            # GAE のスピンアップが遅くて、LINE の ReplyToken の期限に間に合いそうになかったら実行前に中断する
            timestamp = events[0].timestamp
            if timestamp is not None:
                current = int(time.time() * 1000)
                diff = current - timestamp
                #logging.info(u'timestamp: {}, current: {}, diff: {}'.format(timestamp, current, diff))
                if diff > interface.line_abort_duration_ms:
                    logging.warning(u'[LINE] GAE spin-up is too late; aborted: {}'.format(diff))
                    abort_json(504, u'Timeout')

        for event in events:
            context = interface.create_context_from_line_event(event)
            bot.handle_action(context)
    except InvalidSignatureError:
        abort_json(401, u'invalid signature')

    return utility.make_ok_json(u'OK')


