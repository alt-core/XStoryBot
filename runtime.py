# coding: utf-8
import logging
import datetime

from google.appengine.api import taskqueue, memcache

from models import GlobalBotVariables
from scenario import Scenario, Director, ScenarioBuilder, ScenarioSyntaxError


def now_str():
    now = datetime.datetime.now()
    jst = now + datetime.timedelta(hours=9)
    return jst.strftime('%Y/%m/%d %H:%M:%S')


class BotRuntime(object):
    def __init__(self, name, interfaces, scenario_loader):
        self.name = name
        self.interfaces = interfaces
        self.scenario_loader = scenario_loader

        self.scenario = None
        self.scenario_uri = None

    def get_interface(self, service_name):
        return self.interfaces.get(service_name, None)

    def build_scenario(self, options=None):
        try:
            self.scenario = ScenarioBuilder.build_from_tables(self.scenario_loader.load_scenario(), options=options)
            self.scenario_uri = self.scenario.save_to_storage()
            global_bot_variables = GlobalBotVariables.get_by_id(id=self.name)
            if global_bot_variables is None:
                global_bot_variables = GlobalBotVariables(id=self.name, scenario_uri=self.scenario_uri)
            else:
                global_bot_variables.scenario_uri = self.scenario_uri
            global_bot_variables.put()
            # image.py の画像のキャッシュを消す
            memcache.flush_all() # TODO: 画像が更新されたときにファイル名が変わるような方向性での対応
            memcache.set('last_build_result:' + self.name, u"{}\tSuccess".format(now_str()))
            return True, None
        except (ValueError, ScenarioSyntaxError) as e:
            err = unicode(e)
            logging.error(u"ビルドに失敗しました。\n" + err)
            memcache.set('last_build_result:' + self.name, u"{}\tFailure\t{}".format(now_str(), err))
            return False, err

    def load_scenario(self):
        return self.check_reload(force=True)

    def check_reload(self, force=False):
        global_bot_variables = GlobalBotVariables.get_by_id(id=self.name)
        if global_bot_variables is None:
            # 変換済みのシナリオが存在していない
            return False, u'シナリオがビルドされていません'
        scenario_uri = global_bot_variables.scenario_uri
        if force or self.scenario is None or self.scenario_uri != scenario_uri:
            # 初回ロードか、新しくビルドが実行された
            logging.info(u"ビルド済シナリオをロードします: \n" + scenario_uri)
            try:
                self.scenario = Scenario.load_from_uri(scenario_uri)
                self.scenario_uri = scenario_uri
                return True, None
            except (ValueError, ScenarioSyntaxError) as e:
                err = unicode(e)
                logging.error(u"ビルド済シナリオのロードに失敗しました。\n" + err)
                return False, err

    def handle_action(self, context):
        self.check_reload()
        context.reactions = []

        context.load_status()
        director = Director(self.scenario, context)
        director.plan_reactions()
        context.save_status()

        interface = self.get_interface(context.service_name)
        return interface.respond_reaction(context, context.reactions)
