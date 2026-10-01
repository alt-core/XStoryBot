"""親の保存・返信後の登録と、登録失敗時の親の扱いを確認する。"""

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import common_commands
import task_client


class DeferredTaskTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.queue = SimpleNamespace(
            defer_until_response=True,
            create_task=Mock(side_effect=lambda *_args, **_params: self.events.append('enqueue')),
        )
        patcher = patch.object(task_client, '_task_queue', self.queue)
        patcher.start()
        self.addCleanup(patcher.stop)
        settings = ModuleType('settings')
        settings.CONSTANTS = {}
        spec = importlib.util.spec_from_file_location(
            'tests._deferred_runtime', Path(__file__).resolve().parents[1] / 'runtime.py')
        self.runtime = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'settings': settings}):
            spec.loader.exec_module(self.runtime)
        self.context = SimpleNamespace(
            service_name='line', bot_name='bot', action='start',
            user=SimpleNamespace(service_name='line', serialize=lambda: 'line:user,U1'),
            status=SimpleNamespace(scene='scene'), env={},
            add_env=Mock(),
            load_status=Mock(side_effect=lambda: self.events.append('load')),
            save_status=Mock(side_effect=lambda: self.events.append('save')),
            rollback_status=Mock(side_effect=lambda: self.events.append('rollback')),
        )
        self.interface = SimpleNamespace(
            get_retry_count=lambda: 1,
            respond_reaction=Mock(side_effect=lambda *_args: self.events.append('reply') or 'OK'),
        )
        self.bot = self.runtime.BotRuntime('bot', {'line': self.interface}, None)
        self.bot.scenario = SimpleNamespace(version=3, constants={})
        self.common = common_commands.CommonCommands_Runtime({'reset_keyword': '!reset'})
        self.main = ModuleType('main')
        self.main.get_bot = Mock(return_value=SimpleNamespace(get_interface=lambda _name: self.interface))
        self.plan = Mock(side_effect=self._plan)
        self.runtime._director_class = lambda *_args: SimpleNamespace(plan_reactions=self.plan)

    def _plan(self):
        self.events.append('plan')
        self.common.run_command(self.context, None, '@forward', ['target', '#next'])

    def _run(self):
        with patch.dict(sys.modules, {'main': self.main}):
            return self.bot.handle_action(self.context)

    def test_AWSでは保存と返信の後にforwardを登録する(self):
        self.assertEqual('OK', self._run())
        self.assertEqual(['load', 'plan', 'save', 'reply', 'enqueue'], self.events)
        self.queue.create_task.assert_called_once()

    def test_従来のtransportではplan中に登録する(self):
        self.queue.defer_until_response = False
        self.assertEqual('OK', self._run())
        self.assertEqual(['load', 'plan', 'enqueue', 'save', 'reply'], self.events)

    def test_plan再試行の未登録タスクは捨てる(self):
        def plan():
            self._plan()
            if self.plan.call_count == 1:
                raise RuntimeError('plan failed')
        self.plan.side_effect = plan
        with self.assertLogs(level='ERROR'):
            self.assertEqual('OK', self._run())
        self.queue.create_task.assert_called_once()
        self.assertEqual('enqueue', self.events[-1])

    def test_保存に失敗した試行のタスクは登録しない(self):
        self.context.save_status.side_effect = RuntimeError('save failed')
        with self.assertLogs(level='ERROR'):
            self.assertIsNone(self._run())
        self.queue.create_task.assert_not_called()
        self.interface.respond_reaction.assert_not_called()

    def test_返信再試行でもタスクは一度だけ登録する(self):
        self.interface.respond_reaction.side_effect = [RuntimeError('reply failed'), 'OK']
        with self.assertLogs(level='ERROR'):
            self.assertEqual('OK', self._run())
        self.context.rollback_status.assert_called_once()
        self.queue.create_task.assert_called_once()
        self.assertEqual(2, self.plan.call_count)

    def test_登録失敗では保存と返信を巻き戻して再planしない(self):
        self.queue.create_task.side_effect = RuntimeError('enqueue failed')
        with self.assertLogs(level='ERROR') as logs:
            self.assertEqual('OK', self._run())
        self.assertEqual(1, self.plan.call_count)
        self.context.save_status.assert_called_once()
        self.interface.respond_reaction.assert_called_once()
        self.context.rollback_status.assert_not_called()
        self.assertTrue(any('enqueue' in entry for entry in logs.output))

    def test_一つの子登録失敗でも後続を試行し復旧対象をログへ残す(self):
        task_id = '12345678-1234-5678-1234-567812345678'
        error = RuntimeError('enqueue failed')
        error.task_id = task_id
        self.queue.create_task.side_effect = [error, 'second-task']

        def plan():
            self.common.run_command(
                self.context, None, '@forward', ['first-bot', '#first', 'liff'])
            self.common.run_command(
                self.context, None, '@forward', ['second-bot', '#second'])

        self.plan.side_effect = plan
        with self.assertLogs(level='ERROR') as logs:
            self.assertEqual('OK', self._run())

        self.assertEqual(2, self.queue.create_task.call_count)
        self.assertEqual(1, self.plan.call_count)
        self.context.rollback_status.assert_not_called()
        record = json.loads(logs.records[0].getMessage())
        self.assertEqual('enqueue', record['phase'])
        self.assertEqual({
            'bot': 'first-bot', 'action': '#first', 'interface': 'liff',
            'user': 'line:user,U1', 'delay_seconds': None, 'task_id': task_id,
        }, record['task'])

    def test_task_id生成前の失敗はIDなしで復旧対象をログへ残す(self):
        self.queue.create_task.side_effect = ValueError('登録前の設定不備')
        with self.assertLogs(level='ERROR') as logs:
            self.assertEqual('OK', self._run())
        record = json.loads(logs.records[0].getMessage())
        self.assertIsNone(record['task']['task_id'])
        self.assertEqual('target', record['task']['bot'])
        self.assertEqual('#next', record['task']['action'])

    def test_例外伝播interfaceでも返信済みの子登録失敗は親成功を維持する(self):
        self.interface.should_raise_exceptions = lambda: True
        self.queue.create_task.side_effect = RuntimeError('enqueue failed')
        with self.assertLogs(level='ERROR'):
            self.assertEqual('OK', self._run())

    def test_正のdelay登録失敗では元の秒数を復旧ログへ残す(self):
        self.queue.create_task.side_effect = RuntimeError('enqueue failed')
        self.plan.side_effect = lambda: self.common.run_command(
            self.context, None, '@delay', ['90', 'target', '#next'])
        with self.assertLogs(level='ERROR') as logs:
            self.assertEqual('OK', self._run())
        record = json.loads(logs.records[0].getMessage())
        self.assertEqual('enqueue', record['phase'])
        self.assertEqual({
            'bot': 'target', 'action': '#next', 'interface': 'line',
            'user': 'line:user,U1', 'delay_seconds': 90, 'task_id': None,
        }, record['task'])

    def test_delayゼロとinterface指定を返信後へ引き継ぐ(self):
        self.plan.side_effect = lambda: self.common.run_command(
            self.context, None, '@delay', ['0', 'target', '#next', 'liff'])
        self.assertEqual('OK', self._run())
        self.queue.create_task.assert_called_once_with(
            'action-queue', '/api/v1/bots/target/action',
            {'user': 'line:user,U1', 'action': '#next', 'interface': 'liff'},
            delay_seconds=0)


if __name__ == '__main__':
    unittest.main()
