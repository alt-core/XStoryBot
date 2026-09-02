import re
from urllib.parse import urlsplit

import commands
from common_commands import AUDIO_CMDS, IMAGE_CMDS, RAWIMAGE_CMDS, VIDEO_CMDS
from plugin.line.command_names import (
    BUTTON_CMDS,
    CONFIRM_CMDS,
    FLEX_CMDS,
    IMAGEMAP_CMDS,
    PANEL_CMDS,
    REPLY_CMDS,
    RICHMENU_CMDS,
)
from plugin.webchat.errors import BotNotWebCompatible


def _safe_get(values, index, default=None):
    if values is None or index >= len(values):
        return default
    value = values[index]
    return default if value == '' else value


class WebchatPresenter:
    """既存reactionをframework非依存MessageSpecへ変換する。"""

    def __init__(self, interface):
        self.interface = interface

    def _sender(self, sender):
        if sender is None:
            return None
        icon_url = self.interface.sender_icon_urls.get(sender)
        if icon_url:
            icon_url = self.interface.validate_media_url(icon_url)
        return {
            'name': sender,
            'icon_url': icon_url,
        }

    def _message(self, message_type, sender, **values):
        message = {
            'role': 'assistant',
            'sender': self._sender(sender),
            'type': message_type,
        }
        message.update(values)
        return message

    @staticmethod
    def _is_safe_uri(value):
        parsed = urlsplit(value)
        if parsed.scheme.lower() in ('http', 'https'):
            return (
                parsed.hostname is not None
                and parsed.username is None
                and parsed.password is None
            )
        return parsed.scheme.lower() == 'tel' and bool(parsed.path)

    def _action(self, context, choice):
        if not choice:
            return None
        label = str(choice[0])
        if len(choice) == 1:
            return {'type': 'message', 'label': label, 'text': label}

        value = str(choice[1])
        if len(choice) >= 3 and choice[2]:
            resolved = str(choice[2])
            if not re.match(r'^[#*]', resolved):
                raise BotNotWebCompatible('postback labelが不正です')
            return self.interface.make_postback_action(
                context, label, resolved, value or None)
        if self._is_safe_uri(value):
            return {'type': 'uri', 'label': label, 'href': value}
        if re.match(r'^[#*]', value):
            return self.interface.make_postback_action(
                context, label, value, label)
        return {'type': 'message', 'label': label, 'text': value}

    def _actions(self, context, choices):
        return [
            action for action in (
                self._action(context, choice) for choice in (choices or []))
            if action is not None
        ]

    def construct_template(self, context, sender, msg, options, children):
        if msg in FLEX_CMDS:
            raise BotNotWebCompatible('FlexはWebchat初期対象外です')
        if msg in PANEL_CMDS:
            raise BotNotWebCompatible('CarouselはWebchat初期対象外です')
        if msg in RICHMENU_CMDS:
            return True
        if msg in REPLY_CMDS:
            if not context.response:
                context.response.append(self._message(
                    'text', sender,
                    text=self.interface.reply_fallback_message))
            context.response[-1]['quick_replies'] = self._actions(
                context, children)
            return True
        if msg in IMAGEMAP_CMDS:
            try:
                width = int(options[1])
                height = int(options[2])
            except (IndexError, TypeError, ValueError) as error:
                raise BotNotWebCompatible('Imagemapの形式が不正です') from error
            areas = []
            for child in children or []:
                try:
                    x, y, area_width, area_height = [
                        int(value) for value in str(child[0]).split(',')]
                    value = str(child[1])
                except (IndexError, TypeError, ValueError) as error:
                    raise BotNotWebCompatible(
                        'Imagemap actionの形式が不正です') from error
                if self._is_safe_uri(value):
                    action = {'type': 'uri', 'href': value, 'label': value}
                else:
                    action = {
                        'type': 'message',
                        'text': value,
                        'label': value,
                        'echo_text': value,
                    }
                areas.append({
                    'x': x,
                    'y': y,
                    'width': area_width,
                    'height': area_height,
                    'action': action,
                })
            base_url = str(options[0]).rstrip('/')
            sources = [
                {
                    'url': self.interface.validate_media_url(
                        f'{base_url}/{source_width}'),
                    'width': source_width,
                }
                for source_width in (460, 1040)
            ]
            context.response.append(self._message(
                'imagemap', sender,
                image_url=sources[-1]['url'],
                sources=sources,
                width=width,
                height=height,
                alt=self.interface.alt_text,
                areas=areas,
            ))
            return True

        if msg in CONFIRM_CMDS or msg in BUTTON_CMDS:
            text = _safe_get(options, 0, '')
            title = _safe_get(options, 1)
            image_url = _safe_get(options, 2)
            if image_url:
                image_url = self.interface.validate_media_url(image_url)
            context.response.append(self._message(
                'button', sender,
                text=text,
                title=title,
                image_url=image_url,
                actions=self._actions(context, children),
            ))
            return True
        return False

    def present(self, context, reactions):
        context.response = []
        for reaction, children in reactions:
            sender = reaction[0]
            msg = reaction[1]
            options = reaction[2:] if len(reaction) > 2 else []
            if commands.invoke_runtime_construct_response(
                    context, sender, msg, options, children):
                continue
            if msg in IMAGE_CMDS:
                url = self.interface.validate_media_url(options[0])
                preview_url = self.interface.validate_media_url(
                    self.interface.preview_image_url(url))
                context.response.append(self._message(
                    'image', sender,
                    original_url=url,
                    preview_url=preview_url,
                    alt='画像'))
            elif msg in RAWIMAGE_CMDS:
                original_url = self.interface.validate_media_url(options[0])
                preview_url = self.interface.validate_media_url(
                    _safe_get(options, 1, options[0]))
                context.response.append(self._message(
                    'image', sender,
                    original_url=original_url,
                    preview_url=preview_url,
                    alt='画像'))
            elif msg in VIDEO_CMDS:
                poster_url = self.interface.validate_media_url(options[0])
                video_url = self.interface.validate_media_url(options[1])
                values = {
                    'poster_url': poster_url,
                    'url': video_url,
                }
                if len(options) > 2 and options[2]:
                    values['completion_action'] = (
                        self.interface.make_postback_action(
                            context, '', str(options[2]), None))
                context.response.append(self._message(
                    'video', sender, **values))
            elif msg in AUDIO_CMDS:
                audio_url = self.interface.validate_media_url(options[0])
                context.response.append(self._message(
                    'audio', sender,
                    url=audio_url,
                    duration_ms=int(options[1]),
                    mime_type=_safe_get(options, 2)))
            elif msg.startswith('@'):
                raise BotNotWebCompatible(
                    f'Webchatで表示できないcommandです: {msg}')
            else:
                context.response.append(self._message(
                    'text', sender, text=msg))
        return context.response


class WebchatTemplateRuntime:
    def construct_response(self, context, sender, msg, options, children=None):
        interface = context.get_interface('webchat')
        return interface._presenter.construct_template(
            context, sender, msg, options, children or [])


def register_runtime():
    runtime = WebchatTemplateRuntime()
    for names in (
            CONFIRM_CMDS, BUTTON_CMDS, PANEL_CMDS, IMAGEMAP_CMDS,
            FLEX_CMDS, REPLY_CMDS, RICHMENU_CMDS):
        commands.register_commands([
            commands.CommandEntry(
                names=names,
                child='raw',
                runtime=runtime,
                service='webchat')])
