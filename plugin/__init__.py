# coding: utf-8


import importlib


def load_plugins(param_map):
    for plugin_name, params in param_map.items():
        plugin = importlib.import_module('plugin.' + plugin_name)
        if not hasattr(plugin, 'load_plugin'):
            raise RuntimeError('plugin.{} に load_plugin 関数が実装されていません'.format(plugin_name))
        if not isinstance(params, dict):
            raise RuntimeError('settings.py の {} のオプションが辞書型でありません'.format(plugin_name))
        plugin.load_plugin(params)
        #print('plugin.{} loaded.'.format(plugin_name))
