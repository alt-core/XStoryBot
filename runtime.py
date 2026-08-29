import logging
import datetime
import json

import build_cache

import settings
from models import GlobalBotVariablesDB
from scenario import Scenario, Director, ScenarioBuilder, ScenarioSyntaxError
import commands


def now_str():
    now = datetime.datetime.now()
    jst = now + datetime.timedelta(hours=9)
    return jst.strftime('%Y/%m/%d %H:%M:%S')


class BotRuntime:
    def __init__(self, name, interfaces, scenario_loader, state_namespace=None):
        self.name = name
        self.state_namespace = state_namespace or name
        self.interfaces = interfaces
        self.scenario_loader = scenario_loader

        self.scenario = None
        self.scenario_uri = None

    def get_interface(self, service_name):
        return self.interfaces.get(service_name, None)

    def build_scenario(self, task_id="local", options=None, version=1):
        try:
            tables, constants = self.scenario_loader.load_scenario()
            self.scenario = ScenarioBuilder.build_from_tables(tables, constants, options=options, version=version)
            self.scenario_uri = self.scenario.save_to_storage()
            GlobalBotVariablesDB.save(self.name, self.scenario_uri)
            # image.py の画像のキャッシュを消す
            build_cache.clear() # TODO: 画像が更新されたときにファイル名が変わるような方向性での対応
            build_cache.set_cache(f'last_build_result:{self.name}', json.dumps({
                "timestamp": now_str(),
                "task_id": task_id,
                "status": "Success"
            }), sec=60*60*24)
            return True, None
        except (ValueError, ScenarioSyntaxError) as e:
            err = str(e)
            logging.error(f"ビルドに失敗しました。\n{err}")
            build_cache.set_cache(f'last_build_result:{self.name}', json.dumps({
                "timestamp": now_str(),
                "task_id": task_id,
                "status": "Failure",
                "error": err
            }), sec=60*60*24)
            return False, err
        except Exception as e:
            err = str(e)
            logging.exception(f"ビルドに失敗しました。\n{err}")
            build_cache.set_cache(f'last_build_result:{self.name}', json.dumps({
                "timestamp": now_str(),
                "task_id": task_id,
                "status": "Failure",
                "error": err
            }), sec=60*60*24)
            return False, err

    def load_scenario(self):
        return self.check_reload(force=True)

    def check_reload(self, force=False):
        global_bot_variables = GlobalBotVariablesDB.get_by_bot_name(self.name)
        if global_bot_variables is None:
            # 変換済みのシナリオが存在していない
            return False, 'シナリオがビルドされていません'
        scenario_uri = global_bot_variables['scenario_uri']
        if force or self.scenario is None or self.scenario_uri != scenario_uri:
            # 初回ロードか、新しくビルドが実行された
            logging.info(f"ビルド済シナリオをロードします: \n{scenario_uri}")
            try:
                self.scenario = Scenario.load_from_uri(scenario_uri)
                self.scenario_uri = scenario_uri
                return True, None
            except (ValueError, ScenarioSyntaxError) as e:
                err = str(e)
                logging.error(f"ビルド済シナリオのロードに失敗しました。\n{err}")
                return False, err
            except Exception as e:
                err = str(e)
                logging.exception(f"ビルド済シナリオのロードに失敗しました。\n{err}")
                return False, err

    def handle_action(self, context):
        context.version = self.scenario.version
        context.state_namespace = self.state_namespace

        if settings.CONSTANTS:
            context.add_env(settings.CONSTANTS)
        if self.scenario.constants:
            context.add_env(self.scenario.constants)
        context.add_env(commands.get_runtime_object_dictionary(context.service_name, context))
        if self.scenario.version >= 3:
            context.add_env({
                '$$action': context.action,
                '$$bot_name': context.bot_name,
                '$$service_name': context.service_name,
                '$$user_id': context.user.serialize(),
                '$$timestamp': now_str(),
            })

        interface = self.get_interface(context.service_name)
        retry_count = int(interface.get_retry_count()) + 1

        while retry_count > 0:
            try:
                context.reactions = []
                context.load_status()
                director = Director(self.scenario, context)
                director.plan_reactions()
                context.save_status()
            except Exception as e:
                logging.error(f"handle_action で例外が発生しました: {e}")
                retry_count -= 1
                continue

            try:
                result = interface.respond_reaction(context, context.reactions)
                return result
            except Exception as e:
                logging.error(f"interface.respond_reaction で例外が発生しました: {e}")
                context.rollback_status()
                retry_count -= 1
                continue

        logging.error(f"リトライ回数を超えました")
        return None
