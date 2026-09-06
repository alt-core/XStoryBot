"""実際に書式展開する文字列だけをbuild時に検査する。"""

import importlib.util
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cloud_backend
import commands
import common_commands
import expression
import hub
from expression import Expression
from plugin.line import default_commands, quick_reply
from plugin.render_text import renderer


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SourceRow(list):
    def __init__(self, values, source_position):
        super().__init__(values)
        self.source_position = source_position


class ScenarioFormattingTest(unittest.TestCase):
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
            self.scenario = _load_module(
                'tests._scenario_formatting', 'scenario.py')

    def _build(self, rows, version=3):
        return self.scenario.ScenarioBuilder.build_from_table(
            rows, version=version, options={'force': True})

    def _commands(self, built):
        return [command for _condition, lines in built.scenes['default/'].base_region.blocks
                for command in lines]

    def test_未定義変数の後の不正括弧も作成位置付きで拒否する(self):
        for text in ('{$unknown} の後に }', '{$unknown} の後に {'):
            with self.subTest(text=text):
                with self.assertRaises(self.scenario.ScenarioSyntaxError) as caught:
                    self.scenario.ScenarioBuilder.build_from_tables([
                        ('物語', [['開始', '本文'], ['次へ', text]]),
                    ], version=3)

                self.assertIn('文字列の書式が不正です', str(caught.exception))
                self.assertIn(text, str(caught.exception))
                self.assertIn('＠物語!2行目', str(caught.exception))

    def test_表示行番号だけを一始まりにして内部番号と生成ラベルは変えない(self):
        node = self.scenario.SyntaxTree('物語', 0, ['本文'])
        scene = self.scenario.Scene('物語', line_no=0)

        self.assertIn('＠物語!1行目', str(node))
        self.assertEqual(node.line_no, 0)
        self.assertEqual(scene.get_relative_position_desc(node), '物語/_L0')

    def test_環境Sheetの空行やコメントの後でも元の作成位置を表示する(self):
        rows = [
            SourceRow(['開始', '本文'], ('物語', 0)),
            SourceRow([], ('物語.test', 0)),
            SourceRow([';', 'コメント'], ('物語.test', 1)),
            SourceRow(['次へ', '{$unknown} }'], ('物語.test', 2)),
        ]
        with self.assertRaises(self.scenario.ScenarioSyntaxError) as caught:
            self.scenario.ScenarioBuilder.build_from_tables([('物語', rows)], version=3)

        self.assertIn('＠物語.test!3行目', str(caught.exception))

    def test_元Sheet位置を付けてもQuickReplyの内部ラベルを変えない(self):
        quick_reply.load_plugin({
            'command': ['＞'], 'default_reply': '続きを読む',
            'please_select_quick_reply_label': '##please',
        })
        rows = [['開始', '本文'], ['', '＞', '次へ'], ['', '続き']]
        plain = self._build(rows)
        located = self._build([
            SourceRow(row, ('物語.test', index + 10)) for index, row in enumerate(rows)
        ])

        def command_values(built):
            return [(command.msg, command.options, command.children)
                    for command in self._commands(built)]

        self.assertEqual(command_values(plain), command_values(located))

    def test_未定義変数の後の過剰なformat_specの入れ子を拒否する(self):
        text = '{$unknown} {$n:{$width:{$precision}}}'
        with self.assertRaises(self.scenario.ScenarioSyntaxError) as caught:
            self._build([['開始', text]])

        self.assertIn('書式の入れ子が深すぎます', str(caught.exception))

    def test_raw引数も実行時に展開するので構文を検査する(self):
        with self.assertRaises(self.scenario.ScenarioSyntaxError) as caught:
            self._build([
                ['開始', '@postjson', 'https://example.invalid/api', '{$unknown} }'],
            ])

        self.assertIn('{$unknown} }', str(caught.exception))

    def test_合法な書式は変数を評価せず保存し実Formatterでも展開できる(self):
        values = [
            ('{{本文}}', {}, '{本文}'),
            ('{$n:{$width}}', {'$n': 7, '$width': 3}, '  7'),
            ('{{"name":{$name!j}}}', {'$name': 'guide'}, '{"name":"guide"}'),
        ]
        formatter = self.scenario.StringFormatter()
        for text, env, expected in values:
            with self.subTest(text=text):
                built = self._build([['開始', text]])
                command = self._commands(built)[0]
                self.assertEqual(command.msg, text)
                self.assertEqual(formatter.vformat(command.msg, [], env), expected)

        # 変数の値によって決まるformat specの可否はbuild時には判定しない。
        self._build([['開始', '{$unknown:04d}']])

    def test_senderとExpressionの文字列を検査しない(self):
        built = self._build([
            ['開始', '語り}手：\n本文'],
            ['', '@set', '$text', '"}"'],
        ])

        text, assignment = self._commands(built)
        self.assertEqual(text.sender, '語り}手')
        self.assertIsInstance(assignment.options[1], Expression)
        self.assertEqual(assignment.options[1].eval({}, []), '}')

    def test_childrenはv3だけで検査する(self):
        rows = [['開始', '@button', '選択'], ['', '', '右 }']]
        for version in (1, 2):
            with self.subTest(version=version):
                built = self._build(rows, version=version)
                self.assertEqual(self._commands(built)[0].children, [['右 }']])
        with self.assertRaises(self.scenario.ScenarioSyntaxError):
            self._build(rows, version=3)

    def test_v3Carouselのさらに深いchildrenを検査しない(self):
        built = self._build([
            ['開始', '@carousel'],
            ['', '', '本文 }'],
            ['', '', '', '右 }'],
        ])

        self.assertEqual(self._commands(built)[0].children, [
            [
                ['本文 }'],
                [['右 }']],
            ],
        ])

    def test_pluginのfilterが生成した本文も検査する(self):
        hub.register_handler('line', builder=types.SimpleNamespace(
            filter_plain_text=lambda *_args: '{$unknown} }',
        ))
        with self.assertRaises(self.scenario.ScenarioSyntaxError):
            self._build([['開始', '生成用の入力']])

    def test_画像化前の本文は書式展開しないので検査しない(self):
        with patch.object(cloud_backend, 'create_state_store', return_value=Mock()):
            image_text = _load_module(
                'tests._formatting_image_text', 'plugin/line/image_text.py')
        image_text.load_plugin({
            'more_message': 'more',
            'more_image_url': 'https://example.invalid/more',
            'default_frame': 'book',
            'frames': {'book': {'more_mode': 'quick_between'}},
        })
        with (
            patch.object(renderer, 'render_text_to_png', return_value=(b'image', '')) as render,
            patch.object(
                self.scenario.ScenarioBuilder,
                'build_image_for_imagemap_command_with_rawdata',
                return_value=('https://example.invalid/image', (1040, 1040)),
            ),
        ):
            built = self._build([['開始', '@画像テキスト', '波括弧 } を描画']])

        self.assertEqual(render.call_args.args[0], '波括弧 } を描画')
        self.assertEqual(self._commands(built)[0].msg, '@imagemap')


if __name__ == '__main__':
    unittest.main()
