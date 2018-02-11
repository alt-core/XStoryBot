# coding: utf-8
import logging

from bottle import request, response, Bottle, abort
from linebot.exceptions import InvalidSignatureError

import auth
import utility
import xmbot
import users


app = Bottle()


def abort_json(code, msg):
    abort(code, utility.make_error_json(code, msg))


@app.post('/line/callback/<bot_name>')
def callback(bot_name):
    response.content_type = 'text/plain; charset=UTF-8'

    bot = xmbot.get_bot(bot_name)
    if not bot:
        abort_json(404, u'not found')

    interface = bot.get_interface('line')
    if interface is None:
        abort_json(404, u'not found')

    signature = request.headers['X-Line-Signature']
    body = request.body.read().decode('utf-8')
    logging.info("Request body: " + body)

    try:
        events = interface.parser.parse(body, signature)
        for event in events:
            context = interface.create_context_from_line_event(event)
            bot.handle_action(context)
    except InvalidSignatureError:
        abort_json(401, u'invalid signature')

    return utility.make_ok_json(u'OK')


