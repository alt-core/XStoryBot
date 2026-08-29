# coding: utf-8

import logging

from bottle import Bottle, abort, request, response
from twilio.request_validator import RequestValidator

import auth
import main
import settings


app = Bottle()


def _get_bot_and_interface(bot_name):
    bot = main.get_bot(bot_name)
    if not bot:
        abort(404)

    interface = bot.get_interface('twilio')
    if interface is None:
        abort(404)
    return bot, interface


def _external_request_url():
    """Twilioへ登録した外部URLを、署名検証用に復元する。"""
    base_url = settings.SERVICE_SETTINGS['app']['base_url'].rstrip('/')
    raw_uri = request.environ.get('RAW_URI') or request.environ.get('REQUEST_URI')
    if raw_uri and raw_uri.startswith('/'):
        return f'{base_url}{raw_uri}'

    url = f'{base_url}{request.path}'
    if request.query_string:
        url = f'{url}?{request.query_string}'
    return url


def _require_twilio_signature(interface):
    auth_token = interface.params.get('twilio_auth_token', '')
    signature = request.headers.get('X-Twilio-Signature', '')
    if not auth_token:
        logging.error('Twilio Auth Tokenが設定されていません')
        abort(503)
    if not signature:
        abort(403)

    validator = RequestValidator(auth_token)
    if not validator.validate(
            _external_request_url(), request.forms.decode(), signature):
        abort(403)


def _require_api_token():
    if not auth.check_token(request.headers.get('X-API-Token', '')):
        abort(401)


def twilio_callback_sub(bot, interface, from_tel, to_tel, is_voicecall,
                        message):
    body = request.body.read().decode('utf-8')
    logging.info(f'Twilio callback: {body}')

    bot.check_reload()

    response.content_type = 'text/xml; charset=UTF-8'

    if not from_tel.startswith('+81'):
        return '<?xml version="1.0" encoding="UTF-8"?>' \
               '<Response>' \
               '<Say language="ja-jp" voice="woman">' \
               '番号非通知の通話は、お受けできません。おてすうですが、電話番号を通知して、おかけ直しください' \
               '</Say>' \
               '<Reject reason="rejected"></Reject>' \
               '</Response>'

    context = interface.create_context_from_twilio_event(
        from_tel, to_tel, is_voicecall, message)
    return bot.handle_action(context)


@app.post('/twilio/callback/<bot_name>')
def twilio_callback(bot_name):
    bot, interface = _get_bot_and_interface(bot_name)
    _require_twilio_signature(interface)

    if request.params.getunicode('Message'):
        return 'OK'

    from_tel = request.params.getunicode('From')
    to_tel = request.params.getunicode('To')
    is_voicecall = request.params.getunicode('CallSid') is not None
    if is_voicecall:
        # Gather で音声認識した場合のみ
        message = request.params.getunicode('SpeechResult')
    else:
        # SMS の本文
        message = request.params.getunicode('Body')

    return twilio_callback_sub(
        bot, interface, from_tel, to_tel, is_voicecall, message)


# @dial コマンド利用時のみの特殊なコールバック呼び出し
# この endpoint を Twilio 側に設定する必要は無い
@app.post('/twilio/dial_content/<bot_name>/<message>')
def twilio_dial_content(bot_name, message):
    bot, interface = _get_bot_and_interface(bot_name)
    _require_twilio_signature(interface)

    # Outbound のダイアル時なので、From と To が逆になる
    from_tel = request.params.getunicode('To')
    to_tel = request.params.getunicode('From')
    is_voicecall = True

    return twilio_callback_sub(
        bot, interface, from_tel, to_tel, is_voicecall, message)


# @dial コマンドの完了通知のみの特殊なコールバック呼び出し
# この endpoint を Twilio 側に設定する必要は無い
@app.post('/twilio/dial_completed_callback/<bot_name>/<message>')
def twilio_dial_completed_callback(bot_name, message):
    bot, interface = _get_bot_and_interface(bot_name)
    _require_twilio_signature(interface)

    # Outbound のダイアル時なので、From と To が逆になる
    from_tel = request.params.getunicode('To')
    to_tel = request.params.getunicode('From')
    is_voicecall = True

    call_status = request.params.getunicode('CallStatus')
    if call_status == 'completed':
        duration = request.params.getunicode('CallDuration')
        if duration is not None and int(duration) > 1:
            action = f'{message}:OK'
        else:
            # 会話時間が1秒以下の場合は NG 扱い
            action = f'{message}:NG'
    else:
        # 話し中・失敗・電話に出ないなど
        action = f'{message}:NG'

    return twilio_callback_sub(
        bot, interface, from_tel, to_tel, is_voicecall, action)


# @delay コマンド利用時のみの task queue からのコールバック
@app.post('/twilio/internal_callback/<bot_name>')
def twilio_internal_callback(bot_name):
    from_tel = request.params.getunicode('From')
    to_tel = request.params.getunicode('To')
    is_voicecall = request.params.getunicode('CallSid') is not None
    message = request.params.getunicode('Message')

    _require_api_token()
    bot, interface = _get_bot_and_interface(bot_name)

    return twilio_callback_sub(
        bot, interface, from_tel, to_tel, is_voicecall, message)
