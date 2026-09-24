# coding: utf-8

import hub
import commands
import utility
import context
import json
from urllib.parse import urlsplit


class LiffPlugin_ActionContext(context.ActionContext):
    def __init__(self, bot_name, interface, user, action, attrs):
        context.ActionContext.__init__(self, bot_name, "liff", interface, user, action, attrs)
        self.ignore_unhandled_action = interface.ignore_unhandled_action


class LiffPlugin_Interface(object):
    def __init__(self, bot_name, params):
        self.bot_name = bot_name
        self.params = params
        self.allow_origin = params['allow_origin']
        origins = self.allow_origin if isinstance(self.allow_origin, list) else [self.allow_origin]
        if self.allow_origin != '*':
            for origin in origins:
                if not isinstance(origin, str):
                    raise ValueError('LIFFのallow_originにはoriginかoriginの配列を指定してください')
                parsed = urlsplit(origin)
                if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                        or parsed.path or parsed.query or parsed.fragment or parsed.username
                        or parsed.password or any(c.isspace() for c in origin)):
                    raise ValueError('LIFFのallow_originにはoriginだけを指定してください')
        self.login_channel_id = params.get('login_channel_id')
        if self.login_channel_id is not None and (
                not isinstance(self.login_channel_id, str) or not self.login_channel_id.isascii()
                or not self.login_channel_id.isdecimal()):
            raise ValueError('login_channel_idにはLINE LoginチャネルIDを文字列で指定してください')
        self.ignore_unhandled_action = params.get('ignore_unhandled_action', False)
        if type(self.ignore_unhandled_action) is not bool:
            raise ValueError('ignore_unhandled_actionはboolにしてください')
        self.action_prefix = params.get('action_prefix', "##liff.")

    def get_service_list(self):
        return {"liff": self}

    def get_retry_count(self):
        return self.params.get('retry_count', 3)

    def create_context(self, user, action, attrs):
        return LiffPlugin_ActionContext(self.bot_name, self, user, action, attrs)

    def respond_reaction(self, context, reactions):
        context.response = []
        for reaction, children in reactions:
            sender = reaction[0]
            msg = reaction[1]
            options = reaction[2:] if len(reaction) > 2 else []

            if commands.invoke_runtime_construct_response(context, sender, msg, options, children):
                # コマンド毎の処理メソッドの中で context.response への追加が行われている
                pass
            else:
                #text = msg if sender is None else sender + "：\n" + msg
                # LIFFではsenderを無視する
                text = msg
                context.response.append(text)

        return json.dumps(context.response)


class LiffPlugin_InterfaceFactory(object):
    def __init__(self, params):
        self.params = params

    def create_interface(self, bot_name, params):
        return LiffPlugin_Interface(bot_name, utility.merge_params(self.params, params))


def inner_load_plugin(plugin_params):
    hub.register_interface_factory(type_name="liff",
                                   factory=LiffPlugin_InterfaceFactory(plugin_params))
    from .richmenu import register_runtime
    register_runtime(plugin_params)
