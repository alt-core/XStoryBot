# coding: utf-8

import re

from linebot import LineBotApi, WebhookParser
from linebot.models import MessageEvent, PostbackEvent, FollowEvent, UnfollowEvent, JoinEvent, LeaveEvent, TextMessage, LocationMessage, StickerMessage, TextSendMessage, ImageSendMessage, TemplateSendMessage, \
    CarouselColumn, ImagemapSendMessage, ImagemapArea, MessageImagemapAction

# SSL 警告を防ぐため / 今回のケースでは問題ないが、行儀は良くない
try:
    import requests.packages.urllib3
    requests.packages.urllib3.disable_warnings()
except ImportError:
    pass

from context import ActionContext
from users import User
import hub
import commands
import utility


class LinePlugin_ActionContext(ActionContext):
    def __init__(self, bot_name, interface, user, action, event):
        self.event = event
        source_type, source_id = user.user_id.split(',')
        self.source_type = source_type
        self.source_id = source_id
        ActionContext.__init__(self, bot_name, "line", interface, user, action)


class LinePlugin_Interface(object):
    def __init__(self, bot_name, params):
        self.bot_name = bot_name
        self.params = params
        self.line_access_token = params['line_access_token']
        self.line_channel_secret = params['line_channel_secret']
        self.line_bot_api = LineBotApi(self.line_access_token, timeout=30)
        self.parser = WebhookParser(self.line_channel_secret)

    def get_service_list(self):
        return {'line': self}

    def create_context(self, user, action):
        return LinePlugin_ActionContext(self.bot_name, self, user, action, event=None)

    def create_context_from_line_event(self, event):
        user = User("line", event.source.type + ',' + event.source.sender_id)
        action = self._construct_action(event)
        return LinePlugin_ActionContext(self.bot_name, self, user, action, event)

    @staticmethod
    def _construct_action(event):
        if isinstance(event, MessageEvent):
            if isinstance(event.message, TextMessage):
                return event.message.text
            elif isinstance(event.message, LocationMessage):
                return u"LOC:{},{}({},{})".format(event.message.title, event.message.address, event.message.latitude, event.message.longitude)
            elif isinstance(event.message, StickerMessage):
                return u"STK:{},{}".format(event.message.package_id, event.message.sticker_id)
            else:
                # 画像・動画・音声メッセージ
                return u"ETC:{}".format(event.message.type)
        elif isinstance(event, PostbackEvent):
            data, visit_id = event.postback.data.split(u'@@')
            # TODO: ちゃんと visit_id が送れないケースがあるのでコメントアウト
            #        if context.status.visit_id != visit_id and visit_id != u'FORWARD':
            #            # 古いシーンの選択肢が送られてきた
            #            logging.info(u'received postback with invalid visit_id')
            #            return
            return data
        elif isinstance(event, (FollowEvent, UnfollowEvent, JoinEvent, LeaveEvent)):
            return u'##' + event.type

    def respond_reaction(self, context, reactions):
        msgs = self._construct_responses(context, reactions)
        if len(msgs) > 5:
            msgs = [TextSendMessage(text=u'内部エラー: 送信するメッセージが多すぎます')]
        self._reply_message(context, msgs)
        return 'OK' # LINE では respond_reaction の返値は見ていない

    def _reply_message(self, context, messages):
        if context.event is not None:
            self.line_bot_api.reply_message(context.event.reply_token, messages)
        else:
            # API 経由で起動された場合は reply_token がない
            self.line_bot_api.push_message(context.source_id, messages)

    def _construct_responses(self, context, reactions):
        response = []
        context.response = response
        for reaction, children in reactions:
            msg = reaction[0]
            options = reaction[1:] if len(reaction) > 1 else []
            if commands.invoke_runtime_construct_response(context, msg, options, children):
                # コマンド毎の処理メソッドの中で context.response への追加が行われている
                pass
            elif msg == u'@image' or msg == u'@画像':
                url = options[0]
                response.append(ImageSendMessage(self.get_image_url(url), self.get_image_url(url, 'preview')))

            else:
                response.append(TextSendMessage(text=msg))
        return response

    @staticmethod
    def get_image_url(url, option = None):
        if url is None:
            return None
        image_url = url
        if option == "preview":
            image_url = re.sub(r'_1024$', '_240', image_url)
        return image_url


class LinePlugin_InterfaceFactory(object):
    def __init__(self, params):
        self.params = params

    def create_interface(self, bot_name, params):
        return LinePlugin_Interface(bot_name, utility.merge_params(self.params, params))


def inner_load_plugin(plugin_params):
    hub.register_interface_factory(type_name="line",
                                   factory=LinePlugin_InterfaceFactory(plugin_params))
