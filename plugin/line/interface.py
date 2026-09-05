# coding: utf-8

import re
import logging
import uuid
import time

from requests import RequestException

from common_commands import AUDIO_CMDS, IMAGE_CMDS, VIDEO_CMDS, RAWIMAGE_CMDS
from context import ActionContext
from users import User
import hub
import commands
import utility
from plugin.line import messages as line_messages
from plugin.line import webhook
from plugin.line.api import LineApiClient, LineApiError


LINE_API_RETRY_COUNT = 5 # LINE のサーバへの送信時のエラー再送回数のデフォルト値
LINE_API_RETRY_SLEEP = 0.1 # リトライ時のスリープ時間
LINE_ABORT_DURATION = 0 # timestamp からこれ以上遅れていると実行を諦める / 0 は無効を表す


def _error_status_code(error):
    # LineApiError は status_code を持つ。requests 系の例外は response 経由で持つことがある
    status = getattr(error, 'status_code', None)
    if status is None:
        status = getattr(getattr(error, 'response', None), 'status_code', None)
    return status


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
        self.api = LineApiClient(self.line_access_token)
        self.allow_special_action_text_for_debug = params.get('allow_special_action_text_for_debug', False)
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

    def parse_webhook(self, body, signature):
        """署名を検証して event（LINE の JSON をそのまま dict にしたもの）の list を返す。
        署名が一致しなければ webhook.InvalidSignatureError。"""
        return webhook.parse(self.line_channel_secret, body, signature)

    def create_context_from_line_event(self, event):
        source = event.get('source') or {}
        source_type = source.get('type')
        if source_type == 'user':
            sender_id = source.get('userId')
        elif source_type == 'group':
            sender_id = source.get('groupId')
        elif source_type == 'room':
            sender_id = source.get('roomId')
        else:
            raise NotImplementedError
        user = User("line", f"{source_type},{sender_id}")
        action, attrs = self._construct_action(event)
        if action is not None:
            return LinePlugin_ActionContext(self.bot_name, self, user, action, attrs, event)
        else:
            return None

    def _construct_action(self, event):
        # event の形は https://developers.line.biz/ja/reference/messaging-api/#webhook-event-objects
        event_type = event.get('type')
        attrs = {'line.event.type': event_type}
        if event_type == 'message':
            message = event['message']
            message_type = message.get('type')
            provider_type = (message.get('contentProvider') or {}).get('type')
            if message_type == 'text':
                text = message['text']
                if not self.allow_special_action_text_for_debug:
                    text = utility.sanitize_action(text)
                return text, attrs
            elif message_type == 'location':
                return f":LINE_LOCATION:{message.get('title')},{message.get('latitude')},{message.get('longitude')},{message.get('address')}", attrs
            elif message_type == 'sticker':
                return f":LINE_STICKER:{message.get('packageId')},{message.get('stickerId')}", attrs
            elif message_type == 'image' and provider_type == 'line':
                return f":LINE_IMAGE:{message.get('id')}", attrs
            elif message_type == 'video' and provider_type == 'line':
                return f":LINE_VIDEO:{message.get('id')},{message.get('duration')}", attrs
            elif message_type == 'audio' and provider_type == 'line':
                return f":LINE_AUDIO:{message.get('id')},{message.get('duration')}", attrs
            elif message_type == 'file':
                return f":LINE_FILE:{message.get('id')},{message.get('fileName')},{message.get('fileSize')}", attrs
            else:
                # 外部 provider の画像等、ここに来るものはないはず？
                return f":LINE_ETC:{message_type}", attrs
        elif event_type == 'postback':
            action, token_attrs = utility.decode_action_string(event['postback']['data'])
            attrs.update(token_attrs)
            return action, attrs
        elif event_type == 'videoPlayComplete':
            try:
                action, token_attrs = utility.decode_line_video_tracking_id(
                    event['videoPlayComplete']['trackingId'])
            except (AttributeError, KeyError, TypeError, ValueError):
                # 旧形式や外部生成値は本文を記録せず、従来どおり無視する。
                logging.warning('[LINE] 動画完了tracking IDが不正です')
                return None, attrs
            attrs.update(token_attrs)
            return action, attrs
        elif event_type == 'beacon':
            beacon = event.get('beacon') or {}
            return f":LINE_BEACON:{beacon.get('type')},{beacon.get('hwid')}", attrs
        elif event_type in ('follow', 'unfollow', 'join', 'leave'):
            return f'##line.{event_type}', attrs
        else:
            # memberJoined、memberLeft や未知の event は活用が難しいので、そもそもイベントとして引き渡さない
            return None, attrs

    def respond_reaction(self, context, reactions):
        msgs = self._construct_responses(context, reactions)
        if len(msgs) > 5:
            msgs = [line_messages.text('内部エラー: 送信するメッセージが多すぎます')]
        if len(msgs) == 0:
            return 'OK'
        last_e = None
        retry_key = str(uuid.uuid4()) # push の時しか使われない
        retry_sleep = self.line_api_retry_sleep
        for i_retry in range(self.line_api_retry_count):
            try:
                self._reply_message(context, msgs, retry_key=retry_key)
                return 'OK' # LINE では respond_reaction の返値は見ていない
            except (RequestException, LineApiError) as e:
                status = _error_status_code(e)
                if status == 409:
                    logging.warning('[LINE] Server already processed the request')
                    return 'OK'
                if status is not None and 400 <= status < 500 and status != 429:
                    # リクエスト自体の誤り（reply token の期限切れなど）は再送しても直らない
                    logging.error(f'[LINE] Request rejected: {status} {str(e)}')
                    raise
                logging.error(f'[LINE] Failed to reply: {str(e)}')
                last_e = e
                time.sleep(retry_sleep)
                retry_sleep = retry_sleep * 2.0 # 指数バックオフ
        raise last_e

    def _reply_message(self, context, messages, retry_key=None):
        if context.event is not None:
            if 'replyToken' in context.event:
                self.api.reply(context.event['replyToken'], messages)
            else:
                # unfollow／leave や standby mode の event には replyToken が無い
                logging.info(f"event {context.event.get('type')} doesnt have reply_token: {messages}")
        else:
            # API 経由で起動された場合は reply_token がない
            self.api.push(context.source_id, messages, retry_key=retry_key)

    def _make_sender(self, sender):
        if sender is None:
            return None
        else:
            return line_messages.sender(sender, self.sender_icon_urls.get(sender, None))

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
                response.append(line_messages.image(
                    self.get_image_url(url), self.get_image_url(url, 'preview'),
                    sender=self._make_sender(sender)))
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
                response.append(line_messages.video(
                    video_url, thumb_url, tracking_id=tracking_id,
                    sender=self._make_sender(sender)))
            elif msg in RAWIMAGE_CMDS:
                image_url = options[0]
                preview_url = options[1]
                response.append(line_messages.image(
                    image_url, preview_url, sender=self._make_sender(sender)))
            elif msg in AUDIO_CMDS:
                response.append(line_messages.audio(
                    options[0], int(options[1]), sender=self._make_sender(sender)))
            else:
                response.append(line_messages.text(msg, sender=self._make_sender(sender)))
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
