# coding: utf-8

import logging
import urllib

import settings
import hub
import commands

SMS_CMDS = (u'@sms', u'@SMS')
DIAL_CMDS = (u'@dial', u'@電話')


class TwilioDefaultCommandsPlugin_Runtime(object):
    def __init__(self):
        pass

    def run_command(self, context, msg, options):
        interface = context.get_interface('twilio')

        if msg in SMS_CMDS:
            message = options[0]
            twilio_client = interface.get_twilio_client()
            twilio_client.messages.create(
                to=context.from_tel,
                from_=interface.params['sms_from'],
                body=message
            )

        elif msg == u'@dial' or msg == u'@電話':
            action_dial_content = options[0]
            url_dial_content = 'https://' + settings.SERVER_NAME + '/twilio/dial_content/' + interface.bot_name + \
                               '/' + urllib.quote(action_dial_content.encode('utf-8'), '')
            logging.info('TwiML url: ' + url_dial_content)
            if len(options) == 1:
                twilio_client = interface.get_twilio_client()
                twilio_client.calls.create(
                    to=context.from_tel,
                    from_=interface.params['dial_from'],
                    url=url_dial_content,
                    timeout=5
                )
            else:
                # 通話の完了通知が必要
                action_completed = options[1]
                url_completed = 'https://' + settings.SERVER_NAME + '/twilio/dial_completed_callback/' + interface.bot_name + \
                                '/' + urllib.quote(action_completed.encode('utf-8'), '')
                twilio_client = interface.get_twilio_client()
                twilio_client.calls.create(
                    to=context.from_tel,
                    from_=interface.params['dial_from'],
                    url=url_dial_content,
                    timeout=5,
                    status_callback=url_completed,
                    status_callback_event=['completed']
                )


def inner_load_plugin(params):
    builder = commands.Default_Builder()
    runtime = TwilioDefaultCommandsPlugin_Runtime()
    hub.register_handler(
        service='twilio',
        builder=builder,
        runtime=runtime)
    commands.register_commands([
        commands.CommandEntry(
            command=SMS_CMDS,
            options='text',
            builder=builder,
            runtime=runtime,
            service='twilio'),
        commands.CommandEntry(
            command=DIAL_CMDS,
            options='hankaku [label]',
            builder=builder,
            runtime=runtime,
            service='twilio'),
    ])
