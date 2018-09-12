# coding: utf-8

import re
import logging

from linebot import LineBotApi, WebhookParser
from linebot.models import MessageEvent, PostbackEvent, BeaconEvent, FollowEvent, UnfollowEvent, JoinEvent, LeaveEvent, TextMessage, LocationMessage, StickerMessage, TextSendMessage, ImageSendMessage, TemplateSendMessage, \
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
    def __init__(self, bot_name, interface, user, action, attrs, event):
        self.event = event
        source_type, source_id = user.user_id.split(',')
        self.source_type = source_type
        self.source_id = source_id
        ActionContext.__init__(self, bot_name, "line", interface, user, action, attrs)


class LinePlugin_Interface(object):
    def __init__(self, bot_name, params):
        self.bot_name = bot_name
        self.params = params
        self.line_access_token = params['line_access_token']
        self.line_channel_secret = params['line_channel_secret']
        self.line_bot_api = LineBotApi(self.line_access_token, timeout=30)
        self.allow_special_action_text_for_debug = params.get('allow_special_action_text_for_debug', False)
        self.parser = WebhookParser(self.line_channel_secret)

    def get_service_list(self):
        return {'line': self}

    def create_context(self, user, action, attrs):
        return LinePlugin_ActionContext(self.bot_name, self, user, action, attrs, event=None)

    def create_context_from_line_event(self, event):
        sender_id = None
        if event.source.type == 'user':
            sender_id = event.source.user_id
        elif event.source.type == 'group':
            sender_id = event.source.group_id
        elif event.source.type == 'room':
            sender_id = event.source.room_id
        else:  
            raise NotImplementedError
        user = User("line", event.source.type + ',' + sender_id)
        action, attrs = self._construct_action(event)
        return LinePlugin_ActionContext(self.bot_name, self, user, action, attrs, event)

    def _construct_action(self, event):
        attrs = {'line.event.type': event.type}
        if isinstance(event, MessageEvent):
            if isinstance(event.message, TextMessage):
                text = event.message.text
                if not self.allow_special_action_text_for_debug:
                    text = utility.sanitize_action(text)
                return text, attrs
            elif isinstance(event.message, LocationMessage):
                return u"LINE_LOCATION:{},{}({},{})".format(event.message.title, event.message.address, event.message.latitude, event.message.longitude), attrs
            elif isinstance(event.message, StickerMessage):
                return u"LINE_STICKER:{},{}".format(event.message.package_id, event.message.sticker_id), attrs
            else:
                # 画像・動画・音声メッセージ
                return u"LINE_ETC:{}".format(event.message.type), attrs
        elif isinstance(event, PostbackEvent):
            action, token_attrs = utility.decode_action_string(event.postback.data)
            attrs.update(token_attrs)
            return action, attrs
        elif isinstance(event, BeaconEvent):
            return u"LINE_BEACON:{},{}".format(event.beacon.type, event.beacon.hwid), attrs
        elif isinstance(event, (FollowEvent, UnfollowEvent, JoinEvent, LeaveEvent)):
            return u'##line.' + event.type, attrs

    def respond_reaction(self, context, reactions):
        msgs = self._construct_responses(context, reactions)
        if len(msgs) > 5:
            msgs = [TextSendMessage(text=u'内部エラー: 送信するメッセージが多すぎます')]
        if len(msgs) == 0:
            return 'OK'
        self._reply_message(context, msgs)
        return 'OK' # LINE では respond_reaction の返値は見ていない

    def _reply_message(self, context, messages):
        if context.event is not None:
            if hasattr(context.event, 'reply_token'):
                self.line_bot_api.reply_message(context.event.reply_token, messages)
            else:
                # unfollow イベントなどは reply_token が存在しない
                logging.info(u'event {} doesnt have reply_token: {}'.format(context.event.type, messages))
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
            image_url = re.sub(r'_1024\.', '_240.', image_url)
        return image_url


class LinePlugin_InterfaceFactory(object):
    def __init__(self, params):
        self.params = params

    def create_interface(self, bot_name, params):
        return LinePlugin_Interface(bot_name, utility.merge_params(self.params, params))


def inner_load_plugin(plugin_params):
    hub.register_interface_factory(type_name="line",
                                   factory=LinePlugin_InterfaceFactory(plugin_params))
