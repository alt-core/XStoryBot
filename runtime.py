import logging
import datetime
import json
import copy
import math
import threading
import time

import settings
import commands
import task_client
from cloud_backend.contracts import SaveResultUnknownError, StateRecoveryError
from context import ActionDeadlineExceeded, check_action_deadline
from scenario_selection import (
    ScenarioSelection, ScenarioStateError, ScenarioLoadError, read_scenario_generation,
    stamp_scenario_generation,
)


_director_class = None


def _get_director_class():
    global _director_class
    if _director_class is None:
        from scenario import Director
        _director_class = Director
    return _director_class


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
        try:
            self.scenario_check_interval = float(
                getattr(settings, 'OPTIONS', {}).get('scenario_check_interval', 10))
        except (TypeError, ValueError, OverflowError):
            raise ValueError('scenario_check_intervalは0以上の有限数で指定してください') from None
        if not math.isfinite(self.scenario_check_interval) or self.scenario_check_interval < 0:
            raise ValueError('scenario_check_intervalは0以上の有限数で指定してください')
        self._scenario_check_lock = threading.Lock()
        self._scenario_checked_at = None
        self._latest_selection = None
        self._scenario_check_error = None
        self._scenario_check_error_generation = 0
        self._scenario_state_store = None

    def get_interface(self, service_name):
        return self.interfaces.get(service_name, None)

    def build_scenario(self, task_id="local", options=None, version=1):
        import build_cache
        from models import GlobalBotVariablesDB
        from scenario import ScenarioBuilder, ScenarioSyntaxError
        try:
            tables, constants = self.scenario_loader.load_scenario()
            from richmenu_spec import menu_definitions, referenced_constants
            bot_settings = getattr(settings, 'BOTS', {}).get(self.name, {})
            definitions = menu_definitions(bot_settings)
            names = set(definitions)
            ids = {}
            if definitions:
                from richmenu_service import build_menu_ids
                ids = build_menu_ids(self, names)
                for name in referenced_constants(definitions) & constants.keys() & settings.CONSTANTS.keys():
                    if constants[name] != settings.CONSTANTS[name]:
                        logging.warning('メニューと定数Sheetで定数の値が異なります: %s', name)
            options = {**(options or {}), 'richmenu_names': names,
                       'richmenu_ids': ids, 'richmenu_require_ids': self.get_interface('line') is not None}
            scenario = ScenarioBuilder.build_from_tables(tables, constants, options=options, version=version)
            if definitions:
                scenario.richmenu_ids = ids
            scenario_uri = scenario.save_to_storage()
            generation = GlobalBotVariablesDB.save(self.name, scenario_uri)
            if type(generation) is not int or generation < 1:
                raise ValueError('シナリオの公開世代を取得できません')
            with self._scenario_check_lock:
                previous = self._latest_selection
                if previous is None or generation >= previous.generation:
                    if previous is not None and previous.uri == scenario_uri:
                        scenario = previous.scenario
                    self._latest_selection = ScenarioSelection(scenario, scenario_uri, generation)
                    self.scenario, self.scenario_uri = scenario, scenario_uri
                    self._scenario_checked_at = time.monotonic()
                    self._scenario_check_error = None
                    self._scenario_check_error_generation = 0
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

    def select_scenario(self, uri=None, min_generation=0, force=False):
        """固定URI、または保存世代より古くない最新版を共通cacheから選ぶ。"""
        if type(min_generation) is not int or min_generation < 0:
            raise ValueError('必要な公開世代は0以上の整数で指定してください')
        with self._scenario_check_lock:
            if uri is not None:
                return ScenarioSelection(self._load_scenario(uri), uri, None)
            now = time.monotonic()
            expired = (self._scenario_checked_at is None
                       or now - self._scenario_checked_at >= self.scenario_check_interval)
            if not force and not expired:
                if self._scenario_check_error is not None:
                    if min_generation <= self._scenario_check_error_generation:
                        raise copy.copy(self._scenario_check_error) from None
                elif (self._latest_selection is not None
                        and self._latest_selection.generation >= min_generation):
                    self.scenario, self.scenario_uri = (
                        self._latest_selection.scenario, self._latest_selection.uri)
                    return self._latest_selection
            previous = self._latest_selection
            checked_generation = max(min_generation, previous.generation if previous else 0)
            try:
                if self._scenario_state_store is None:
                    from cloud_backend import create_state_store
                    self._scenario_state_store = create_state_store()
                variables = self._scenario_state_store.get_global_bot_variables(self.name)
                if variables is None or not variables.get('scenario_uri'):
                    raise ValueError('シナリオがビルドされていません')
                uri = variables['scenario_uri']
                generation = variables.get('generation')
                if type(generation) is not int or generation < 1:
                    raise ValueError('公開世代付きのシナリオをビルドしてください')
                checked_generation = max(checked_generation, generation)
                if (generation < min_generation
                        or (previous is not None and generation < previous.generation)
                        or (previous is not None and generation == previous.generation
                            and uri != previous.uri)):
                    raise ValueError('保存状態とシナリオの公開世代が一致しません')
                scenario = self._load_scenario(uri)
                selected = ScenarioSelection(scenario, uri, generation)
            except Exception as error:
                self._scenario_checked_at = time.monotonic()
                self._scenario_check_error = error.with_traceback(None)
                self._scenario_check_error_generation = checked_generation
                raise copy.copy(self._scenario_check_error) from None
            self._latest_selection = selected
            self._scenario_checked_at = time.monotonic()
            self._scenario_check_error = None
            self._scenario_check_error_generation = 0
            return selected

    def _load_scenario(self, uri):
        if self._latest_selection is not None and self._latest_selection.uri == uri:
            self.scenario, self.scenario_uri = self._latest_selection.scenario, uri
        elif self.scenario is None or self.scenario_uri != uri:
            from scenario import Scenario
            logging.info('ビルド済シナリオをロードします: %s', uri)
            scenario = Scenario.load_from_uri(uri)
            self.scenario, self.scenario_uri = scenario, uri
        return self.scenario

    def check_reload(self, force=False):
        self.select_scenario(force=force)
        return True, None

    def _prepare_context(self, context, selection):
        scenario = selection.scenario
        context.scenario_selection = selection
        context.version = scenario.version

        override = getattr(context, 'constants_override', None)
        if override:
            context.add_env(override)
        if scenario.constants:
            context.add_env(scenario.constants)
        if settings.CONSTANTS:
            context.add_env(settings.CONSTANTS)
        context.richmenu_ids = getattr(scenario, 'richmenu_ids', {})
        context.add_env(commands.get_runtime_object_dictionary(context.service_name, context))
        if scenario.version >= 3:
            context.add_env({
                '$$action': context.action,
                '$$bot_name': context.bot_name,
                '$$service_name': context.service_name,
                '$$user_id': context.user.serialize(),
                '$$timestamp': now_str(),
            })

    def handle_action(self, context, interface=None, selection=None):
        context.state_namespace = self.state_namespace
        if interface is None:
            interface = self.get_interface(context.service_name)
        retry_count = int(interface.get_retry_count()) + 1
        raise_exceptions = bool(
            getattr(interface, 'should_raise_exceptions', lambda: False)())

        last_phase = None
        last_error = None
        context.defer_async_tasks = task_client.defer_until_response()
        base_envs = list(context.env_dicts)
        base_attrs = dict(context.attrs)
        reset_keyword = getattr(settings, 'OPTIONS', {}).get('reset_keyword')
        planning_started = False
        while retry_count > 0:
            phase = 'load'
            try:
                check_action_deadline(context)
                context.reactions = []
                context.pending_tasks = []
                context.load_status()
                phase = 'scenario'
                if selection is None or selection.generation is not None:
                    generations = context.status.scenario_generations
                    minimum = (0 if context.action == reset_keyword else
                               read_scenario_generation(generations, self.name))
                    if selection is None or selection.generation < minimum:
                        selection = self.select_scenario(min_generation=minimum)
                phase = 'plan'
                context.env_dicts = list(base_envs)
                context.attrs = dict(base_attrs)
                context.current_action = context.action
                self._prepare_context(context, selection)
                director = _get_director_class()(selection.scenario, context)
                planning_started = True
                director.plan_reactions()
                stamp_scenario_generation(context.status, self.name, selection.generation)
                check_action_deadline(context)
                phase = 'save'
                context.save_status()
            except ActionDeadlineExceeded as error:
                self._log_failure(context, 'deadline', error)
                return None
            except ScenarioStateError as error:
                self._log_failure(context, 'scenario-state', error)
                if raise_exceptions:
                    raise
                return None
            except StateRecoveryError as error:
                self._log_failure(context, 'save-result-unknown' if isinstance(
                    error, SaveResultUnknownError) else phase, error)
                raise
            except Exception as e:
                if phase == 'scenario' and not planning_started:
                    self._log_failure(context, 'scenario-load', e)
                    raise ScenarioLoadError('シナリオを取得できません') from e
                if raise_exceptions:
                    logging.error(
                        'Webchat handle_actionで例外が発生しました: '
                        'error_type=%s', type(e).__name__)
                    raise
                logging.error(f"handle_action で例外が発生しました: {e}")
                last_phase, last_error = phase, e
                retry_count -= 1
                continue

            try:
                check_action_deadline(context)
                result = interface.respond_reaction(context, context.reactions)
            except Exception as e:
                try:
                    context.rollback_status()
                except StateRecoveryError as rollback_error:
                    self._log_failure(context, 'rollback', rollback_error)
                    raise
                if raise_exceptions:
                    logging.error(
                        'Webchat response生成で例外が発生しました: '
                        'error_type=%s', type(e).__name__)
                    raise
                logging.error(f"interface.respond_reaction で例外が発生しました: {e}")
                last_phase, last_error = 'send', e
                retry_count -= 1
                continue

            for register_task, task_metadata in context.pending_tasks:
                try:
                    register_task()
                except Exception as error:
                    # 保存と応答が済んだ親を再実行せず、他の子の登録は続ける。
                    self._log_failure(context, 'enqueue', error, task=task_metadata)
            return result

        self._log_failure(context, last_phase, last_error)
        return None

    def _log_failure(self, context, phase, error, task=None):
        # 進行不能になった利用者を後から uid で辿れるよう、@log と同じ JSON 1行形式で残す
        try:
            status = context.status
            record = {
                'type': 'XSBFail',
                'bot': self.name,
                'service': context.service_name,
                'uid': str(context.user),
                'action': context.action,
                'scene': getattr(status, 'scene', None) if status is not None else None,
                'phase': phase,
                'error_type': type(error).__name__ if error is not None else None,
                'error': str(error) if error is not None else None,
            }
            saved_version = getattr(status, '_saved_version', None)
            if saved_version is not None:
                record['saved_version'] = str(saved_version)
            loaded_version = getattr(status, '_loaded_version', None)
            if loaded_version is not None:
                record['loaded_version'] = str(loaded_version)
            if error is not None and error.__cause__ is not None:
                record['cause_type'] = type(error.__cause__).__name__
            selection = getattr(context, 'scenario_selection', None)
            if selection is not None:
                record['scenario_generation'] = selection.generation
            if task is not None:
                record['task'] = dict(task, task_id=getattr(error, 'task_id', None))
            logging.error(json.dumps(record, ensure_ascii=False, default=str))
        except Exception:
            logging.exception('リトライ回数を超えました（XSBFail の記録にも失敗）')
