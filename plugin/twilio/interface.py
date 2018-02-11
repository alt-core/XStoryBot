# coding: utf-8

import twilio.rest

import hub
import commands
import utility
import context
import users


class TwilioPlugin_ActionContext(context.ActionContext):
    def __init__(self, bot_name, interface, user, action, from_tel, to_tel, is_voicecall, message):
        context.ActionContext.__init__(self, bot_name, "twilio", interface, user, action)
        self.from_tel = from_tel
        self.to_tel = to_tel
        self.is_voicecall = is_voicecall
        self.message = message


class TwilioPlugin_Interface(object):
    def __init__(self, bot_name, params):
        self.bot_name = bot_name
        self.params = params
        self._twilio_client = None

    def get_twilio_client(self):
        if self._twilio_client is None:
            self._twilio_client = twilio.rest.Client(self.params['twilio_sid'], self.params['twilio_auth_token'])
        return self._twilio_client

    def get_service_list(self):
        return {'twilio': self}

    def create_context(self, user, action):
        return TwilioPlugin_ActionContext(self.bot_name, self, user, action,
                                          from_tel=user.user_id, to_tel=u"null", is_voicecall=False, message=u"")

    def create_context_from_twilio_event(self, from_tel, to_tel, is_voicecall, message):
        # ユーザID は from_tel
        # TODO: from_tel をそのまま user_id として使わない（個人情報保護の観点から）
        user = users.User("twilio", from_tel)
        if is_voicecall:
            # 音声着信の場合、action は #tel:電話番号 とする
            action = u'#tel:'+to_tel
            if message is not None:
                # message がある場合（音声認識した、または @dial の内容取得時）は
                # message で上書き
                action = message
        else:
            # テキストメッセージの場合、action は 本文 とする
            action = message
        return TwilioPlugin_ActionContext(self.bot_name, self, user, action,
                                          from_tel=from_tel, to_tel=to_tel, is_voicecall=is_voicecall, message=message)

    def respond_reaction(self, context, reactions):
        twiml = u'<?xml version="1.0" encoding="UTF-8"?>' \
                u'<Response>'

        context.response = []
        for reaction, children in reactions:
            msg = reaction[0]
            options = reaction[1:] if len(reaction) > 1 else []

            if commands.invoke_runtime_construct_response(context, msg, options, children):
                # コマンド毎の処理メソッドの中で context.response への追加が行われている
                pass
            elif msg.startswith(u'<'):
                context.response.append(msg)
            else:
                if context.is_voicecall:
                    context.response.append(u'<Say language="ja-jp" voice="woman">' + msg + u'</Say>')
                else:
                    context.response.append(u'<Message>' + msg + u'</Message>')

        twiml += u''.join(context.response)
        twiml += u'</Response>'

        return twiml


class TwilioPlugin_InterfaceFactory(object):
    def __init__(self, params):
        self.params = params

    def create_interface(self, bot_name, params):
        return TwilioPlugin_Interface(bot_name, utility.merge_params(self.params, params))


def inner_load_plugin(plugin_params):
    hub.register_interface_factory(type_name="twilio",
                                   factory=TwilioPlugin_InterfaceFactory(plugin_params))
