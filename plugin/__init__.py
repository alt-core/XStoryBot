# coding: utf-8

import importlib

plugins = {}

def load_plugins(common_params, param_map):
    for plugin_name, params in param_map.items():
        plugin = importlib.import_module('plugin.' + plugin_name)
        if not hasattr(plugin, 'load_plugin'):
            raise RuntimeError('plugin.{} に load_plugin 関数が実装されていません'.format(plugin_name))
        if not isinstance(params, dict):
            raise RuntimeError('settings.py の {} のオプションが辞書型でありません'.format(plugin_name))
        plugin_params = common_params.copy()
        plugin_params.update(params)
        plugin.load_plugin(plugin_params)
        plugins[plugin_name] = plugin
        #print('plugin.{} loaded.'.format(plugin_name))

def get_plugins():
    return plugins
