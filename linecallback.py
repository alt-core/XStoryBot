# coding: utf-8
# [START app]
import os
import logging
import re
from unicodedata import normalize

from bottle import request, Bottle, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, PostbackEvent, FollowEvent, UnfollowEvent, JoinEvent, LeaveEvent, TextMessage, TextSendMessage, ImageSendMessage, TemplateSendMessage, ButtonsTemplate, ConfirmTemplate, CarouselTemplate, CarouselColumn, MessageTemplateAction, PostbackTemplateAction, URITemplateAction, ImagemapSendMessage, ImagemapArea, MessageImagemapAction, URIImagemapAction, BaseSize

# SSL 警告を防ぐため / 今回のケースでは問題ないが、行儀は良くない
try:
    import requests.packages.urllib3
    requests.packages.urllib3.disable_warnings()
except ImportError:
    pass

import settings
from models import PlayerStateDB, GlobalVariables
from scenario import Scenario, Director, ScenarioSyntaxError


app = Bottle()

line_access_token = settings.LINE['access_token']
line_channel_secret = settings.LINE['channel_secret']
sheet_id = settings.SHEETS['sheet_id']

line_bot_api = LineBotApi(line_access_token)
handler = WebhookHandler(line_channel_secret)

alt_text = u'LINEアプリで確認してください。'

# シナリオをロードする
scenario = None
scenario_counter = 0
scenario_error_log = u''


def check_reload():
    global scenario_counter
    global_variables = GlobalVariables.get_by_id(id="global")
    if global_variables is None:
        global_variables = GlobalVariables(id="global", scenario_counter=0)
    if scenario_counter != global_variables.scenario_counter:
        # 他のインスタンスがリロードを実行した
        ok, err = reload_scenario()
        if ok:
            # リロードに成功した
            scenario_counter = global_variables.scenario_counter
        else:
            # 現在どこでも使っていない
            global scenario_error_log
            scenario_error_log = err


def reload_scenario():
    import google_sheets
    global scenario
    try:
        scenario = Scenario.from_tables(google_sheets.get_table_from_google_sheets(sheet_id))
        return True, None
    except (ValueError, ScenarioSyntaxError) as e:
        return False, unicode(e)


if settings.STARTUP_LOAD_SHEET:
    check_reload()

if scenario is None:
    scenario = Scenario.from_table([
        [u'//', u'シナリオのロードができていません'],
    ])


@app.post('/callback')
def callback():
    check_reload()

    signature = request.headers['X-Line-Signature']
    body = request.body.read().decode('utf-8')
    logging.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    if event.message.text == u"リロード":
        ok, err = reload_scenario()
        if ok:
            # リロードに成功した
            global scenario_counter
            global_variables = GlobalVariables.get_by_id(id="global")
            if global_variables is None:
                global_variables = GlobalVariables(id="global", scenario_counter=0)
            global_variables.scenario_counter += 1
            global_variables.put()
            scenario_counter = global_variables.scenario_counter
            player_state = PlayerStateDB(event.source.sender_id)
            player_state.reset()
            player_state.save()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=u"リロードしました。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=u"リロードに失敗しました。\n\n" + err))
        return

    if event.message.text == u"リセット":
        player_state = PlayerStateDB(event.source.sender_id)
        player_state.reset()
        player_state.save()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=u"リセットしました。"))
        return

    player_state = PlayerStateDB(event.source.sender_id)
    msgs = respond_message(event.message.text, player_state)
    player_state.save()

    if msgs:
        line_bot_api.reply_message(event.reply_token, msgs)


@handler.add(PostbackEvent)
def handle_postback(event):
    player_state = PlayerStateDB(event.source.sender_id)
    data, visit_id = event.postback.data.split(u'@@')
    if player_state.visit_id != visit_id:
        # 古いシーンの選択肢が送られてきた
        logging.info(u'received postback with invalid visit_id')
        return
    msgs = respond_message(data, player_state)
    player_state.save()

    if msgs:
        line_bot_api.reply_message(event.reply_token, msgs)


@handler.add(FollowEvent)
@handler.add(UnfollowEvent)
@handler.add(JoinEvent)
@handler.add(LeaveEvent)
def handle_other_events(event):
    player_state = PlayerStateDB(event.source.sender_id)

    if event.type == u"follow" or event.type == u"join":
        # follow と join の際にはセーブデータを初期化する
        player_state.reset()

    msgs = respond_message(u'##' + event.type, player_state)
    player_state.save()

    if msgs:
        line_bot_api.reply_message(event.reply_token, msgs)


def append_visit_id(data, visit_id):
    return data + u'@@' + visit_id

def build_template_actions(choices, visit_id):
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
                results.append(PostbackTemplateAction(label=choice[0], data=append_visit_id(choice[1], visit_id)))
            else:
                results.append(MessageTemplateAction(choice[0], choice[1]))
        elif len(choice) >= 3:
            if choice[1]:
                results.append(PostbackTemplateAction(label=choice[0], text=choice[1], data=append_visit_id(choice[2], visit_id)))
            else:
                results.append(PostbackTemplateAction(label=choice[0], data=append_visit_id(choice[2], visit_id)))
    return results


def safe_list_get(li, index, default_value):
    return li[index] if len(li) > index else default_value


def parse_image_url(cell):
    image_match = re.match(r'^=IMAGE\("([^"]+)"\)', cell)
    if image_match:
        return image_match.group(1)
    else:
        return None


def template_message(template):
    return TemplateSendMessage(alt_text, template)


def respond_message(action, player_state):
    director = Director(scenario, player_state)
    reactions = director.get_reaction(action)
    if reactions is None:
        return None
    results = []
    for reaction, args in reactions:
        msg = reaction[0]
        options = reaction[1:] if len(reaction) > 1 else []
        if msg == u'@confirm' or msg == u'@確認':
            if len(options) > 0:
                results.append(template_message(ConfirmTemplate(text=options[0], actions=build_template_actions(args, player_state.visit_id))))
            else:
                logging.error("invalid format: @confirm")
                results.append(TextSendMessage(text=u"<<@confirmを解釈できませんでした>>"))
        elif msg == u'@button' or msg == u'@ボタン':
            if len(options) > 0:
                title = safe_list_get(options, 1, None)
                image_url = parse_image_url(options[2]) if len(options) > 2 else None
                results.append(template_message(ButtonsTemplate(text=options[0], title=title, thumbnail_image_url=image_url, actions=build_template_actions(args, player_state.visit_id))))
            else:
                logging.error("invalid format: @button")
                results.append(TextSendMessage(text=u"<<@buttonを解釈できませんでした>>"))
        elif msg == u'@carousel' or msg == u'@カルーセル' or msg == u'@panel' or msg == u'@パネル':
            panel_templates = []
            for panel, choices in args:
                title = safe_list_get(panel, 1, None)
                image_url = parse_image_url(panel[2]) if len(panel) > 2 else None
                panel_templates.append(
                    CarouselColumn(text=panel[0], title=title, thumbnail_image_url=image_url, actions=build_template_actions(choices, player_state.visit_id))
                )
            results.append(template_message(CarouselTemplate(panel_templates)))
        elif msg == u'@imagemap' or msg == u'@イメージマップ':
            try:
                url = parse_image_url(options[0])
                if url is None:
                    raise ValueError
                m = re.match(r'^(.*)\.(png|jpg|jpeg|gif)$', url, re.IGNORECASE)
                if not m:
                    raise ValueError
                base_url = m.group(1)

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
                results.append(ImagemapSendMessage(base_url, alt_text, BaseSize(1040, height), imagemap_actions))
            except (ValueError, IndexError):
                logging.error("invalid format: @imagemap")
                results.append(TextSendMessage(text=u"<<@imagemapを解釈できませんでした>>"))
        else:
            url = parse_image_url(msg)
            if url is not None:
                m = re.match(r'^(https://.*/)([^/]*)', url)
                if m:
                    preview_url = m.group(1) + 'resize/' + m.group(2)
                    results.append(ImageSendMessage(url, preview_url))
                else:
                    logging.error("cannot parse image url: " + url)
                    results.append(TextSendMessage(text=u"<<ImageUrlを解釈できませんでした>>"))
            else:
                results.append(TextSendMessage(text=msg))
    return results


#@app.error(500)
#def server_error(e):
#    # Log the error and stacktrace.
#    logging.exception('An error occurred during a request.')
#    return 'An internal error occurred.', 500

if __name__ == "__main__":
    app.run(host='localhost', port=8080, debug=True)

# [END app]
