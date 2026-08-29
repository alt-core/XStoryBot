# coding: utf-8
import logging
import re
from unicodedata import normalize

from linebot.models import MessageEvent, PostbackEvent, FollowEvent, UnfollowEvent, JoinEvent, LeaveEvent, TextMessage, LocationMessage, StickerMessage, TextSendMessage, ImageSendMessage, TemplateSendMessage, ButtonsTemplate, ConfirmTemplate, CarouselTemplate, CarouselColumn, MessageAction, PostbackAction, URIAction, ImagemapSendMessage, ImagemapArea, MessageImagemapAction, URIImagemapAction, BaseSize, Sender, QuickReply, QuickReplyButton, FlexSendMessage
import json

import hub
import commands
import utility


BUTTON_CMDS = ('@button', '@ボタン')
CONFIRM_CMDS = ('@confirm', '@確認')
PANEL_CMDS = ('@carousel', '@カルーセル', '@panel', '@パネル')
IMAGEMAP_CMDS = ('@imagemap', '@イメージマップ')
FLEX_CMDS = ('@flex', '@フレックス')
REPLY_CMDS = ('@reply', '@リプライ')
RICHMENU_CMDS = (u'@richmenu', u'@リッチメニュー')
ALL_TEMPLATE_CMDS = BUTTON_CMDS + CONFIRM_CMDS + PANEL_CMDS + IMAGEMAP_CMDS + REPLY_CMDS + RICHMENU_CMDS


class LineDefaultCommandsPlugin_Builder(object):
    def __init__(self, params):
        self.params = params
        self.disable_response_length_check = params.get('disable_response_length_check', False)

    def build_from_command(self, builder, sender, msg, options, children=[], grandchildren=[]):
        if msg in CONFIRM_CMDS or msg in BUTTON_CMDS or msg in IMAGEMAP_CMDS or msg in REPLY_CMDS:
            for choice in children:
                self.lint_choice(builder, msg, choice)

        if msg in CONFIRM_CMDS:
            builder.add_command(sender, msg, options, children)

        elif msg in BUTTON_CMDS:
            builder._build_and_replace_imageurl(options, 2)
            if (len(options) > 1 and options[1] != '') or (len(options) > 2 and options[2] != ''):
                builder.assert_strlen_from_array(options, 0, 60, 'タイトルか画像を指定した場合の文字数制限（{}文字）')
            builder.add_command(sender, msg, options, children)

        elif msg in IMAGEMAP_CMDS:
            orig_url = builder.parse_imageurl(options[0])
            url, size = builder.build_image_for_imagemap_command(orig_url)
            options[:] = [str(url), str(size[0]), str(size[1])]
            builder.add_command(sender, msg, options, children)

        elif msg in PANEL_CMDS:
            panels = []
            num_choices = -1
            flag_title = None
            flag_image = None
            for i in range(len(children)):
                panel = children[i]
                builder._build_and_replace_imageurl(panel, 2)
                if (len(panel) > 1 and panel[1] != '') or (len(panel) > 2 and panel[2] != ''):
                    builder.assert_strlen_from_array(panel, 0, 60, 'タイトルか画像を指定した場合の文字数制限（{}文字）')
                for choice in grandchildren[i]:
                    self.lint_choice(builder, msg, choice)
                if len(grandchildren[i]) == 0:
                    builder.raise_error('選択肢が0個です')
                if len(grandchildren[i]) > 3:
                    builder.raise_error('パネルの選択肢は最大3個です')
                if num_choices != -1 and num_choices != len(grandchildren[i]):
                    builder.raise_error('各パネルの選択肢数がばらばらです')
                num_choices = len(grandchildren[i])
                title = utility.safe_list_get(panel, 1, '')
                image = utility.safe_list_get(panel, 2, '')
                if (flag_title is not None) and ((title != '') != flag_title):
                    builder.raise_error('各パネルのタイトルの有無がばらばらです')
                flag_title = (title != '')
                if (flag_image is not None) and ((image != '') != flag_image):
                    builder.raise_error('各パネルの画像の有無がばらばらです')
                flag_image = (image != '')
                panels.append([children[i], grandchildren[i]])
            builder.add_command(sender, msg, options, panels)

        elif msg in FLEX_CMDS:
            builder.add_command(sender, msg, options, children)

        elif msg in REPLY_CMDS:
            if (builder.msg_count == 0):
                builder.raise_error('クイックリプライをつける対象のメッセージがありません')
            builder.add_command(sender, msg, options, children)
            builder.msg_count -= 1 # REPLY_CMDSはメッセージ数を消費しない

        elif msg in RICHMENU_CMDS:
            builder.add_command(sender, msg, options, children)

        else:
            # ここには来ないはず
            builder.raise_error('内部エラー:未知のコマンドです')

        builder.msg_count += 1

        # 解釈はここで終了
        return True

    def callback_new_block(self, builder, cond):
        builder.msg_count = 0

    def build_plain_text(self, builder, sender, msg, options):
        # 通常のテキストメッセージ表示
        # 仕様書に記述がないが、おそらく300文字が上限
        builder.assert_strlen(msg, 300)
        builder.msg_count += 1
        builder.add_command(sender, msg, options, None)
        return True

    def callback_after_each_line(self, builder):
        if (not self.disable_response_length_check) and builder.msg_count > 5:
            builder.raise_error('6つ以上のメッセージを同時に送ろうとしました')

    def lint_choice(self, builder, msg, choice):
        action_label = choice[0]
        action_value = ''
        action_data = ''
        if len(choice) <= 1 or not choice[1]:
            action_type = 'message'
            action_value = action_label
        else:
            if utility.parse_url(choice[1]):
                action_type = 'url'
                action_value = choice[1]
            elif re.match(r'^[#＃*＊]', choice[1]):
                action_type = 'postback'
                action_data = choice[1]
            else:
                action_type = 'message'
                action_value = choice[1]
        if len(choice) > 2 and choice[2]:
            if re.match(r'^[#＃*＊]', choice[2]):
                if action_type == 'url':
                    builder.raise_error('アクションラベル指定時は URL を開かせることはできません', *choice)
                action_type = 'postback'
                action_data = choice[2]
            else:
                builder.raise_error('アクションラベルは # か * で始まらないといけません', *choice)

        if msg in IMAGEMAP_CMDS:
            if action_type == 'postback':
                builder.raise_error('イメージマップではアクションラベルは指定できません', *choice)
            try:
                x, y, w, h = [int(x) for x in action_label.split(',')]
                if x < 0 or 1040 <= x or y < 0 or w <= 0 or 1040 < w or h <= 0:
                    raise ValueError
            except (ValueError, IndexError):
                builder.raise_error('イメージマップアクションの指定が不正です', action_label)
        else:
            builder.assert_strlen(action_label, 20)
        if action_type in ('message', 'postback'):
            builder.assert_strlen(action_value, 300)
        builder.assert_strlen(action_data, 300)
        return True


class LineDefaultCommandsPlugin_Runtime(object):
    def __init__(self, params):
        self.alt_text = params['alt_text']
        self.sender_icon_urls = params.get('sender_icon_urls', {})
        if not isinstance(self.sender_icon_urls, dict):
            logging.warning("sender_icon_urls is not a dictionary. Please check settings.yaml.")
            self.sender_icon_urls = {}
        self.reply_fallback_message = params.get('reply_fallback_message', '...')

    def _build_template_actions(self, choices, action_token):
        results = []
        if choices is None: return results
        for choice in choices:
            if len(choice) == 0:
                continue
            elif len(choice) == 1:
                results.append(MessageAction(choice[0], choice[0]))
            elif len(choice) == 2:
                if re.match(r'^(https?|tel):', choice[1]):
                    results.append(URIAction(choice[0], choice[1]))
                elif re.match(r'^[#*]', choice[1]):
                    results.append(PostbackAction(label=choice[0], display_text=choice[0], data=utility.encode_action_string(choice[1], action_token=action_token)))
                else:
                    results.append(MessageAction(choice[0], choice[1]))
            elif len(choice) >= 3:
                if choice[1]:
                    results.append(PostbackAction(label=choice[0], display_text=choice[1], data=utility.encode_action_string(choice[2], action_token=action_token)))
                else:
                    results.append(PostbackAction(label=choice[0], data=utility.encode_action_string(choice[2], action_token=action_token)))
        return results

    def _build_quick_reply_actions(self, choices, action_token):
        results = []
        if choices is None: return results
        for choice in choices:
            action = None
            if len(choice) == 0:
                continue
            elif len(choice) == 1:
                action = MessageAction(choice[0], choice[0])
            elif len(choice) == 2:
                if re.match(r'^(https?|tel):', choice[1]):
                    action = URIAction(choice[0], choice[1])
                elif re.match(r'^[#*]', choice[1]):
                    action = PostbackAction(label=choice[0], display_text=choice[0], data=utility.encode_action_string(choice[1], action_token=action_token))
                else:
                    action = MessageAction(choice[0], choice[1])
            elif len(choice) >= 3:
                if choice[1]:
                    action = PostbackAction(label=choice[0], display_text=choice[1], data=utility.encode_action_string(choice[2], action_token=action_token))
                else:
                    action = PostbackAction(label=choice[0], data=utility.encode_action_string(choice[2], action_token=action_token))
            if action is not None:
                results.append(QuickReplyButton(action=action))
        return results

    def _make_sender(self, sender):
        if sender is None:
            return None
        else:
            return Sender(name=sender, icon_url=self.sender_icon_urls.get(sender, None))

    def _template_message(self, template, sender):
        return TemplateSendMessage(self.alt_text, template, sender=self._make_sender(sender))

    def construct_response(self, context, sender, msg, options, children=[]):
        if msg == '@confirm' or msg == '@確認':
            if len(options) > 0:
                context.response.append(self._template_message(ConfirmTemplate(text=options[0], actions=self._build_template_actions(children, context.status.action_token)), sender))
            else:
                logging.error("invalid format: @confirm")
                context.response.append(TextSendMessage(text="<<@confirmを解釈できませんでした>>"))
        elif msg == '@button' or msg == '@ボタン':
            if len(options) > 0:
                title = utility.safe_list_get(options, 1, None)
                image_url = options[2] if len(options) > 2 else None
                send_message = self._template_message(ButtonsTemplate(text=options[0], title=title, thumbnail_image_url=image_url, actions=self._build_template_actions(children, context.status.action_token)), sender)
                #logging.warning(json.dumps(children))
                #logging.warning(json.dumps(send_message.as_json_dict()))
                context.response.append(send_message)
            else:
                logging.error("invalid format: @button")
                context.response.append(TextSendMessage(text="<<@buttonを解釈できませんでした>>"))
        elif msg == '@carousel' or msg == '@カルーセル' or msg == '@panel' or msg == '@パネル':
            panel_templates = []
            for panel, choices in children:
                title = utility.safe_list_get(panel, 1, None)
                image_url = panel[2] if len(panel) > 2 else None
                panel_templates.append(
                    CarouselColumn(text=panel[0], title=title, thumbnail_image_url=image_url, actions=self._build_template_actions(choices, context.status.action_token))
                )
            context.response.append(self._template_message(CarouselTemplate(panel_templates), sender))
        elif msg == '@imagemap' or msg == '@イメージマップ':
            try:
                url = options[0]
                if url is None:
                    raise ValueError

                if len(options) < 3:
                    raise ValueError

                width = int(options[1])
                height = int(options[2])

                imagemap_actions = []
                for arg in children:
                    coord = list(map(int, arg[0].split(',')))
                    area = ImagemapArea(coord[0], coord[1], coord[2], coord[3])

                    if re.match(r'^(https?|tel):', arg[1]):
                        imagemap_action = URIImagemapAction(arg[1], area)
                    else:
                        imagemap_action = MessageImagemapAction(arg[1], area)
                    imagemap_actions.append(imagemap_action)
                send_message = ImagemapSendMessage(base_url=url, alt_text=self.alt_text, base_size=BaseSize(width, height), actions=imagemap_actions, sender=self._make_sender(sender))
                #logging.warning(json.dumps(send_message.as_json_dict()))
                context.response.append(send_message)
            except (ValueError, IndexError):
                logging.error("invalid format: @imagemap")
                context.response.append(TextSendMessage(text="<<@imagemapを解釈できませんでした>>"))
        elif msg in FLEX_CMDS:
            try:
                flex_data = json.loads(options[0]) if len(options) > 0 else {}
                if type(flex_data) == str:
                    flex_data = json.loads(flex_data)
                # flex_data の中をたどって "action" キーの type が "jump" だったら "postback" に書き換える
                def rewrite_action_data(obj):
                    if isinstance(obj, dict):
                        if 'action' in obj and isinstance(obj['action'], dict) and 'type' in obj['action'] and obj['action']['type'] == 'jump':
                            action = obj['action']
                            data = action.get('data', '')
                            hankaku_data = normalize('NFKC', data.strip())
                            if re.match(r'^[#*]', hankaku_data):
                                action['type'] = 'postback'
                                action['data'] = utility.encode_action_string(hankaku_data, action_token=context.status.action_token)
                            else:
                                action['type'] = 'message'
                                action['text'] = data
                                if 'data' in action:
                                    del action['data']
                        for k, v in obj.items():
                            rewrite_action_data(v)
                    elif isinstance(obj, list):
                        for item in obj:
                            rewrite_action_data(item)
                rewrite_action_data(flex_data)

                send_message = FlexSendMessage(alt_text=self.alt_text, sender=self._make_sender(sender))
                # 無理矢理 FlexSendMessage の中身を書き換える
                send_message.contents = flex_data
                context.response.append(send_message)
            except json.JSONDecodeError:
                logging.error("invalid format: @flex")
                context.response.append(TextSendMessage(text="<<@flexを解釈できませんでした>>"))
        elif msg in REPLY_CMDS:
            if len(context.response) == 0:
                logging.warning("@reply: no preceding message, using fallback (possible duplicate quick_reply in scenario)")
                context.response.append(TextSendMessage(text=self.reply_fallback_message))
            context.response[-1].quick_reply = QuickReply(items=self._build_quick_reply_actions(children, context.status.action_token))
        elif msg in RICHMENU_CMDS:
            richmenu_id = options[0]
            interface = context.get_interface('line')
            if interface and interface.line_bot_api:
                interface.line_bot_api.link_rich_menu_to_user(context.source_id, richmenu_id)
            else:
                logging.error("invalid interface: @menu")
        # 解釈はここで終了
        return True


def inner_load_plugin(params):
    builder = LineDefaultCommandsPlugin_Builder(params)
    runtime = LineDefaultCommandsPlugin_Runtime(params)
    hub.register_handler(
        service='line',
        builder=builder,
        runtime=runtime)
    commands.register_commands([
        commands.CommandEntry(
            names=CONFIRM_CMDS,
            options='text(240)',
            child='text [text|raw|label] [label]',
            builder=builder,
            runtime=runtime,
            service='line',
            specs={'children_min': 1, 'children_max': 2}),
        commands.CommandEntry(
            names=BUTTON_CMDS,
            options='text(160) [text(40)] [image]',
            child='text [text|raw|label] [label]',
            builder=builder,
            runtime=runtime,
            service='line',
            specs={'children_min': 1, 'children_max': 4}),
        commands.CommandEntry(
            names=IMAGEMAP_CMDS,
            options='image',
            # 最後のhankaku引数は実際には付けられないが、間違えてラベルを指定されたことを lint_choice で検知するために付けている
            child='text text|label [hankaku]',
            builder=builder,
            runtime=runtime,
            service='line',
            specs={'children_max': 49}),
        commands.CommandEntry(
            names=PANEL_CMDS,
            options='',
            child='text(120) [text(40)] [image]',
            grandchild='text [text|raw|label] [label]',
            builder=builder,
            runtime=runtime,
            service='line',
            specs={'children_min': 1, 'children_max': 10}),
        commands.CommandEntry(
            names=FLEX_CMDS,
            options='text',
            builder=builder,
            runtime=runtime,
            service='line'),
        commands.CommandEntry(
            names=REPLY_CMDS,
            options='',
            child='text [text|raw|label] [label]',
            builder=builder,
            runtime=runtime,
            service='line',
            specs={'children_min': 1, 'children_max': 13}),
        commands.CommandEntry(
            names=RICHMENU_CMDS,
            options='text',
            builder=builder,
            runtime=runtime,
            service='line'),
    ])
