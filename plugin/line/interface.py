# coding: utf-8

import re
import logging
import uuid
import time
# import urllib3

from linebot import LineBotApi, WebhookParser
from linebot.models import MessageEvent, PostbackEvent, VideoPlayCompleteEvent, BeaconEvent, FollowEvent, UnfollowEvent, JoinEvent, LeaveEvent, MemberJoinedEvent, MemberLeftEvent, TextMessage, ImageMessage, VideoMessage, AudioMessage, FileMessage, LocationMessage, StickerMessage, TextSendMessage, ImageSendMessage, VideoSendMessage, AudioSendMessage, TemplateSendMessage, \
    CarouselColumn, ImagemapSendMessage, ImagemapArea, MessageImagemapAction, Sender

# # SSL 警告を抑制
# urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from requests import RequestException

from common_commands import AUDIO_CMDS, IMAGE_CMDS, VIDEO_CMDS, RAWIMAGE_CMDS
from context import ActionContext
from users import User
import hub
import commands
import utility


LINE_API_RETRY_COUNT = 5 # LINE のサーバへの送信時のエラー再送回数のデフォルト値
LINE_API_RETRY_SLEEP = 0.1 # リトライ時のスリープ時間
LINE_ABORT_DURATION = 0 # timestamp からこれ以上遅れていると実行を諦める / 0 は無効を表す


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
        self.sender_icon_urls = params.get('sender_icon_urls', {})
        if not isinstance(self.sender_icon_urls, dict):
            logging.warning("sender_icon_urls is not a dictionary. Please check settings.yaml.")
            self.sender_icon_urls = {}
        self.line_api_retry_count = int(params.get('line_api_retry_count', LINE_API_RETRY_COUNT))
        self.line_api_retry_sleep = float(params.get('line_api_retry_sleep', LINE_API_RETRY_SLEEP))
        self.line_abort_duration_ms = float(params.get('line_abort_duration', LINE_ABORT_DURATION)) * 1000
        self.line_abort_duration_dont_break = not not params.get('line_abort_duration_dont_break', False)

    def get_service_list(self):
        return {'line': self}

    def get_retry_count(self):
        return self.params.get('retry_count', 3)

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
        user = User("line", f"{event.source.type},{sender_id}")
        action, attrs = self._construct_action(event)
        if action is not None:
            return LinePlugin_ActionContext(self.bot_name, self, user, action, attrs, event)
        else:
            return None

    def _construct_action(self, event):
        attrs = {'line.event.type': event.type}
        if isinstance(event, MessageEvent):
            if isinstance(event.message, TextMessage):
                text = event.message.text
                if not self.allow_special_action_text_for_debug:
                    text = utility.sanitize_action(text)
                return text, attrs
            elif isinstance(event.message, LocationMessage):
                return f":LINE_LOCATION:{event.message.title},{event.message.latitude},{event.message.longitude},{event.message.address}", attrs
            elif isinstance(event.message, StickerMessage):
                return f":LINE_STICKER:{event.message.package_id},{event.message.sticker_id}", attrs
            elif isinstance(event.message, ImageMessage) and event.message.content_provider is not None and event.message.content_provider.type == 'line':
                return f":LINE_IMAGE:{event.message.id}", attrs
            elif isinstance(event.message, VideoMessage) and event.message.content_provider is not None and event.message.content_provider.type == 'line':
                return f":LINE_VIDEO:{event.message.id},{event.message.duration}", attrs
            elif isinstance(event.message, AudioMessage) and event.message.content_provider is not None and event.message.content_provider.type == 'line':
                return f":LINE_AUDIO:{event.message.id},{event.message.duration}", attrs
            elif isinstance(event.message, FileMessage):
                return f":LINE_FILE:{event.message.id},{event.message.file_name},{event.message.file_size}", attrs
            else:
                # ここに来るものはないはず？
                return f":LINE_ETC:{event.message.type}", attrs
        elif isinstance(event, PostbackEvent):
            action, token_attrs = utility.decode_action_string(event.postback.data)
            attrs.update(token_attrs)
            return action, attrs
        elif isinstance(event, VideoPlayCompleteEvent):
            try:
                action, token_attrs = utility.decode_line_video_tracking_id(
                    event.video_play_complete.tracking_id)
            except (AttributeError, ValueError):
                # 旧形式や外部生成値は本文を記録せず、従来どおり無視する。
                logging.warning('[LINE] 動画完了tracking IDが不正です')
                return None, attrs
            attrs.update(token_attrs)
            return action, attrs
        elif isinstance(event, BeaconEvent):
            return f":LINE_BEACON:{event.beacon.type},{event.beacon.hwid}", attrs
        elif isinstance(event, (FollowEvent, UnfollowEvent, JoinEvent, LeaveEvent)):
            return f'##line.{event.type}', attrs
        else:
            # MemberJoinedEvent, MemberLeftEvent は活用が難しいので、そもそもイベントとして引き渡さない
            return None, attrs

    def respond_reaction(self, context, reactions):
        msgs = self._construct_responses(context, reactions)
        if len(msgs) > 5:
            msgs = [TextSendMessage(text='内部エラー: 送信するメッセージが多すぎます')]
        if len(msgs) == 0:
            return 'OK'
        last_e = None
        retry_key = str(uuid.uuid4()) # push の時しか使われない
        retry_sleep = self.line_api_retry_sleep
        for i_retry in range(self.line_api_retry_count):
            try:
                self._reply_message(context, msgs, retry_key=retry_key)
                return 'OK' # LINE では respond_reaction の返値は見ていない
            except RequestException as e:
                if e.response is not None and e.response.status_code == 409:
                    logging.warning('[LINE] Server already processed the request')
                    return 'OK'
                logging.error(f'[LINE] Failed to reply: {str(e)}')
                last_e = e
                time.sleep(retry_sleep)
                retry_sleep = retry_sleep * 2.0 # 指数バックオフ
        raise last_e

    def _reply_message(self, context, messages, retry_key=None):
        if context.event is not None:
            if hasattr(context.event, 'reply_token'):
                #for message in messages:
                #    logging.info(f'[LINE] {message.as_json_dict()}')
                self.line_bot_api.reply_message(context.event.reply_token, messages)
            else:
                # unfollow イベントなどは reply_token が存在しない
                logging.info(f'event {context.event.type} doesnt have reply_token: {messages}')
        else:
            # API 経由で起動された場合は reply_token がない
            self.line_bot_api.push_message(context.source_id, messages, retry_key=retry_key)

    def _make_sender(self, sender):
        if sender is None:
            return None
        else:
            return Sender(name=sender, icon_url=self.sender_icon_urls.get(sender, None))

    def _construct_responses(self, context, reactions):
        response = []
        context.response = response
        for reaction, children in reactions:
            sender = reaction[0]
            msg = reaction[1]
            options = reaction[2:] if len(reaction) > 2 else []
            if commands.invoke_runtime_construct_response(context, sender, msg, options, children):
                # コマンド毎の処理メソッドの中で context.response への追加が行われている
                pass
            elif msg in IMAGE_CMDS:
                url = options[0]
                response.append(ImageSendMessage(self.get_image_url(url), self.get_image_url(url, 'preview'), sender=self._make_sender(sender)))
            elif msg in VIDEO_CMDS:
                thumb_url = options[0]
                video_url = options[1]
                video_action = options[2] if len(options) > 2 else None
                tracking_id = None
                if video_action and context.source_type == 'user':
                    tracking_id = utility.encode_line_video_tracking_id(
                        video_action, context.status.action_token)
                elif video_action:
                    logging.warning(
                        '[LINE] group／roomでは動画完了actionを利用できません')
                response.append(VideoSendMessage(original_content_url=video_url, preview_image_url=thumb_url, tracking_id=tracking_id, sender=self._make_sender(sender)))
            elif msg in RAWIMAGE_CMDS:
                image_url = options[0]
                preview_url = options[1]
                response.append(ImageSendMessage(image_url, preview_url, sender=self._make_sender(sender)))
            elif msg in AUDIO_CMDS:
                response.append(AudioSendMessage(
                    original_content_url=options[0],
                    duration=int(options[1]),
                    sender=self._make_sender(sender)))
            else:
                response.append(TextSendMessage(text=msg, sender=self._make_sender(sender)))
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
