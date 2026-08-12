import unittest
from unittest.mock import Mock, patch

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
    def test_CLI引数を共有処理へ渡して成功終了する(self):
        with patch.object(
                build_service, 'build_bot', return_value=(True, None)) as run:
            result = build_job.main([
                '--bot-name', 'bot',
                '--task-id', 'task-1',
                '--skip-image',
                '--force',
            ])

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            bot_name='bot',
            task_id='task-1',
            skip_image=True,
            force=True,
        )

    def test_論理失敗は終了値1にする(self):
        with patch.object(
                build_service, 'build_bot', return_value=(False, 'error')):
            result = build_job.main([
                '--bot-name', 'bot', '--task-id', 'task-1'])

        self.assertEqual(result, 1)

    def test_存在しないBotは入力不備の終了値にする(self):
        with patch.object(
                build_service,
                'build_bot',
                side_effect=build_service.BotNotFoundError('missing')):
            result = build_job.main([
                '--bot-name', 'missing', '--task-id', 'task-1'])

        self.assertEqual(result, 2)

    def test_必須引数不備はargparseの終了値2にする(self):
        with self.assertRaises(SystemExit) as raised:
            build_job.main(['--bot-name', 'bot'])

        self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
