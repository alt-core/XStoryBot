# coding: utf-8

from plugin.line import default_commands
from plugin.line import interface


def load_plugin(params):
    default_commands.inner_load_plugin(params)
    interface.inner_load_plugin(params)
