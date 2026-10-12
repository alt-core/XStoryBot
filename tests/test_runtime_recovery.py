"""保存・送信・巻き戻しを通した進行と応答の対応を確認する。"""

import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from cloud_backend.contracts import (
    RollbackConflictError, RollbackError, SaveResultUnknownError,
    StateConflictError, StateStoreError,
)
from cloud_backend.local.state_store import LocalStateStore
from cloud_backend.aws.state_store import AwsStateStore
from context import ActionContext
from scenario_selection import ScenarioSelection
from tests.test_api_endpoints import TestApp
from tests.test_aws_state_store import AWS_SETTINGS, _MemoryDynamoClient, _MemoryObjectStore
from users import User


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeRecoveryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = LocalStateStore(directory.name)
        with patch('cloud_backend.create_state_store', return_value=self.store):
            self.models = load_module('_recovery_models', 'models.py')
        modules = patch.dict(sys.modules, {
            'settings': SimpleNamespace(CONSTANTS={}, OPTIONS={}),
            'commands': SimpleNamespace(get_runtime_object_dictionary=lambda *args: {}),
            'task_client': SimpleNamespace(defer_until_response=lambda: False),
        })
        modules.start()
        self.addCleanup(modules.stop)
        self.runtime = load_module('_recovery_runtime', 'runtime.py')
        players = patch('context._player_status_class', self.models.PlayerStatusDB)
        players.start()
        self.addCleanup(players.stop)
        self.now = 0
        clock = patch('context.time.monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.interface = Mock()
        self.interface.get_retry_count.return_value = 3
        self.interface.should_raise_exceptions.return_value = False
        self.interface.respond_reaction.return_value = 'OK'
        self.bot = self.runtime.BotRuntime('bot', {'line': self.interface}, None)
        self.selected = SimpleNamespace(version=3, constants={'$edition': 'selected'},
                                        richmenu_ids={'menu': 'selected-menu'})
        self.bot.scenario = self.selected
        self.bot.scenario_uri = 'first'
        self.bot._scenario_state_store = self.store
        self.bot._latest_selection = ScenarioSelection(self.selected, 'first', 1)
        self.bot._scenario_checked_at = 0
        self.store.save_global_bot_variables('bot', 'first')
        self.context = ActionContext('bot', 'line', self.interface,
                                     User('line', 'user,U1'), 'go', {})
        self.planned = []
        test = self

        class Director:
            def __init__(self, scenario, context):
                self.scenario, self.context = scenario, context

            def plan_reactions(self):
                test.planned.append(self.scenario)
                self.context.status.scene = self.context.env['$edition']
                self.context.status['$step'] = self.context.status.get('$step', 0) + 1

        self.runtime._director_class = Director

    def player(self):
        return self.models.PlayerStatusDB('bot', 'line:user,U1')

    def _action_http_client(self, members=()):
        modules = patch.dict(sys.modules, {
            'main': SimpleNamespace(get_bot=Mock(return_value=self.bot)),
            'auth': SimpleNamespace(check_token=lambda token: token == 'test-token'),
            'users': SimpleNamespace(User=User, get_group_members=Mock(return_value=members)),
        })
        modules.start()
        self.addCleanup(modules.stop)
        self.interface.create_context.side_effect = lambda user, action, attrs: ActionContext(
            'bot', 'line', self.interface, user, action, attrs)
        return TestApp(load_module('_recovery_action_http', 'webapi.py').app)

    def _cache_scenario_read_failure(self):
        self.now = 10
        with patch.object(self.store, 'get_global_bot_variables',
                          side_effect=ConnectionError('一時的な読取障害')):
            with self.assertRaises(ConnectionError):
                self.bot.check_reload()
        self.now = 11

    def test_HTTP_action入口も新版保存を読んで負cacheを期限前に再確認する(self):
        self._cache_scenario_read_failure()
        self.store.save_global_bot_variables('bot', 'next')
        self.store.create_player_status('bot:line:user,U1', {
            'scene': 'next', 'value': '{"$step":7}', 'action_token': 'another',
            'scenario_generations': {'bot': 2}})
        next_scenario = SimpleNamespace(version=3, constants={'$edition': 'next'})
        scenario_module = SimpleNamespace(Scenario=SimpleNamespace(
            load_from_uri=Mock(return_value=next_scenario)))
        client = self._action_http_client()
        with patch.dict(sys.modules, {'scenario': scenario_module}), \
                patch.object(self.store, 'get_global_bot_variables',
                             wraps=self.store.get_global_bot_variables) as read:
            result = client.post('/api/v1/bots/bot/action', params={
                'user': 'line:user,U1', 'action': 'go', 'token': 'test-token'})
        self.assertEqual(200, result.status_int)
        read.assert_called_once_with('bot')
        self.assertEqual([next_scenario], self.planned)
        self.assertEqual(8, self.player()['$step'])
        self.assertEqual({'bot': 2}, self.player().scenario_generations)
        self.interface.respond_reaction.assert_called_once()

    def test_HTTP_action旧保存の取得障害は500を維持し進行もDB再確認も行わない(self):
        self.store.create_player_status('bot:line:user,U1', {
            'scene': 'selected', 'value': '{"$step":7}', 'action_token': 'old',
            'scenario_generations': {'bot': 1}})
        self._cache_scenario_read_failure()
        client = self._action_http_client()
        with patch.object(self.store, 'get_global_bot_variables',
                          wraps=self.store.get_global_bot_variables) as read, \
                self.assertLogs(level='ERROR'):
            result = client.post('/api/v1/bots/bot/action', params={
                'user': 'line:user,U1', 'action': 'go', 'token': 'test-token'},
                expect_errors=True)
        self.assertEqual(500, result.status_int)
        read.assert_not_called()
        self.assertEqual([], self.planned)
        self.assertEqual(7, self.player()['$step'])
        self.assertEqual({'bot': 1}, self.player().scenario_generations)
        self.interface.respond_reaction.assert_not_called()

    def test_HTTP_group全員が取得前に止まる場合は500で再試行へ戻す(self):
        members = [User('line', 'user,U1'), User('line', 'user,U2')]
        for member in members:
            self.store.create_player_status('bot:' + member.serialize(), {
                'scene': 'selected', 'value': '{"$step":7}', 'action_token': 'old',
                'scenario_generations': {'bot': 1}})
        self._cache_scenario_read_failure()
        client = self._action_http_client(members)
        with patch.object(self.store, 'get_global_bot_variables',
                          wraps=self.store.get_global_bot_variables) as read, \
                self.assertLogs(level='ERROR'):
            result = client.post('/api/v1/bots/bot/action', params={
                'user': 'group:group-1', 'action': 'go', 'token': 'test-token'},
                expect_errors=True)
        self.assertEqual(500, result.status_int)
        read.assert_not_called()
        self.assertEqual([], self.planned)
        for member in members:
            self.assertEqual(7, self.models.PlayerStatusDB('bot', member.serialize())['$step'])
        self.interface.respond_reaction.assert_not_called()

    def test_HTTP_group部分成功後の新版取得障害で成功者を全体再送へ戻さない(self):
        members = [User('line', 'user,U1'), User('line', 'user,U2')]
        for index, member in enumerate(members, 1):
            self.store.create_player_status('bot:' + member.serialize(), {
                'scene': 'selected', 'value': '{"$step":7}', 'action_token': 'old',
                'scenario_generations': {'bot': index}})
        self.store.save_global_bot_variables('bot', 'next')
        client = self._action_http_client(members)
        with patch.object(self.store, 'get_global_bot_variables',
                          side_effect=ConnectionError('新版取得の障害')) as read, \
                self.assertLogs(level='ERROR'):
            result = client.post('/api/v1/bots/bot/action', params={
                'user': 'group:group-1', 'action': 'go', 'token': 'test-token'})
        self.assertEqual(200, result.status_int)
        read.assert_called_once_with('bot')
        self.assertEqual([self.selected], self.planned)
        self.assertEqual(8, self.models.PlayerStatusDB('bot', members[0].serialize())['$step'])
        self.assertEqual(7, self.models.PlayerStatusDB('bot', members[1].serialize())['$step'])
        self.interface.respond_reaction.assert_called_once()

    def test_HTTP_groupCAS競合前に計画済みなら再取得失敗を全員未実行とみなさない(self):
        create = self.store.create_player_status

        def new_state_then_conflict(*args):
            create(args[0], {'scene': 'next', 'value': '{"$step":7}',
                            'action_token': 'another', 'scenario_generations': {'bot': 2}})
            self.store.save_global_bot_variables('bot', 'next')
            raise StateConflictError('別の処理が新版状態を保存しました')
        client = self._action_http_client([User('line', 'user,U1')])
        with patch.object(self.store, 'create_player_status', side_effect=new_state_then_conflict), \
                patch.object(self.store, 'get_global_bot_variables',
                             side_effect=ConnectionError('新版取得の障害')) as read, \
                self.assertLogs(level='ERROR'):
            result = client.post('/api/v1/bots/bot/action', params={
                'user': 'group:group-1', 'action': 'go', 'token': 'test-token'})
        self.assertEqual(200, result.status_int)
        read.assert_called_once_with('bot')
        self.assertEqual([self.selected], self.planned)
        self.assertEqual('next', self.player().scene)
        self.assertEqual(7, self.player()['$step'])
        self.assertEqual({'bot': 2}, self.player().scenario_generations)
        self.interface.respond_reaction.assert_not_called()

    def test_一時送信障害は巻戻して回復し途中の版変更を取り込まない(self):
        def respond(context, reactions):
            if self.interface.respond_reaction.call_count == 1:
                self.bot.scenario = SimpleNamespace(version=1, constants={'$edition': 'new'})
                raise ConnectionError('一時障害')
            return 'OK'
        self.interface.respond_reaction.side_effect = respond
        with self.assertLogs(level='ERROR'):
            self.assertEqual('OK', self.bot.handle_action(self.context))
        self.assertEqual([self.selected, self.selected], self.planned)
        self.assertEqual('selected', self.player().scene)
        self.assertEqual(1, self.player()['$step'])
        self.assertEqual(3, self.context.version)
        self.assertEqual({'menu': 'selected-menu'}, self.context.richmenu_ids)

    def _aws_store_with_saved_state(self):
        client = _MemoryDynamoClient()
        self.store = AwsStateStore(AWS_SETTINGS, client=client, object_store=_MemoryObjectStore())
        self.models._state_store = self.store
        self.bot._scenario_state_store = self.store
        self.store.save_global_bot_variables('bot', 'first')
        player = self.player()
        player.scene = 'before'
        player['$step'] = 7
        player.scenario_generations = {'bot': 1}
        player.save()
        client.calls.clear()
        test = self

        class Director:
            def __init__(self, scenario, context):
                self.scenario, self.context = scenario, context

            def plan_reactions(self):
                test.planned.append(self.scenario)
                self.context.status.scene = 'selected'
                self.context.status['$step'] = self.context.status['$step'] + 1
                self.context.reactions = [([None, f'plan-{len(test.planned)}'], None)]

        self.runtime._director_class = Director
        return client

    def _commit_then_sdk_retry_rejection(self, client, writes, lost_at):
        put = client.put_item

        def write(**request):
            item = self.store._decode_item(request['Item'])
            writes.append(item['version'])
            result = put(**request)
            if len(writes) == lost_at:
                # ACKを失ったSDKが同じ候補UUIDで再送し、最終応答だけが条件拒否になる。
                try:
                    put(**request)
                except ClientError as error:
                    error.response['ResponseMetadata'] = {'RetryAttempts': 1}
                    raise
                raise AssertionError('同じCAS要求の再送は条件拒否になるはずです')
            return result

        return write

    def test_AWS通常保存のACK喪失は自書込み確認後に同じ計画の応答へ進む(self):
        client = self._aws_store_with_saved_state()
        writes, delivered = [], []
        self.interface.respond_reaction.side_effect = (
            lambda context, reactions: delivered.append(reactions[0][0][1]) or 'OK')
        with patch.object(client, 'put_item', side_effect=self._commit_then_sdk_retry_rejection(
                client, writes, lost_at=1)):
            self.assertEqual('OK', self.bot.handle_action(self.context))
        self.assertEqual(1, len(self.planned))
        self.assertEqual(['plan-1'], delivered)
        self.assertEqual(1, len(writes))
        self.assertEqual(8, self.player()['$step'])
        self.assertEqual('selected', self.player().scene)
        self.assertEqual(self.store._version_token(writes[0]), self.context.status._saved_version)

    def test_AWS巻戻しのACK喪失も確認できれば旧応答を送らず新計画から回復する(self):
        client = self._aws_store_with_saved_state()
        writes, attempted, delivered = [], [], []

        def respond(context, reactions):
            message = reactions[0][0][1]
            attempted.append(message)
            if len(attempted) == 1:
                raise ConnectionError('送信前の一時障害')
            delivered.append(message)
            return 'OK'

        self.interface.respond_reaction.side_effect = respond
        with patch.object(client, 'put_item', side_effect=self._commit_then_sdk_retry_rejection(
                client, writes, lost_at=2)), self.assertLogs(level='ERROR'):
            self.assertEqual('OK', self.bot.handle_action(self.context))
        self.assertEqual(2, len(self.planned))
        self.assertEqual(['plan-1', 'plan-2'], attempted)
        self.assertEqual(['plan-2'], delivered)
        self.assertEqual(3, len(writes))
        self.assertEqual(3, len(set(writes)))
        self.assertEqual(8, self.player()['$step'])
        self.assertEqual('selected', self.player().scene)
        self.assertEqual(self.store._version_token(writes[2]), self.context.status._saved_version)

    def _assert_unknown_save_stops(self, existing):
        expected_step = 1
        loaded_version = None
        if existing:
            loaded_version = self.store.create_player_status('bot:line:user,U1', {
                'scene': 'selected', 'value': '{"$step":7}', 'action_token': 'old',
                'scenario_generations': {'bot': 1}})
            expected_step = 8
        method = 'update_player_status' if existing else 'create_player_status'
        write = getattr(self.store, method)

        def commit_then_fail(*args):
            write(*args)
            raise StateStoreError('保存の返答を受信できません')
        with patch.object(self.store, method, side_effect=commit_then_fail) as saving, \
                patch.object(self.store, 'delete_player_status') as deleting, \
                self.assertLogs(level='ERROR') as logs, self.assertRaises(SaveResultUnknownError):
            self.bot.handle_action(self.context)
        self.assertEqual(1, len(self.planned))
        saving.assert_called_once()
        deleting.assert_not_called()
        self.assertEqual(expected_step, self.player()['$step'])
        self.assertEqual({'bot': 1}, self.player().scenario_generations)
        self.assertIsNone(self.context.status._saved_version)
        self.interface.respond_reaction.assert_not_called()
        recorded = '\n'.join(logs.output)
        self.assertIn('"phase": "save-result-unknown"', recorded)
        self.assertIn('"error_type": "SaveResultUnknownError"', recorded)
        if loaded_version is not None:
            self.assertIn(str(loaded_version), recorded)

    def test_新規保存がcommit後に例外を返しても一度で停止して送信しない(self):
        self._assert_unknown_save_stops(existing=False)

    def test_更新保存がcommit後に例外を返しても再計画や復元をせず停止する(self):
        self._assert_unknown_save_stops(existing=True)

    def test_保存前の書込み障害も未保存と推測せず一度で停止する(self):
        with patch.object(self.store, 'create_player_status',
                          side_effect=StateStoreError('書込み結果を確認できません')) as write, \
                self.assertLogs(level='ERROR'), self.assertRaises(SaveResultUnknownError):
            self.bot.handle_action(self.context)
        write.assert_called_once()
        self.assertEqual(1, len(self.planned))
        self.assertIsNone(self.store.load_player_status('bot:line:user,U1'))
        self.interface.respond_reaction.assert_not_called()

    def test_保存API前のJSON変換エラーは既存の台本失敗の扱いを維持する(self):
        class UnserializableDirector:
            def __init__(self, scenario, context):
                self.context = context

            def plan_reactions(self):
                self.context.status['$bad'] = object()
        self.runtime._director_class = UnserializableDirector
        with patch.object(self.store, 'create_player_status') as write, self.assertLogs(level='ERROR'):
            self.assertIsNone(self.bot.handle_action(self.context))
        write.assert_not_called()
        self.interface.respond_reaction.assert_not_called()

    def test_保存前の競合は再試行し送信は一度だけ行う(self):
        create = self.store.create_player_status
        calls = []

        def create_once_conflicting(*args):
            calls.append(args)
            if len(calls) == 1:
                raise StateConflictError('競合')
            return create(*args)
        with patch.object(self.store, 'create_player_status', side_effect=create_once_conflicting), \
                self.assertLogs(level='ERROR'):
            self.assertEqual('OK', self.bot.handle_action(self.context))
        self.assertEqual(2, len(self.planned))
        self.interface.respond_reaction.assert_called_once()
        self.assertEqual(1, self.player()['$step'])

    def test_CAS再読込が新版なら定数とDirectorと保存世代を一緒に更新する(self):
        next_scenario = SimpleNamespace(version=3, constants={'$edition': 'next'},
                                        richmenu_ids={'menu': 'next-menu'})
        create = self.store.create_player_status

        def new_state_then_conflict(*args):
            create(args[0], {'scene': 'next', 'value': '{"$step":7}',
                            'action_token': 'another', 'scenario_generations': {'bot': 2}})
            self.store.save_global_bot_variables('bot', 'next')
            raise StateConflictError('新版の状態が保存されました')
        scenario_module = SimpleNamespace(Scenario=SimpleNamespace(
            load_from_uri=Mock(return_value=next_scenario)))
        with patch.object(self.store, 'create_player_status', side_effect=new_state_then_conflict), \
                patch.dict(sys.modules, {'scenario': scenario_module}), self.assertLogs(level='ERROR'):
            self.assertEqual('OK', self.bot.handle_action(self.context))
        self.assertEqual([self.selected, next_scenario], self.planned)
        self.assertEqual('next', self.player().scene)
        self.assertEqual(8, self.player()['$step'])
        self.assertEqual({'bot': 2}, self.player().scenario_generations)
        self.assertEqual({'menu': 'next-menu'}, self.context.richmenu_ids)
        self.interface.respond_reaction.assert_called_once()

    def test_世代欠損の既存保存は進めず明示resetだけを実行できる(self):
        self.store.create_player_status('bot:line:user,U1', {
            'scene': 'old', 'value': '{"$step":7}', 'action_token': 'old'})
        with self.assertLogs(level='ERROR'):
            self.assertIsNone(self.bot.handle_action(self.context))
        self.assertEqual([], self.planned)
        self.assertEqual(7, self.player()['$step'])
        self.runtime.settings.OPTIONS = {'reset_keyword': '!reset'}
        self.context.action = '!reset'

        class ResetDirector:
            def __init__(self, scenario, context):
                self.context = context

            def plan_reactions(self):
                self.context.status.reset()
        self.runtime._director_class = ResetDirector
        self.assertEqual('OK', self.bot.handle_action(self.context))
        self.assertNotIn('$step', self.player())
        self.assertEqual({'bot': 1}, self.player().scenario_generations)

    def test_後続保存を巻戻さず元入力を再計画しない(self):
        def fail_after_other_save(*args):
            other = self.player()
            other.scene = 'later'
            other['$step'] = 7
            other.save()
            raise ConnectionError('送信失敗')
        self.interface.respond_reaction.side_effect = fail_after_other_save
        with self.assertLogs(level='ERROR') as logs, self.assertRaises(RollbackConflictError):
            self.bot.handle_action(self.context)
        self.assertEqual(1, len(self.planned))
        self.assertEqual('later', self.player().scene)
        self.assertEqual(7, self.player()['$step'])
        self.assertIn('"phase": "rollback"', '\n'.join(logs.output))
        self.assertIn('"saved_version"', '\n'.join(logs.output))

    def test_巻戻し先の障害も再計画へ戻さず救済対象にする(self):
        self.interface.respond_reaction.side_effect = ConnectionError('送信失敗')
        for error in (StateStoreError('障害'), RuntimeError('保存先の想定外の障害')):
            with self.subTest(error=type(error).__name__):
                self.store.delete_player_status('bot:line:user,U1')
                self.planned.clear()
                with patch.object(self.store, 'delete_player_status', side_effect=error), \
                        self.assertLogs(level='ERROR'), self.assertRaises(RollbackError):
                    self.bot.handle_action(self.context)
                self.assertEqual(1, len(self.planned))
                self.assertEqual(1, self.player()['$step'])

    def test_期限切れなら新しい計画や保存を始めない(self):
        self.context.action_deadline = 0
        with self.assertLogs(level='ERROR'):
            self.assertIsNone(self.bot.handle_action(self.context))
        self.assertEqual([], self.planned)
        self.assertIsNone(self.store.load_player_status('bot:line:user,U1'))
        self.interface.respond_reaction.assert_not_called()

    def test_保存直後に期限を迎えた場合は未送信状態を戻す(self):
        self.context.action_deadline = 10
        create = self.store.create_player_status

        def save_at_deadline(*args):
            version = create(*args)
            self.now = 10
            return version
        with patch.object(self.store, 'create_player_status', side_effect=save_at_deadline), \
                self.assertLogs(level='ERROR'):
            self.assertIsNone(self.bot.handle_action(self.context))
        self.assertIsNone(self.store.load_player_status('bot:line:user,U1'))
        self.interface.respond_reaction.assert_not_called()

    def test_返信成功後の期限超過だけで進行を巻戻さない(self):
        self.context.action_deadline = 10

        def success(*args):
            self.now = 11
            return 'OK'
        self.interface.respond_reaction.side_effect = success
        self.assertEqual('OK', self.bot.handle_action(self.context))
        self.assertEqual(1, self.player()['$step'])


if __name__ == '__main__':
    unittest.main()
