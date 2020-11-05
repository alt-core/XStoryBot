# coding: utf-8
import logging

from bottle import request, response, Bottle, abort

import main
import users
import auth


app = Bottle()


def twilio_callback_sub(bot_name, from_tel, to_tel, is_voicecall, message):

    body = request.body.read().decode('utf-8')
    logging.info("Twilio callback: " + body)

    bot = main.get_bot(bot_name)
    if not bot:
        abort(404)

    interface = bot.get_interface('twilio')
    if interface is None:
        abort(404)

    bot.check_reload()

    response.content_type = 'text/xml; charset=UTF-8'

    if not from_tel.startswith(u'+81'):
        return u'<?xml version="1.0" encoding="UTF-8"?>' \
               u'<Response>' \
               u'<Say language="ja-jp" voice="woman">' \
               u'番号非通知の通話は、お受けできません。おてすうですが、電話番号を通知して、おかけ直しください' \
               u'</Say>' \
               u'<Reject reason="rejected"></Reject>' \
               u'</Response>'

    context = interface.create_context_from_twilio_event(from_tel, to_tel, is_voicecall, message)
    return bot.handle_action(context)

@app.post('/twilio/callback/<bot_name>')
def twilio_callback(bot_name):
    if request.params.getunicode('Message'):
        return 'OK'
    from_tel = request.params.getunicode('From')
    to_tel = request.params.getunicode('To')
    is_voicecall = request.params.getunicode('CallSid') is not None
    token = request.params.getunicode('token')
    if is_voicecall:
        # Gather で音声認識した場合のみ
        message = request.params.getunicode('SpeechResult')
    else:
        # SMS の本文
        message = request.params.getunicode('Body')

    # token チェック
    if not auth.check_token(token):
        abort(401)

    return twilio_callback_sub(bot_name, from_tel, to_tel, is_voicecall, message)

# @dial コマンド利用時のみの特殊なコールバック呼び出し
# この endpoint を Twilio 側に設定する必要は無い
@app.post('/twilio/dial_content/<bot_name>/<message>')
def twilio_dial_content(bot_name, message):
    # Outbound のダイアル時なので、From と To が逆になる
    from_tel = request.params.getunicode('To')
    to_tel = request.params.getunicode('From')
    is_voicecall = True
    token = request.params.getunicode('token')

    # token チェック
    if not auth.check_token(token):
        abort(401)

    return twilio_callback_sub(bot_name, from_tel, to_tel, is_voicecall, message)

# @dial コマンドの完了通知のみの特殊なコールバック呼び出し
# この endpoint を Twilio 側に設定する必要は無い
@app.post('/twilio/dial_completed_callback/<bot_name>/<message>')
def twilio_dial_content(bot_name, message):
    # Outbound のダイアル時なので、From と To が逆になる
    from_tel = request.params.getunicode('To')
    to_tel = request.params.getunicode('From')
    is_voicecall = True

    token = request.params.getunicode('token')
    # token チェック
    if not auth.check_token(token):
        abort(401)

    call_status = request.params.getunicode('CallStatus')
    if call_status == u'completed':
        duration = request.params.getunicode('CallDuration')
        if duration is not None and int(duration) > 1:
            action = message + u':OK'
        else:
            # 会話時間が1秒以下の場合は NG 扱い
            action = message + u':NG'
    else:
        # 話し中・失敗・電話に出ないなど
        action = message + u':NG'

    return twilio_callback_sub(bot_name, from_tel, to_tel, is_voicecall, action)

# @delay コマンド利用時のみの task queue からのコールバック
@app.post('/twilio/internal_callback/<bot_name>')
def twilio_internal_callback(bot_name):
    from_tel = request.params.getunicode('From')
    to_tel = request.params.getunicode('To')
    is_voicecall = request.params.getunicode('CallSid') is not None
    message = request.params.getunicode('Message')

    token = request.params.getunicode('token')
    # token チェック
    if not auth.check_token(token):
        abort(401)

    return twilio_callback_sub(bot_name, from_tel, to_tel, is_voicecall, message)
