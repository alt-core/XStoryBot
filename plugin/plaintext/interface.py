# coding: utf-8

import hub
import commands
import utility
import context


class PlainTextPlugin_ActionContext(context.ActionContext):
    def __init__(self, bot_name, interface, user, action):
        context.ActionContext.__init__(self, bot_name, "plaintext", interface, user, action)


class PlainTextPlugin_Interface(object):
    def __init__(self, bot_name, params):
        self.bot_name = bot_name
        self.params = params

    def get_service_list(self):
        return {"plaintext": self}

    def create_context(self, user, action):
        return PlainTextPlugin_ActionContext(self.bot_name, self, user, action)

    def respond_reaction(self, context, reactions):
        context.response = []
        for reaction, children in reactions:
            msg = reaction[0]
            options = reaction[1:] if len(reaction) > 1 else []

            if commands.invoke_runtime_construct_response(context, msg, options, children):
                # コマンド毎の処理メソッドの中で context.response への追加が行われている
                pass
            else:
                context.response.append(msg)

        return u"\n".join(context.response) + u"\n"


class PlainTextPlugin_InterfaceFactory(object):
    def __init__(self, params):
        self.params = params

    def create_interface(self, bot_name, params):
        return PlainTextPlugin_Interface(bot_name, utility.merge_params(self.params, params))


def inner_load_plugin(plugin_params):
    hub.register_interface_factory(type_name="plaintext",
                                   factory=PlainTextPlugin_InterfaceFactory(plugin_params))
