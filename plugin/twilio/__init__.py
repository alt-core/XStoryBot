# coding: utf-8

from plugin.twilio import default_commands
from plugin.twilio import interface


def load_plugin(params):
    default_commands.inner_load_plugin(params)
    interface.inner_load_plugin(params)

