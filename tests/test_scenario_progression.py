"""実BuilderとRuntimeで、複数入力にまたがる物語の進行を確認する。"""

import sys
import types
import unittest
from unittest.mock import Mock, patch

import cloud_backend
import commands
import common_commands
import expression
import hub
from plugin.line import default_commands, quick_reply
from plugin.webchat.context import WebchatActionContext
from tests.test_scenario_formatting import _load_module


class ScenarioProgressionTest(unittest.TestCase):
    def setUp(self):
        for patcher in (
            patch.multiple(
                hub, builder_list=[], runtime_list=[], method_cache={},
                interface_factory_map={}, scenario_loader_factory_map={}),
            patch.multiple(
                commands, catalog=[], catalog_map={},
                object_catalog=[], object_catalog_map={}),
            patch.multiple(expression, **{
                name: getattr(expression, name) for name in (
                    'EXPRESSION_VERSION', 'TRUE_VALUE', 'FALSE_VALUE', 'NONE_VALUE')
            }),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        options = {'reset_keyword': '!reset', 'timezone': 'Asia/Tokyo'}
        common_commands.setup(options)
        default_commands.inner_load_plugin({**options, 'alt_text': 'alt'})
        with patch.object(cloud_backend, 'create_object_store', return_value=Mock()):
            self.scenario = _load_module('tests._scenario_progression', 'scenario.py')
        settings = types.ModuleType('settings')
        settings.CONSTANTS = {}
        with patch.dict(sys.modules, {'settings': settings}):
            self.runtime = _load_module('tests._progression_runtime', 'runtime.py')
        self.runtime._director_class = self.scenario.Director
        self.interface = types.SimpleNamespace(
            allowed_commands=(), turn_deadline_seconds=29,
            get_retry_count=lambda: 0,
            should_raise_exceptions=lambda: True,
            respond_reaction=lambda _context, reactions: reactions,
        )

    def _build(self, rows):
        self.bot = self.runtime.BotRuntime('bot', {'webchat': self.interface}, None)
        self.bot.scenario = self.scenario.ScenarioBuilder.build_from_table(rows, version=3)
        self.player = None

    def _turn(self, action):
        context = WebchatActionContext(
            'bot', self.interface, 'conversation', action, self.player)
        self.reactions = self.bot.handle_action(context)
        self.player = context.saved_player
        self.context = context
        return [row[1] for row, _children in self.reactions if not row[1].startswith('@')]

    def test_条件と変数を次の入力へ引き継ぐ(self):
        self._build([
            ['開始', '@set', '$count', '0'],
            ['', '準備完了'],
            ['進む', '@set', '$count', '$count + 1'],
            ['', '/if', '$count < 2'],
            ['', '一歩目です'],
            ['', '/else'],
            ['', '二歩目です'],
            ['', '/end'],
            ['', '回数:{$count}'],
        ])

        self.assertEqual(self._turn('開始'), ['準備完了'])
        self.assertEqual(self._turn('進む'), ['一歩目です', '回数:1'])
        self.assertEqual(self._turn('進む'), ['二歩目です', '回数:2'])
        self.assertEqual(self.player['flags']['$count'], 2)

    def test_seqは最後で止まりloopは最初へ戻る(self):
        self._build([
            ['順番', '/seq'],
            ['', '一番'],
            ['', '/else'],
            ['', '二番'],
            ['', '/end'],
            ['循環', '/loop'],
            ['', '朝'],
            ['', '/else'],
            ['', '夜'],
            ['', '/end'],
        ])

        self.assertEqual([self._turn('順番') for _ in range(3)], [
            ['一番'], ['二番'], ['二番'],
        ])
        self.assertEqual([self._turn('循環') for _ in range(3)], [
            ['朝'], ['夜'], ['朝'],
        ])

    def test_randomは直前の枝を避けて次の入力へ履歴を引き継ぐ(self):
        self._build([
            ['抽選', '/random'],
            ['', 'A'],
            ['', '/else'],
            ['', 'B'],
            ['', '/else'],
            ['', 'C'],
            ['', '/end'],
        ])

        with patch.object(self.scenario.random, 'randint', side_effect=[1, 1, 0]):
            replies = [self._turn('抽選') for _ in range(3)]
        self.assertEqual(replies, [['B'], ['C'], ['A']])

    def test_callは引数を渡して元のsceneへreturnし結果を次の入力でも読める(self):
        self._build([
            ['呼ぶ', '@call', '*sub', '旅人'],
            ['', '復帰:{$$result}'],
            ['確認', '前の戻り値:{$$result}'],
            ['*sub', '受け取った:{$$1}'],
            ['', '@return', '$$1 + "さん"'],
        ])

        self.assertEqual(self._turn('呼ぶ'), ['受け取った:旅人', '復帰:旅人さん'])
        self.assertEqual(self.player['scene'], 'default/')
        self.assertEqual(self.player['scene_history'], [])
        self.assertEqual(self._turn('確認'), ['前の戻り値:旅人さん'])

    def test_QuickReplyは再提示後も選択でき選択後にguardを解除する(self):
        quick_reply.load_plugin({
            'command': ['＞'], 'default_reply': '続きを読む',
            'please_select_quick_reply_label': '##please',
        })
        self._build([
            ['質問', 'どちらにしますか'],
            ['', '＞', '左', '右'],
            ['', '左を選んだ'],
            ['', '@set', '$choice', '"left"'],
            ['', '/else'],
            ['', '右を選んだ'],
            ['', '@set', '$choice', '"right"'],
            ['', '/end'],
            ['', '選択済み'],
            ['##please', '候補から選んでください'],
            ['', '@show_quick_reply_choices'],
        ])

        guard_key = quick_reply.QUICK_REPLY_GUARD_VARIABLE
        self.assertEqual(self._turn('質問'), ['どちらにしますか'])
        original_label = self.player['flags'][guard_key]['label']
        self.assertEqual(self._turn('わからない'), ['候補から選んでください'])
        self.assertEqual(self.player['flags'][guard_key]['label'], original_label)
        self.assertEqual(self.player['flags'][guard_key]['retry_count'], 1)
        choices = next(children for row, children in self.reactions if row[1] == '@reply')
        self.assertEqual([choice[0] for choice in choices], ['左', '右'])
        self.assertEqual(self._turn('右'), ['右を選んだ', '選択済み'])
        self.assertNotIn(guard_key, self.player['flags'])
        self.assertEqual(self.player['flags']['$choice'], 'right')

    def test_存在しない移動先をログへ残しfallbackの入力値は変えない(self):
        for target in ('#missing', '*missing'):
            with self.subTest(target=target):
                self._build([
                    ['開始', target],
                    ['##error_invalid_label', 'fallback:{$$invalid_label}'],
                ])

                with self.assertLogs(level='WARNING') as logged:
                    self.assertEqual(self._turn('開始'), ['fallback:開始'])

                self.assertEqual(self.context.env['$$invalid_label'], '開始')
                self.assertTrue(any(target in message for message in logged.output), logged.output)


if __name__ == '__main__':
    unittest.main()
