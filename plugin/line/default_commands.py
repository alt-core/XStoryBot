# coding: utf-8
import logging
import re

from linebot.models import MessageEvent, PostbackEvent, FollowEvent, UnfollowEvent, JoinEvent, LeaveEvent, TextMessage, LocationMessage, StickerMessage, TextSendMessage, ImageSendMessage, TemplateSendMessage, ButtonsTemplate, ConfirmTemplate, CarouselTemplate, CarouselColumn, MessageTemplateAction, PostbackTemplateAction, URITemplateAction, ImagemapSendMessage, ImagemapArea, MessageImagemapAction, URIImagemapAction, BaseSize

import hub
import commands
import utility


BUTTON_CMDS = (u'@button', u'@ボタン')
CONFIRM_CMDS = (u'@confirm', u'@確認')
PANEL_CMDS = (u'@carousel', u'@カルーセル', u'@panel', u'@パネル')
IMAGEMAP_CMDS = (u'@imagemap', u'@イメージマップ')
ALL_TEMPLATE_CMDS = BUTTON_CMDS + CONFIRM_CMDS + PANEL_CMDS + IMAGEMAP_CMDS


class LineDefaultCommandsPlugin_Builder(object):
    def build_from_command(self, builder, msg, options, children=[], grandchildren=[]):
        if msg in CONFIRM_CMDS or msg in BUTTON_CMDS or msg in IMAGEMAP_CMDS:
            for choice in children:
                self.lint_choice(builder, msg, choice)

        if msg in CONFIRM_CMDS:
            builder.add_command(msg, options, children)

        elif msg in BUTTON_CMDS:
            builder._build_and_replace_imageurl(options, 2)
            if (len(options) > 1 and options[1] != u'') or (len(options) > 2 and options[2] != u''):
                builder.assert_strlen_from_array(options, 0, 60, u'タイトルか画像を指定した場合の文字数制限（{}文字）')
            builder.add_command(msg, options, children)

        elif msg in IMAGEMAP_CMDS:
            orig_url = builder.parse_imageurl(options[0])
            url, size = builder.build_image_for_imagemap_command(orig_url)
            options[:] = [unicode(url), unicode(size[0]), unicode(size[1])]
            builder.add_command(msg, options, children)

        elif msg in PANEL_CMDS:
            panels = []
            num_choices = -1
            flag_title = None
            flag_image = None
            for i in range(len(children)):
                panel = children[i]
                builder._build_and_replace_imageurl(panel, 2)
                if (len(panel) > 1 and panel[1] != u'') or (len(panel) > 2 and panel[2] != u''):
                    builder.assert_strlen_from_array(panel, 0, 60, u'タイトルか画像を指定した場合の文字数制限（{}文字）')
                for choice in grandchildren[i]:
                    self.lint_choice(builder, msg, choice)
                if len(grandchildren[i]) == 0:
                    builder.raise_error(u'選択肢が0個です')
                if len(grandchildren[i]) > 3:
                    builder.raise_error(u'パネルの選択肢は最大3個です')
                if num_choices != -1 and num_choices != len(grandchildren[i]):
                    builder.raise_error(u'各パネルの選択肢数がばらばらです')
                num_choices = len(grandchildren[i])
                title = utility.safe_list_get(panel, 1, u'')
                image = utility.safe_list_get(panel, 2, u'')
                if (flag_title is not None) and ((title != u'') != flag_title):
                    builder.raise_error(u'各パネルのタイトルの有無がばらばらです')
                flag_title = (title != u'')
                if (flag_image is not None) and ((image != u'') != flag_image):
                    builder.raise_error(u'各パネルの画像の有無がばらばらです')
                flag_image = (image != u'')
                panels.append([children[i], grandchildren[i]])
            builder.add_command(msg, options, panels)

        else:
            # ここには来ないはず
            builder.raise_error(u'内部エラー：未知のコマンドです')

        builder.msg_count += 1

        # 解釈はここで終了
        return True
    
    def callback_new_block(self, builder, cond):
        builder.msg_count = 0

    def build_plain_text(self, builder, msg, options):
        # 通常のテキストメッセージ表示
        # 仕様書に記述がないが、おそらく300文字が上限
        builder.assert_strlen(msg, 300)
        builder.msg_count += 1
        builder.add_command(msg, options, None)
        return True

    def callback_after_each_line(self, builder):
        if builder.msg_count > 5:
            builder.raise_error(u'6つ以上のメッセージを同時に送ろうとしました')

    def lint_choice(self, builder, msg, choice):
        action_label = choice[0]
        action_value = u''
        action_data = u''
        if len(choice) <= 1 or not choice[1]:
            action_type = 'message'
            action_value = action_label
        else:
            if utility.parse_url(choice[1]):
                action_type = 'url'
                action_value = choice[1]
            elif re.match(u'^[#＃*＊]', choice[1]):
                action_type = 'postback'
                action_data = choice[1]
            else:
                action_type = 'message'
                action_value = choice[1]
        if len(choice) > 2 and choice[2]:
            if re.match(u'^[#＃*＊]', choice[2]):
                if action_type == 'url':
                    builder.raise_error(u'アクションラベル指定時は URL を開かせることはできません', *choice)
                action_type = 'postback'
                action_data = choice[2]
            else:
                builder.raise_error(u'アクションラベルは # か * で始まらないといけません', *choice)

        if msg in IMAGEMAP_CMDS:
            if action_type == 'postback':
                builder.raise_error(u'イメージマップではアクションラベルは指定できません', *choice)
            try:
                x, y, w, h = [int(x) for x in action_label.split(u',')]
                if x < 0 or 1040 <= x or y < 0 or 1040 <= y or w <= 0 or 1040 < w or h <= 0 or 1040 < h:
                    raise ValueError
            except (ValueError, IndexError):
                builder.raise_error(u'イメージマップアクションの指定が不正です', action_label)
        else:
            builder.assert_strlen(action_label, 20)
        if action_type in ('message', 'postback'):
            builder.assert_strlen(action_value, 300)
        builder.assert_strlen(action_data, 300)
        return True


class LineDefaultCommandsPlugin_Runtime(object):
    def __init__(self, params):
        self.alt_text = params['alt_text']

    def _build_template_actions(self, choices, action_token):
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
                    results.append(PostbackTemplateAction(label=choice[0], data=utility.encode_action_string(choice[1], action_token=action_token)))
                else:
                    results.append(MessageTemplateAction(choice[0], choice[1]))
            elif len(choice) >= 3:
                if choice[1]:
                    results.append(PostbackTemplateAction(label=choice[0], text=choice[1], data=utility.encode_action_string(choice[2], action_token=action_token)))
                else:
                    results.append(PostbackTemplateAction(label=choice[0], data=utility.encode_action_string(choice[2], action_token=action_token)))
        return results

    def _template_message(self, template):
        return TemplateSendMessage(self.alt_text, template)

    def construct_response(self, context, msg, options, children):
        if msg == u'@confirm' or msg == u'@確認':
            if len(options) > 0:
                context.response.append(self._template_message(ConfirmTemplate(text=options[0], actions=self._build_template_actions(children, context.status.action_token))))
            else:
                logging.error("invalid format: @confirm")
                context.response.append(TextSendMessage(text=u"<<@confirmを解釈できませんでした>>"))
        elif msg == u'@button' or msg == u'@ボタン':
            if len(options) > 0:
                title = utility.safe_list_get(options, 1, None)
                image_url = options[2] if len(options) > 2 else None
                context.response.append(self._template_message(ButtonsTemplate(text=options[0], title=title, thumbnail_image_url=image_url, actions=self._build_template_actions(children, context.status.action_token))))
            else:
                logging.error("invalid format: @button")
                context.response.append(TextSendMessage(text=u"<<@buttonを解釈できませんでした>>"))
        elif msg == u'@carousel' or msg == u'@カルーセル' or msg == u'@panel' or msg == u'@パネル':
            panel_templates = []
            for panel, choices in children:
                title = utility.safe_list_get(panel, 1, None)
                image_url = panel[2] if len(panel) > 2 else None
                panel_templates.append(
                    CarouselColumn(text=panel[0], title=title, thumbnail_image_url=image_url, actions=self._build_template_actions(choices, context.status.action_token))
                )
            context.response.append(self._template_message(CarouselTemplate(panel_templates)))
        elif msg == u'@imagemap' or msg == u'@イメージマップ':
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
                    coord = map(int, arg[0].split(','))
                    area = ImagemapArea(coord[0], coord[1], coord[2], coord[3])

                    if re.match(r'^(https?|tel):', arg[1]):
                        imagemap_action = URIImagemapAction(arg[1], area)
                    else:
                        imagemap_action = MessageImagemapAction(arg[1], area)
                    imagemap_actions.append(imagemap_action)
                context.response.append(ImagemapSendMessage(url, self.alt_text, BaseSize(width, height), imagemap_actions))
            except (ValueError, IndexError):
                logging.error("invalid format: @imagemap")
                context.response.append(TextSendMessage(text=u"<<@imagemapを解釈できませんでした>>"))
        # 解釈はここで終了
        return True


def inner_load_plugin(params):
    builder = LineDefaultCommandsPlugin_Builder()
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
            specs={'children_min': 1, 'children_max': 5}),
    ])
