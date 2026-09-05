# coding: utf-8

import importlib

plugins = {}

def load_plugins(common_params, param_map):
    for plugin_name, params in param_map.items():
        try:
            plugin = importlib.import_module('plugin.' + plugin_name)
        except ModuleNotFoundError as error:
            if error.name and error.name.startswith('plugin.'):
                raise
            # plugin 自体はあるが、その依存 package が入っていない（twilio／pusher など）
            raise RuntimeError(
                f'plugin.{plugin_name} の依存 package "{error.name}" が見つかりません。'
                f'requirements-optional.txt を install するか、settings.yaml の plugins から外してください'
            ) from error
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
