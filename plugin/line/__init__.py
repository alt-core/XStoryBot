# coding: utf-8

def load_plugin(params):
    from plugin.line import default_commands
    from plugin.line import interface
    default_commands.inner_load_plugin(params)
    interface.inner_load_plugin(params)
