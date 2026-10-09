import builtins
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import uuid

import build_job
import build_service


class BuildServiceTest(unittest.TestCase):
    def setUp(self):
        self.bot = Mock()
        self.bot.build_scenario.return_value = True, None
        self.main = Mock()
        self.main.get_bot.return_value = self.bot
        self.main.get_options.return_value = {'scenario_version': 3}

    def test_httpとバッチで共有する引数を構築する(self):
        result = build_service.build_bot(
            'bot',
            task_id='task-1',
            skip_image=True,
            force=False,
            main_module=self.main,
        )

        self.assertEqual(result, (True, None))
        self.main.get_bot.assert_called_once_with('bot')
        self.bot.build_scenario.assert_called_once_with(
            task_id='task-1',
            options={'skip_image': True, 'force': False},
            version=3,
        )

    def test_解決済みBotも同じ共有処理でビルドする(self):
        result = build_service.build_runtime(
            self.bot,
            task_id='task-2',
            skip_image=False,
            force=True,
            version=2,
        )

        self.assertEqual(result, (True, None))
        self.bot.build_scenario.assert_called_once_with(
            task_id='task-2',
            options={'skip_image': False, 'force': True},
            version=2,
        )

    def test_scenario_version未指定時は従来値を使う(self):
        self.main.get_options.return_value = {}

        build_service.build_bot('bot', main_module=self.main)

        self.bot.build_scenario.assert_called_once_with(
            task_id='',
            options={'skip_image': False, 'force': False},
            version=1,
        )

    def test_存在しないBotを明示的な例外にする(self):
        self.main.get_bot.return_value = None

        with self.assertRaises(build_service.BotNotFoundError):
            build_service.build_bot('missing', main_module=self.main)


class BuildJobTest(unittest.TestCase):
    def test_CLI引数と明示IDを共有処理へ渡して成功終了する(self):
        with patch.object(build_service, 'build_bot', return_value=(True, None)) as run:
            result = build_job.main([
                '--bot-name=-bot', '--task-id=task-1', '--skip-image', '--force'])

        self.assertEqual(0, result)
        run.assert_called_once_with(
            bot_name='-bot', task_id='task-1', skip_image=True, force=True)

    def test_省略したtask_IDは呼出しごとに異なるcanonical_UUIDを生成する(self):
        with patch.object(build_service, 'build_bot', return_value=(True, None)) as run:
            self.assertEqual(0, build_job.main(['--bot-name', 'bot']))
            self.assertEqual(0, build_job.main(['--bot-name', 'bot']))

        ids = [entry.kwargs['task_id'] for entry in run.call_args_list]
        self.assertNotEqual(ids[0], ids[1])
        for task_id in ids:
            self.assertEqual(str(uuid.UUID(task_id)), task_id)
        self.assertEqual(run.call_args.kwargs, {
            'bot_name': 'bot', 'task_id': ids[-1], 'skip_image': False, 'force': False,
        })

    def test_論理失敗は終了値1にする(self):
        with patch.object(build_service, 'build_bot', return_value=(False, 'error')):
            self.assertEqual(1, build_job.main(['--bot-name', 'bot']))

    def test_存在しないBotは終了値2にする(self):
        with patch.object(build_service, 'build_bot',
                          side_effect=build_service.BotNotFoundError('missing')):
            with self.assertLogs(level='ERROR'):
                self.assertEqual(2, build_job.main(['--bot-name', 'missing']))

    def test_CLI読込とhelpと引数不足ではmainやsettingsを初期化しない(self):
        original_import = builtins.__import__

        def import_module(name, *args, **kwargs):
            if name in ('main', 'settings'):
                raise AssertionError('引数の検査前に実行設定を読み込んではならない')
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, '__import__', side_effect=import_module), \
                patch.object(build_service, 'build_bot') as build:
            spec = importlib.util.spec_from_file_location(
                '_test_build_cli', Path(__file__).resolve().parents[1] / 'build_job.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for arguments, code in ((['--help'], 0), ([], 2), (['--bot-name'], 2)):
                with self.subTest(arguments=arguments), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as raised:
                    module.main(arguments)
                self.assertEqual(code, raised.exception.code)
            build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
