# coding: utf-8

import os
import re

DEPLOY_ENV = os.getenv('XSBOT_DEPLOY_ENV', '')
if DEPLOY_ENV != 'test' and DEPLOY_ENV != 'local':
    # ログを Cloud Logging に送信するための初期化
    import google.cloud.logging
    client = google.cloud.logging.Client()
    client.setup_logging()

    import sys
    import logging
    import traceback

    # 例外も Logging ライブラリで送信する（これを設定しないと、エラー出力が1行1ログとして記録される）
    def exception_handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        tb_list = traceback.extract_tb(exc_traceback)
        if tb_list:
            last_entry = tb_list[-1]
            location_info = f"{last_entry.filename}:{last_entry.lineno} ({last_entry.name})"
        else:
            location_info = "情報なし"

        summary = f"例外が発生しました: {exc_type.__name__} at {location_info}"
        logging.error(summary, exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = exception_handler

import settings
import auth
import common_commands
import plugin
import hub
import commands
import task_client
from group_message_task_db import GroupMessageTaskDB
from runtime import BotRuntime
from scenario import ScenarioBuilder


bot_dict = {}


def get_bot(bot_name):
    return bot_dict.get(bot_name, None)

def get_bots():
    return bot_dict

def get_options():
    return settings.OPTIONS

def get_plugins():
    return plugin.get_plugins()

def initialize_bot_dict():
    bot_dict.clear()
    for name, bot_settings in settings.BOTS.items():
        if not re.match(r'^[-_a-zA-Z0-9]+$', name):
            raise RuntimeError(f'bot の name が不正です: {name}')
        interfaces = {}
        for interface_settings in bot_settings['interfaces']:
            interface = hub.create_interface(
                type_name=interface_settings['type'],
                bot_name=name,
                params=interface_settings.get('params', {}))
            if interface is None:
                raise RuntimeError(f'type: {interface_settings["type"]} の interface が見つかりません')
            interfaces.update(interface.get_service_list())
        scenario_loader = hub.create_scenario_loader(
            type_name=bot_settings['scenario']['type'],
            params=bot_settings['scenario'].get('params', {}))
        if scenario_loader is None:
            raise RuntimeError(f'type: {bot_settings["scenario"]["type"]} の scenario loader が見つかりません')
        bot_dict[name] = BotRuntime(
            name,
            interfaces,
            scenario_loader,
            state_namespace=bot_settings.get('state_namespace', name))

    for name, bot in bot_dict.items():
        if bot.scenario is None:
            bot.scenario = ScenarioBuilder.build_from_table([
                ['//', 'シナリオのロードができていません'],
            ])


def initialize():
    task_client.initialize(settings.BACKEND_SETTINGS)
    GroupMessageTaskDB.initialize(settings.BACKEND_SETTINGS, settings.OPTIONS)

    auth.setup(settings.AUTH_SETTINGS)
    hub.clear()
    commands.clear()
    common_commands.setup(settings.OPTIONS)
    plugin.load_plugins(settings.OPTIONS, settings.PLUGINS)
    initialize_bot_dict()


initialize()
