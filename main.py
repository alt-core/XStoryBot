# coding: utf-8

import re

import auth
import common_commands
import plugin
import settings
import hub
import commands
from runtime import BotRuntime
from scenario import ScenarioBuilder


bot_dict = {}


def get_bot(bot_name):
    return bot_dict.get(bot_name, None)


def initialize_bot_dict():
    bot_dict.clear()
    for name, bot_settings in settings.BOTS.items():
        if not re.match(r'^[-_a-zA-Z0-9]+$', name):
            raise RuntimeError(u'bot の name が不正です: {}'.format(name))
        interfaces = {}
        for interface_settings in bot_settings['interfaces']:
            interface = hub.create_interface(
                type_name=interface_settings['type'],
                bot_name=name,
                params=interface_settings.get('params', {}))
            if interface is None:
                raise RuntimeError('type: {} の interface が見つかりません'.format(interface_settings['type']))
            interfaces.update(interface.get_service_list())
        scenario_loader = hub.create_scenario_loader(
            type_name=bot_settings['scenario']['type'],
            params=bot_settings['scenario'].get('params', {}))
        if scenario_loader is None:
            raise RuntimeError('type: {} の scenario loader が見つかりません'.format(bot_settings['scenario']['type']))
        bot_dict[name] = BotRuntime(name, interfaces, scenario_loader)

    for name, bot in bot_dict.items():
        if bot.scenario is None:
            bot.scenario = ScenarioBuilder.build_from_table([
                [u'//', u'シナリオのロードができていません'],
            ])


def initialize():
    auth.setup(settings.OPTIONS)
    hub.clear()
    commands.clear()
    common_commands.setup(settings.OPTIONS)
    plugin.load_plugins(settings.PLUGINS)
    initialize_bot_dict()


initialize()
