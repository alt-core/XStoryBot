# coding: utf-8

import commands


PUSHER_CMDS = (u'@pusher', u'@Pusher')


import pusher
import pusher.gae


class PusherPlugin_Runtime(object):
    def __init__(self, params):
        self.params = params
        self.pusher_client = None

    def get_pusher_client(self):
        if self.pusher_client is None:
            self.pusher_client = pusher.Pusher(
                app_id=self.params['app_id'],
                key=self.params['key'],
                secret=self.params['secret'],
                cluster=self.params['cluster'],
                backend=pusher.gae.GAEBackend
            )
        return self.pusher_client

    def run_command(self, _context, _msg, options):
        channel_id = options[0]
        event_id = options[1]
        message = options[2]
        pusher_client = self.get_pusher_client()
        pusher_client.trigger(channel_id, event_id, {"message": message})
        return True


def load_plugin(params):
    builder = commands.Default_Builder()
    runtime = PusherPlugin_Runtime(params)
    commands.register_command(commands.CommandEntry(
        command=PUSHER_CMDS,
        options='raw raw raw',
        builder=builder,
        runtime=runtime,
        service='*'))
