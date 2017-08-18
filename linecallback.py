# coding: utf-8
# [START app]
import os
import logging
import re
from unicodedata import normalize
import urllib
import hmac
import hashlib
import base64
import time

from google.appengine.api import taskqueue

from bottle import request, response, Bottle, abort
from linebot import LineBotApi, WebhookParser
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, PostbackEvent, FollowEvent, UnfollowEvent, JoinEvent, LeaveEvent, TextMessage, TextSendMessage, ImageSendMessage, TemplateSendMessage, ButtonsTemplate, ConfirmTemplate, CarouselTemplate, CarouselColumn, MessageTemplateAction, PostbackTemplateAction, URITemplateAction, ImagemapSendMessage, ImagemapArea, MessageImagemapAction, URIImagemapAction, BaseSize

# SSL 警告を防ぐため / 今回のケースでは問題ないが、行儀は良くない
try:
    import requests.packages.urllib3
    requests.packages.urllib3.disable_warnings()
except ImportError:
    pass

import settings
from models import PlayerStateDB, GlobalBotVariables, GroupMembers
from scenario import Scenario, Director, ScenarioSyntaxError


ALT_TEXT = u'LINEアプリで確認してください。'


app = Bottle()

import twilio.rest
twilio_client = twilio.rest.Client(settings.PHONE['twilio_sid'], settings.PHONE['twilio_auth_token'])


class LineBot(object):
    def __init__(self, name, line_access_token, line_channel_secret, sheet_id):
        self.name = name
        self.line_access_token = line_access_token
        self.line_channel_secret = line_channel_secret
        self.sheet_id = sheet_id
        self.scenario = None
        self.scenario_counter = 0
        self.error_log = u''
        self.line_bot_api = LineBotApi(line_access_token, timeout=30)
        self.parser = WebhookParser(line_channel_secret)

    def load_scenario(self):
        import google_sheets
        try:
            self.scenario = Scenario.from_tables(google_sheets.get_table_from_google_sheets(self.sheet_id))
            return True, None
        except (ValueError, ScenarioSyntaxError) as e:
            return False, unicode(e)

    def check_reload(self, force=False):
        global_bot_variables = GlobalBotVariables.get_by_id(id=self.name)
        if global_bot_variables is None:
            global_bot_variables = GlobalBotVariables(id=self.name, scenario_counter=0)
        if self.scenario_counter != global_bot_variables.scenario_counter or force:
            # 他のインスタンスがリロードを実行した
            ok, err = self.load_scenario()
            if ok:
                # リロードに成功した
                self.scenario_counter = global_bot_variables.scenario_counter
            else:
                # 現在どこでも使っていない
                self.error_log = err

    def reply_message(self, recv_event, message):
        if recv_event.reply_token != "0" * 32:
            self.line_bot_api.reply_message(recv_event.reply_token, message)
        else:
            self.line_bot_api.push_message(recv_event.source.sender_id, message)

    def gen_signature(self, line_channel_secret, body):
        return base64.b64encode(hmac.new(
            line_channel_secret,
            body.encode('utf-8'),
            hashlib.sha256
        ).digest())

    def send_action(self, source_type, source_id, bot_name, action):
        if source_type == 'user':
            source_json = u'{"userId":"' + source_id + u'","type":"user"}'
        elif source_type == 'group':
            source_json = u'{"groupId":"' + source_id + u'","type":"group"}'
        elif source_type == 'room':
            source_json = u'{"roomId":"' + source_id + u'","type":"room"}'
        else:
            logging.error(u'unknown source type:' + source_type)
            source_json = None
        data = u'{"events":[{"type":"postback","replyToken":"00000000000000000000000000000000","source":'+source_json+u',"timestamp":'+unicode(int(time.time()))+u',"postback":{"data":"' + action + u'@@FORWARD"}}]}'
        global bot_dict
        bot = bot_dict[bot_name]
        sign = self.gen_signature(bot.line_channel_secret, data)
        events = bot.parser.parse(data, sign)
        for event in events:
            bot.handle_event(event)

    def handle_api_send(self, group, action):
        group_members = GroupMembers.get_by_id(id=group)
        if group_members:
            members = group_members.members
        else:
            members = []
        for member in members:
            source_type, source_id = member.split(':')
            self.send_action(source_type, source_id, self.name, action)
            # TODO: LINE 側に rate limit ない？

    def handle_event(self, event):
        if isinstance(event, MessageEvent):
            if isinstance(event.message, TextMessage):
                self.handle_text_message(event)
        elif isinstance(event, PostbackEvent):
            self.handle_postback(event)
        elif isinstance(event, (FollowEvent, UnfollowEvent, JoinEvent, LeaveEvent)):
            self.handle_other_events(event)

    def reload_scenario_and_notify_to_other_instances(self):
        ok, err = self.load_scenario()
        if ok:
            # リロードに成功した
            global_bot_variables = GlobalBotVariables.get_by_id(id=self.name)
            if global_bot_variables is None:
                global_bot_variables = GlobalBotVariables(id=self.name, scenario_counter=0)
            global_bot_variables.scenario_counter += 1
            global_bot_variables.put()
            self.scenario_counter = global_bot_variables.scenario_counter
            # リロード時にプレイヤー状態をリセットしていたが、
            # 複雑なシナリオのデバッグ時に不都合なのでコメントアウト
            #player_state = PlayerStateDB(event.source.sender_id)
            #player_state.reset()
            #player_state.save()
            return True
        else:
            logging.error(u"リロードに失敗しました。\n" + unicode(err))
            return False

    def handle_text_message(self, event):
        if event.message.text == u"リロード":
            if self.reload_scenario_and_notify_to_other_instances():
                # リロードに成功した
                self.reply_message(event, TextSendMessage(text=u"リロードしました。"))
            else:
                self.reply_message(event, TextSendMessage(text=u"リロードに失敗しました。\n\n" + err))
            return

        if event.message.text == u"リセット":
            player_state = PlayerStateDB(event.source.sender_id)
            player_state.reset()
            player_state.save()
            self.reply_message(event, TextSendMessage(text=u"リセットしました。"))
            return

        player_state = PlayerStateDB(event.source.sender_id)
        msgs = self.respond_message(event.message.text, player_state, event.source)
        player_state.save()

        if msgs:
            self.reply_message(event, msgs)

    def handle_postback(self, event):
        player_state = PlayerStateDB(event.source.sender_id)
        data, visit_id = event.postback.data.split(u'@@')
# TODO: ちゃんと visit_id が送れないケースがあるのでコメントアウト
#        if player_state.visit_id != visit_id and visit_id != u'FORWARD':
#            # 古いシーンの選択肢が送られてきた
#            logging.info(u'received postback with invalid visit_id')
#            return
        msgs = self.respond_message(data, player_state, event.source)
        player_state.save()

        if msgs:
            self.reply_message(event, msgs)

    def handle_other_events(self, event):
        player_state = PlayerStateDB(event.source.sender_id)

# 新規フォロー時にプレイヤー状態をリセットしていたが、
# 複数のアカウントを連携させる場合、2番目のフォローで初期化されると困るのでコメントアウト
#        if event.type == u"follow" or event.type == u"join":
#            # follow と join の際にはセーブデータを初期化する
#            player_state.reset()

        msgs = self.respond_message(u'##' + event.type, player_state, event.source)
        player_state.save()

        if msgs:
            self.reply_message(event, msgs)

    @staticmethod
    def append_visit_id(data, visit_id):
        return data + u'@@' + visit_id

    def build_template_actions(self, choices, visit_id):
        results = []
        if choices is None: return results
        for choice in choices:
            if len(choice) == 0:
                continue
            elif len(choice) == 1:
                results.append(MessageTemplateAction(choice[0], choice[0]))
            elif len(choice) == 2:
                if re.match(r'^(https?|tel):', choice[1]):
                    results.append(URITemplateAction(choice[0], choice[1]))
                elif re.match(u'^[#*]', choice[1]):
                    results.append(PostbackTemplateAction(label=choice[0], data=self.append_visit_id(choice[1], visit_id)))
                else:
                    results.append(MessageTemplateAction(choice[0], choice[1]))
            elif len(choice) >= 3:
                if choice[1]:
                    results.append(PostbackTemplateAction(label=choice[0], text=choice[1], data=self.append_visit_id(choice[2], visit_id)))
                else:
                    results.append(PostbackTemplateAction(label=choice[0], data=self.append_visit_id(choice[2], visit_id)))
        return results

    @staticmethod
    def safe_list_get(li, index, default_value):
        return li[index] if len(li) > index else default_value

    @staticmethod
    def parse_image_url(cell):
        image_match = re.match(r'^=IMAGE\("([^"]+)"\)', cell)
        if image_match:
            return image_match.group(1)
        else:
            return None

    @staticmethod
    def get_image_url(url, option = None):
        if url is None:
            return None
        image_url = 'https://' + os.getenv('SERVER_NAME', 'localhost:8080') + '/line/image/' + urllib.quote_plus(url)
        if option:
            image_url += ('/' + option)
        return image_url

    def template_message(self, template):
        return TemplateSendMessage(ALT_TEXT, template)

    def process_common_reaction(self, msg, options, player_state):
        return False

    def respond_message(self, action, player_state, source):
        director = Director(self.scenario, player_state)
        reactions = director.get_reaction(action)
        if reactions is None:
            return None
        results = []
        for reaction, args in reactions:
            msg = reaction[0]
            options = reaction[1:] if len(reaction) > 1 else []
            if self.process_common_reaction(msg, options, player_state):
                pass
            elif msg == u'@confirm' or msg == u'@確認':
                if len(options) > 0:
                    results.append(self.template_message(ConfirmTemplate(text=options[0], actions=self.build_template_actions(args, player_state.visit_id))))
                else:
                    logging.error("invalid format: @confirm")
                    results.append(TextSendMessage(text=u"<<@confirmを解釈できませんでした>>"))
            elif msg == u'@button' or msg == u'@ボタン':
                if len(options) > 0:
                    title = self.safe_list_get(options, 1, None)
                    image_url = self.parse_image_url(options[2]) if len(options) > 2 else None
                    results.append(self.template_message(ButtonsTemplate(text=options[0], title=title, thumbnail_image_url=self.get_image_url(image_url), actions=self.build_template_actions(args, player_state.visit_id))))
                else:
                    logging.error("invalid format: @button")
                    results.append(TextSendMessage(text=u"<<@buttonを解釈できませんでした>>"))
            elif msg == u'@carousel' or msg == u'@カルーセル' or msg == u'@panel' or msg == u'@パネル':
                panel_templates = []
                for panel, choices in args:
                    title = self.safe_list_get(panel, 1, None)
                    image_url = self.parse_image_url(panel[2]) if len(panel) > 2 else None
                    panel_templates.append(
                        CarouselColumn(text=panel[0], title=title, thumbnail_image_url=self.get_image_url(image_url), actions=self.build_template_actions(choices, player_state.visit_id))
                    )
                results.append(self.template_message(CarouselTemplate(panel_templates)))
            elif msg == u'@imagemap' or msg == u'@イメージマップ':
                try:
                    url = self.parse_image_url(options[0])
                    if url is None:
                        raise ValueError
#                    m = re.match(r'^(.*)\.(png|jpg|jpeg|gif)$', url, re.IGNORECASE)
#                    if not m:
#                        raise ValueError
#                    base_url = m.group(1)

                    if len(options) > 1:
                        height = int(1040 * float(options[1]))
                    else:
                        height = 1040

                    imagemap_actions = []
                    for arg in args:
                        coord = map(int, arg[0].split(','))
                        area = ImagemapArea(coord[0], coord[1], coord[2], coord[3])

                        if re.match(r'^(https?|tel):', arg[1]):
                            imagemap_action = URIImagemapAction(arg[1], area)
                        else:
                            imagemap_action = MessageImagemapAction(arg[1], area)
                        imagemap_actions.append(imagemap_action)
#                    results.append(ImagemapSendMessage(base_url, ALT_TEXT, BaseSize(1040, height), imagemap_actions))
                    results.append(ImagemapSendMessage(self.get_image_url(url), ALT_TEXT, BaseSize(1040, height), imagemap_actions))
                except (ValueError, IndexError):
                    logging.error("invalid format: @imagemap")
                    results.append(TextSendMessage(text=u"<<@imagemapを解釈できませんでした>>"))
            elif msg == u'@forward' or msg == u'@転送':
                bot_name = options[0]
                action = options[1]
                global bot_dict
                if bot_name not in bot_dict:
                    logging.error("invalid bot name: @forward:"+ bot_name)
                    results.append(TextSendMessage(text=u"<<@forwardを解釈できませんでした>>"))
                self.send_action(source.type, source.sender_id, bot_name, action)
            elif msg == u'@group_add' or msg == u'@グループ追加':
                group_name = options[0]
                group_members = GroupMembers.get_by_id(id=group_name)
                if group_members is None:
                    group_members = GroupMembers(id=group_name, members=[])
                member = source.type + ':' + source.sender_id
                if member not in group_members.members:
                    group_members.members.append(member)
                    group_members.put()
            elif msg == u'@group_del' or msg == u'@グループ削除':
                group_name = options[0]
                group_members = GroupMembers.get_by_id(id=group_name)
                if group_members:
                    member = source.type + ':' + source.sender_id
                    if member in group_members.members:
                        group_members.members.remove(member)
                        group_members.put()

            else:
                url = self.parse_image_url(msg)
                if url is not None:
                    results.append(ImageSendMessage(self.get_image_url(url), self.get_image_url(url, 'preview')))
#                    m = re.match(r'^(https://.*/)([^/]*)', url)
#                    if m:
#                        preview_url = m.group(1) + 'resize/' + m.group(2)
#                        results.append(ImageSendMessage(url, preview_url))
#                    else:
#                        logging.error("cannot parse image url: " + url)
#                        results.append(TextSendMessage(text=u"<<ImageUrlを解釈できませんでした>>"))
                else:
                    results.append(TextSendMessage(text=msg))
        return results

    def handle_twilio(self, from_tel, to_tel, is_voicecall, message):
        if message == u"リロード":
            self.reload_scenario_and_notify_to_other_instances()
            return

        if is_voicecall:
            # 音声着信の場合、action は #tel:電話番号 とする
            action = u'#tel:'+to_tel
            if message is not None:
                # message がある場合（音声認識した、または @dial の内容取得時）は
                # message で上書き
                action = message
        else:
            # テキストメッセージの場合、action は 本文 とする
            action = message
            # action = u'#sms:'+to_tel

        # ユーザID は 'tel:' + to_tel
        # TODO: to_tel をそのまま user_id として使わない
        player_state = PlayerStateDB(u'tel:'+from_tel)

        director = Director(self.scenario, player_state)
        reactions = director.get_reaction(action)

        twiml = u'<?xml version="1.0" encoding="UTF-8"?>' \
                u'<Response>'

        if reactions is None:
            reactions = []
        for reaction, args in reactions:
            msg = reaction[0]
            options = reaction[1:] if len(reaction) > 1 else []

            if self.process_common_reaction(msg, options, player_state):
                pass
            elif msg == u'@sms' or msg == u'@SMS':
                message = options[0]
                twilio_client.messages.create(
                    to=from_tel,
                    from_=settings.PHONE['sms_from'],
                    body=message
                )
            elif msg == u'@dial' or msg == u'@電話':
                action_dial_content = options[0]
                url_dial_content = 'https://' + settings.SERVER_NAME + '/twilio/dial_content/' + self.name + \
                                   '/' + urllib.quote(action_dial_content.encode('utf-8'), '')
                logging.info('TwiML url: ' + url_dial_content)
                if len(options) == 1:
                    twilio_client.calls.create(
                        to=from_tel,
                        from_=settings.PHONE['dial_from'],
                        url=url_dial_content,
                        timeout=5
                    )
                else:
                    # 通話の完了通知が必要
                    action_completed = options[1]
                    url_completed = 'https://' + settings.SERVER_NAME + '/twilio/dial_completed_callback/' + self.name + \
                                    '/' + urllib.quote(action_completed.encode('utf-8'), '')
                    twilio_client.calls.create(
                        to=from_tel,
                        from_=settings.PHONE['dial_from'],
                        url=url_dial_content,
                        timeout=5,
                        status_callback=url_completed,
                        status_callback_event=['completed']
                    )

            elif msg == u'@delay' or msg == u'@遅延':
                delay_secs = int(options[0])
                action = options[1]
                global bot_dict
                param_sid = 'CallSid' if is_voicecall else 'MessageSid'
                task = taskqueue.add(url='/twilio/internal_callback/' + self.name,
                                     params={'From': from_tel, 'To': to_tel, param_sid: 'DELAY', 'Message': action},
                                     countdown=delay_secs)
                logging.info("enqueue a task: {}, ETA {}".format(task.name, task.eta))
            elif msg.startswith(u'<'):
                twiml += msg
            else:
                if is_voicecall:
                    twiml += u'<Say language="ja-jp" voice="woman">' + msg + u'</Say>'
                else:
                    twiml += u'<Message>' + msg + u'</Message>'

        twiml += u'</Response>'

        player_state.save()

        return twiml


bot_dict = {}
for name, bot_settings in settings.BOTS.items():
    bot_dict[name] = LineBot(name,
                         line_access_token = bot_settings['line_access_token'],
                         line_channel_secret = bot_settings['line_channel_secret'],
                         sheet_id = bot_settings['sheet_id'])


for name, bot in bot_dict.items():
    if settings.STARTUP_LOAD_SHEET:
        bot.check_reload(force=True)

    if bot.scenario is None:
        bot.scenario = Scenario.from_table([
            [u'//', u'シナリオのロードができていません'],
        ])


@app.post('/line/callback/<bot_name>')
def callback(bot_name):
    bot = bot_dict.get(bot_name, None)
    if not bot:
        abort(404)
    bot.check_reload()

    signature = request.headers['X-Line-Signature']
    body = request.body.read().decode('utf-8')
    logging.info("Request body: " + body)

    try:
        events = bot.parser.parse(body, signature)
        for event in events:
            bot.handle_event(event)
    except InvalidSignatureError:
        abort(400)

    return 'OK'


@app.get('/line/api/send/<bot_name>')
def send(bot_name):
    bot = bot_dict.get(bot_name, None)
    if not bot:
        abort(404)
    bot.check_reload()

    body = request.body.read().decode('utf-8')
    logging.info("API call: " + body)

    group = request.query.getunicode('group')
    action = request.query.getunicode('action')
    bot.handle_api_send(group, action)

    return 'OK'


def twilio_callback_sub(bot_name, from_tel, to_tel, is_voicecall, message):

    body = request.body.read().decode('utf-8')
    logging.info("Twilio callback: " + body)

    bot = bot_dict.get(bot_name, None)
    if not bot:
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

    return bot.handle_twilio(from_tel, to_tel, is_voicecall, message)

@app.post('/twilio/callback/<bot_name>')
def twilio_callback(bot_name):
    if request.forms.getunicode('Message'):
        return 'OK'
    from_tel = request.forms.getunicode('From')
    to_tel = request.forms.getunicode('To')
    is_voicecall = request.forms.getunicode('CallSid') is not None
    if is_voicecall:
        # Gather で音声認識した場合のみ
        message = request.forms.getunicode('SpeechResult')
    else:
        # SMS の本文
        message = request.forms.getunicode('Body')

    # TODO: 不正なアクセスを防ぐために secret_key が本来は必要

    return twilio_callback_sub(bot_name, from_tel, to_tel, is_voicecall, message)

# @dial コマンド利用時のみの特殊なコールバック呼び出し
# この endpoint を Twilio 側に設定する必要は無い
@app.post('/twilio/dial_content/<bot_name>/<message>')
def twilio_dial_content(bot_name, message):
    # Outbound のダイアル時なので、From と To が逆になる
    from_tel = request.forms.getunicode('To')
    to_tel = request.forms.getunicode('From')
    is_voicecall = True

    # TODO: 不正なアクセスを防ぐために secret_key が本来は必要

    return twilio_callback_sub(bot_name, from_tel, to_tel, is_voicecall, message)

# @dial コマンドの完了通知のみの特殊なコールバック呼び出し
# この endpoint を Twilio 側に設定する必要は無い
@app.post('/twilio/dial_completed_callback/<bot_name>/<message>')
def twilio_dial_content(bot_name, message):
    # Outbound のダイアル時なので、From と To が逆になる
    from_tel = request.forms.getunicode('To')
    to_tel = request.forms.getunicode('From')
    is_voicecall = True

    call_status = request.forms.getunicode('CallStatus')
    if call_status == u'completed':
        duration = request.forms.getunicode('CallDuration')
        if duration is not None and int(duration) > 1:
            action = message + u':OK'
        else:
            # 会話時間が1秒以下の場合は NG 扱い
            action = message + u':NG'
    else:
        # 話し中・失敗・電話に出ないなど
        action = message + u':NG'

    # TODO: 不正なアクセスを防ぐために secret_key が本来は必要

    return twilio_callback_sub(bot_name, from_tel, to_tel, is_voicecall, action)

# @delay コマンド利用時のみの task queue からのコールバック
@app.post('/twilio/internal_callback/<bot_name>')
def twilio_internal_callback(bot_name):
    from_tel = request.forms.getunicode('From')
    to_tel = request.forms.getunicode('To')
    is_voicecall = request.forms.getunicode('CallSid') is not None
    message = request.forms.getunicode('Message')

    # TODO: 不正なアクセスを防ぐために secret_key が本来は必要

    return twilio_callback_sub(bot_name, from_tel, to_tel, is_voicecall, message)


#@app.error(500)
#def server_error(e):
#    # Log the error and stacktrace.
#    logging.exception('An error occurred during a request.')
#    return 'An internal error occurred.', 500

if __name__ == "__main__":
    app.run(host='localhost', port=8080, debug=True)

# [END app]
