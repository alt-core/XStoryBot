# coding: utf-8

import hub
import commands
import utility
import context


class LiffPlugin_ActionContext(context.ActionContext):
    def __init__(self, bot_name, interface, user, action, attrs):
        context.ActionContext.__init__(self, bot_name, "liff", interface, user, action, attrs)


class LiffPlugin_Interface(object):
    def __init__(self, bot_name, params):
        self.bot_name = bot_name
        self.params = params
        self.allow_origin = params['allow_origin']

    def get_service_list(self):
        return {"liff": self}

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
                #text = msg if sender is None else sender + u"：\n" + msg
                # LIFFではsenderを無視する
                text = msg
                context.response.append(text)

        return u"\n".join(context.response) + u"\n"


class LiffPlugin_InterfaceFactory(object):
    def __init__(self, params):
        self.params = params

    def create_interface(self, bot_name, params):
        return LiffPlugin_Interface(bot_name, utility.merge_params(self.params, params))


def inner_load_plugin(plugin_params):
    hub.register_interface_factory(type_name="liff",
                                   factory=LiffPlugin_InterfaceFactory(plugin_params))
