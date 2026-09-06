# coding: utf-8
"""plugin.load_plugins の依存欠落と任意pluginの登録境界。"""

from contextlib import ExitStack
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cloud_backend
import commands
import hub
import plugin


class PluginLoadingTest(unittest.TestCase):
    def test_任意pluginの依存packageが無ければ何を入れるか示して停止する(self):
        error = ModuleNotFoundError("No module named 'twilio'", name='twilio')
        with patch.object(plugin.importlib, 'import_module', side_effect=error):
            with self.assertRaises(RuntimeError) as captured:
                plugin.load_plugins({}, {'twilio': {}})
        message = str(captured.exception)
        self.assertIn('plugin.twilio', message)
        self.assertIn('"twilio"', message)
        self.assertIn('requirements-optional.txt', message)

    def test_plugin自体が無い場合は元の例外のまま(self):
        error = ModuleNotFoundError("No module named 'plugin.nothing'", name='plugin.nothing')
        with patch.object(plugin.importlib, 'import_module', side_effect=error):
            with self.assertRaises(ModuleNotFoundError):
                plugin.load_plugins({}, {'nothing': {}})


class MorePluginLoadingTest(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        # 実importで増えるmodule、親packageの属性、各登録先をまとめて元へ戻す。
        stack.enter_context(patch.dict(sys.modules))
        stack.enter_context(patch.dict(plugin.__dict__))
        stack.enter_context(patch.object(plugin, 'plugins', {}))
        stack.enter_context(patch.multiple(
            commands, catalog=[], catalog_map={},
            object_catalog=[], object_catalog_map={}))
        stack.enter_context(patch.multiple(
            hub, builder_list=[], runtime_list=[], method_cache={}))
        for name in list(sys.modules):
            if name == 'plugin.line' or name.startswith('plugin.line.'):
                del sys.modules[name]
        plugin.__dict__.pop('line', None)

        self.renderer = ModuleType('plugin.render_text.renderer')
        self.renderer.render_text_to_png = Mock()
        render_text = ModuleType('plugin.render_text')
        render_text.__path__ = []
        render_text.renderer = self.renderer
        sys.modules['plugin.render_text'] = render_text
        sys.modules['plugin.render_text.renderer'] = self.renderer
        plugin.render_text = render_text
        self.create_store = stack.enter_context(patch.object(
            cloud_backend, 'create_state_store', side_effect=lambda: Mock()))

    def _load(self, frames, with_more=False, image_params=None):
        params = {
            'line.quick_reply': {
                'command': ['＞'], 'default_reply': '続きを読む',
                'please_select_quick_reply_label': '##選び直し',
            },
            'line.image_text': {
                'frames': frames,
                **(image_params or {}),
            },
        }
        if with_more:
            params['line.more'] = {
                'command': ['▼'], 'message': '続きを読む',
                'image_url': 'https://example.invalid/more.png',
                'please_push_more_button_label': '##選び直し',
            }
        plugin.load_plugins({}, params)
        return commands.get_command('@画像テキスト', 3, service='line').builder

    def _builder(self):
        def raise_error(message):
            raise ValueError(message)

        builder = SimpleNamespace(
            version=3, option_force=True, node=None,
            scene=SimpleNamespace(get_relative_position_desc=lambda _node: 'test'),
            add_command=Mock(), add_new_string_block=Mock(),
            build_image_for_imagemap_command=Mock(return_value=(
                'https://example.invalid/more', (1040, 1040))),
            build_image_for_imagemap_command_with_rawdata=Mock(return_value=(
                'https://example.invalid/page', (1040, 1040))),
            raise_error=raise_error,
        )
        self.renderer.render_text_to_png.reset_mock()
        self.renderer.render_text_to_png.side_effect = [
            (b'page-1', '続き'), (b'page-2', '')]
        return builder

    def test_Moreなしで実importとQuickReplyの2ページ生成ができる(self):
        image_builder = self._load({
            mode: {'more_mode': mode} for mode in ('quick_between', 'quick_always')
        })
        self.assertNotIn('plugin.line.more', sys.modules)
        self.assertIsNone(commands.get_command('@@set_next_label', 3, service='line'))
        guard = commands.get_command('@@set_quick_reply_guard', 3, service='line')
        self.assertIn(('line', guard.runtime), hub.runtime_list)

        for mode, expected in (('quick_between', 1), ('quick_always', 2)):
            with self.subTest(mode=mode):
                builder = self._builder()
                image_builder.build_from_command(
                    builder, None, '@画像テキスト', ['本文', mode])
                emitted = [call.args[1] for call in builder.add_command.call_args_list]
                self.assertEqual(2, emitted.count('@imagemap'))
                self.assertEqual(expected, emitted.count('@@set_quick_reply_guard'))
                self.assertNotIn('@@set_next_label', emitted)
                self.assertEqual(2, self.renderer.render_text_to_png.call_count)
                builder.build_image_for_imagemap_command.assert_not_called()
        self.assertNotIn('plugin.line.more', sys.modules)
        self.assertEqual(1, self.create_store.call_count)

    def test_More明示時は登録され各方式の2ページ生成を維持する(self):
        image_builder = self._load({
            mode: {'more_mode': mode} for mode in ('between', 'always', 'inner')
        }, with_more=True, image_params={
            'more_message': '続きを読む',
            'more_image_url': 'https://example.invalid/more.png',
        })
        self.assertIn('plugin.line.more', sys.modules)
        self.assertEqual(2, self.create_store.call_count)
        entry = commands.get_command('@@set_next_label', 3, service='line')
        self.assertIn(('line', entry.runtime), hub.runtime_list)
        for mode, expected in (('between', 1), ('always', 2), ('inner', 2)):
            with self.subTest(mode=mode):
                builder = self._builder()
                image_builder.build_from_command(
                    builder, None, '@画像テキスト', ['本文', mode])
                emitted = [call.args[1] for call in builder.add_command.call_args_list]
                self.assertEqual(expected, emitted.count('@@set_next_label'))
                self.assertEqual(2, self.renderer.render_text_to_png.call_count)

    def test_More未設定で旧方式を選ぶと描画前に理由を示して拒否する(self):
        frames = {mode: {'more_mode': mode} for mode in ('between', 'always', 'inner')}
        frames['default'] = {}
        image_builder = self._load(frames)
        for frame in frames:
            with self.subTest(frame=frame):
                builder = self._builder()
                with self.assertRaisesRegex(ValueError, r'line\.more'):
                    image_builder.build_from_command(
                        builder, None, '@画像テキスト', ['本文', frame])
                self.renderer.render_text_to_png.assert_not_called()
                builder.add_command.assert_not_called()
        self.assertNotIn('plugin.line.more', sys.modules)
        self.assertEqual(1, self.create_store.call_count)

    def test_More方式の必要値欠落は描画前に設定名を示す(self):
        image_builder = self._load({
            'no_message': {'more_mode': 'inner'},
            'empty_message': {'more_mode': 'inner', 'more_message': ''},
            'no_url': {'more_mode': 'between', 'more_message': '続きを読む'},
            'always_no_url': {'more_mode': 'always', 'more_message': '続きを読む'},
        }, with_more=True)
        for frame, missing in (
                ('no_message', 'more_message'), ('empty_message', 'more_message'),
                ('no_url', 'more_image_url'), ('always_no_url', 'more_image_url')):
            with self.subTest(frame=frame):
                builder = self._builder()
                with self.assertRaisesRegex(ValueError, missing):
                    image_builder.build_from_command(
                        builder, None, '@画像テキスト', ['本文', frame])
                self.renderer.render_text_to_png.assert_not_called()
                builder.add_command.assert_not_called()

    def test_innerは追加画像URLなしでフレームと第3引数のメッセージを使う(self):
        image_builder = self._load({
            'frame': {'more_mode': 'inner', 'more_message': 'フレーム指定'},
            'command': {'more_mode': 'inner'},
        }, with_more=True)
        for options, expected in (
                (['本文', 'frame'], 'フレーム指定'),
                (['本文', 'command', 'コマンド指定'], 'コマンド指定'),
                (['本文', 'frame', '上書き'], '上書き')):
            with self.subTest(options=options):
                builder = self._builder()
                image_builder.build_from_command(builder, None, '@画像テキスト', options)
                labels = [call.args[2] for call in builder.add_command.call_args_list
                          if call.args[1] == '@@set_next_label']
                self.assertEqual([expected, expected], [values[1] for values in labels])
                self.assertEqual(2, self.renderer.render_text_to_png.call_count)
                builder.build_image_for_imagemap_command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
