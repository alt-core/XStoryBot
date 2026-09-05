"""command引数書式（可変長 ... 、@delay／@webhook）の回帰テスト。"""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

import commands
import common_commands
import hub


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_scenario_module():
    """ObjectStore の生成だけ差し替えて、実 ScenarioBuilder を隔離 load する。"""
    cloud_backend = types.ModuleType('cloud_backend')
    cloud_backend.__path__ = []
    cloud_backend.create_object_store = Mock()
    contracts = types.ModuleType('cloud_backend.contracts')
    contracts.InvalidObjectReferenceError = type(
        'InvalidObjectReferenceError', (Exception,), {})
    module_name = 'tests._command_arguments_scenario'
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / 'scenario.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        'cloud_backend': cloud_backend,
        'cloud_backend.contracts': contracts,
        module_name: module,
    }):
        spec.loader.exec_module(module)
    return module


class FakeBuilder:
    def __init__(self, version=3):
        self.version = version

    def raise_error(self, msg, *args):
        raise ValueError(msg)


class FakeNode:
    def __init__(self, term, children=None):
        self.term = list(term)
        self.children = children or []

    def get_factors(self, index):
        return self.term[index:]


class FormatStringTest(unittest.TestCase):
    def test_trailing_marker_makes_last_format_variadic(self):
        formats = commands._convert_format_string('raw [raw]...')
        self.assertTrue(formats.variadic)
        self.assertEqual(2, len(formats))
        self.assertEqual((['raw'], True, 0), formats[0])
        self.assertEqual((['raw'], False, 0), formats[1])

    def test_marker_is_only_allowed_on_last_format(self):
        with self.assertRaises(ValueError):
            commands._convert_format_string('[raw]... raw')

    def test_plain_format_is_not_variadic(self):
        formats = commands._convert_format_string('number text|label')
        self.assertFalse(formats.variadic)
        self.assertEqual(2, len(formats))

    def test_empty_format_keeps_list_contract(self):
        formats = commands._convert_format_string(None)
        self.assertEqual([], formats)
        self.assertFalse(formats.variadic)


class ParseCommandArgumentsTest(unittest.TestCase):
    def setUp(self):
        hub.clear()
        commands.clear()
        common_commands.setup({'reset_keyword': '!reset', 'timezone': 'Asia/Tokyo'})

    def tearDown(self):
        hub.clear()
        commands.clear()

    def _options(self, row, version=3):
        builder = FakeBuilder(version)
        _entry, _sender, _cmd, options, _children, _grand = commands.parse_command(
            builder, FakeNode(row))
        return options

    def test_webhook_accepts_key_value_pairs_after_url(self):
        self.assertEqual(
            ['https://hooks.example.test/x', 'k1', 'v1', 'k2', 'v2'],
            self._options(['@webhook', 'https://hooks.example.test/x', 'k1', 'v1', 'k2', 'v2']))

    def test_webhook_url_only_is_unchanged(self):
        self.assertEqual(
            ['https://hooks.example.test/x'],
            self._options(['@webhook', 'https://hooks.example.test/x']))

    def test_delay_keeps_two_argument_form(self):
        self.assertEqual(['60', '#later'], self._options(['@delay', '60', '#later']))

    def test_delay_accepts_bot_name_before_action(self):
        self.assertEqual(
            ['60', 'otherbot', '#later'],
            self._options(['@delay', '60', 'otherbot', '#later']))

    def test_delay_is_not_variadic(self):
        self.assertEqual(
            ['60', 'otherbot', '#later'],
            self._options(['@delay', '60', 'otherbot', '#later', 'memo']))

    def test_forward_still_reads_two_cells(self):
        self.assertEqual(['bot', '#x'], self._options(['@forward', 'bot', '#x', 'extra']))

    def test_seq_keeps_sixteen_label_limit(self):
        # 既存 Scenario の意味を変えないため、17個目以降のセルは従来どおり読まない
        labels = [f'#l{index}' for index in range(17)]
        self.assertEqual(labels[:16], self._options(['@seq'] + labels))

    def test_seq_still_compacts_empty_cells(self):
        self.assertEqual(['#a', '#c'], self._options(['@seq', '#a', '', '#c']))

    def test_seq_v3_without_labels_is_control_flow_form(self):
        self.assertEqual([], self._options(['@seq']))

    def test_seq_v1_requires_first_label(self):
        with self.assertRaises(ValueError):
            self._options(['@seq'], version=1)
        self.assertEqual(['#a'], self._options(['@seq', '#a'], version=1))

    def test_call_keeps_fifteen_argument_limit(self):
        # 16個目は読まない（Director が消去する $$1〜$$15 と揃える）
        args = [f'a{index}' for index in range(16)]
        self.assertEqual(['#sub'] + args[:15], self._options(['@call', '#sub'] + args))

    def test_trailing_empty_cells_are_still_removed(self):
        self.assertEqual(
            ['https://hooks.example.test/x', 'k', 'v'],
            self._options(['@webhook', 'https://hooks.example.test/x', 'k', 'v', '', '']))

    def test_webhook_keeps_empty_value_in_place(self):
        # key/value の対応を保つため、@webhook だけは途中の空セルを詰めない
        self.assertEqual(
            ['https://hooks.example.test/x', 'a', '', 'b', '2'],
            self._options(['@webhook', 'https://hooks.example.test/x', 'a', '', 'b', '2']))


class WebhookRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.runtime = common_commands.CommonCommands_Runtime({'reset_keyword': '!reset'})
        self.context = types.SimpleNamespace(service_name='line', status={}, version=3)

    def test_posts_key_value_pairs_as_form_data(self):
        with patch.object(common_commands.requests, 'post') as post:
            handled = self.runtime.run_command(
                self.context, None, '@webhook',
                ['https://hooks.example.test/x', 'a', '1', 'b', '2'])
        self.assertTrue(handled)
        post.assert_called_once_with(
            'https://hooks.example.test/x', data={'a': '1', 'b': '2'}, timeout=120)

    def test_posts_without_data_when_only_url(self):
        with patch.object(common_commands.requests, 'post') as post:
            self.runtime.run_command(self.context, None, '@webhook', ['https://hooks.example.test/x'])
        post.assert_called_once_with('https://hooks.example.test/x', data=None, timeout=120)

    def test_dangling_key_is_sent_with_empty_value(self):
        # Sheet の末尾空セルは読み込み時に消えるため、value の無い最後の key は空文字として送る
        with patch.object(common_commands.requests, 'post') as post:
            self.runtime.run_command(
                self.context, None, '@webhook', ['https://hooks.example.test/x', 'a', '1', 'b'])
        post.assert_called_once_with(
            'https://hooks.example.test/x', data={'a': '1', 'b': ''}, timeout=120)

    def test_empty_value_in_the_middle_is_sent_as_empty_string(self):
        with patch.object(common_commands.requests, 'post') as post:
            self.runtime.run_command(
                self.context, None, '@webhook',
                ['https://hooks.example.test/x', 'a', '', 'b', '2'])
        post.assert_called_once_with(
            'https://hooks.example.test/x', data={'a': '', 'b': '2'}, timeout=120)


class WebhookBuilderToRuntimeTest(unittest.TestCase):
    """実 ScenarioBuilder が組んだ @webhook の引数が、そのまま runtime で送られることを確認する。"""

    @classmethod
    def setUpClass(cls):
        cls.scenario_module = _load_scenario_module()

    def setUp(self):
        hub.clear()
        commands.clear()
        common_commands.setup({'reset_keyword': '!reset', 'timezone': 'Asia/Tokyo'})

    def tearDown(self):
        hub.clear()
        commands.clear()

    def _built_webhook_options(self, cells):
        built = self.scenario_module.ScenarioBuilder.build_from_table(
            [['hook', '@webhook'] + cells], version=3)
        for scene in built.scenes.values():
            for region in scene.regions:
                for _condition, lines in region.blocks:
                    for command in lines:
                        if command.msg in common_commands.WEBHOOK_CMDS:
                            return command.options
        self.fail('@webhook が build されていません')

    def _post_data(self, options):
        runtime = common_commands.CommonCommands_Runtime({'reset_keyword': '!reset'})
        context = types.SimpleNamespace(service_name='line', status={}, version=3)
        with patch.object(common_commands.requests, 'post') as post:
            runtime.run_command(context, None, '@webhook', options)
        post.assert_called_once()
        return post.call_args.kwargs['data']

    def test_url_only(self):
        options = self._built_webhook_options(['https://hooks.example.test/x'])
        self.assertIsNone(self._post_data(options))

    def test_pairs_with_empty_value_in_the_middle(self):
        options = self._built_webhook_options(
            ['https://hooks.example.test/x', 'a', '', 'b', '2'])
        self.assertEqual({'a': '', 'b': '2'}, self._post_data(options))

    def test_last_key_without_value(self):
        options = self._built_webhook_options(
            ['https://hooks.example.test/x', 'a', '1', 'b'])
        self.assertEqual({'a': '1', 'b': ''}, self._post_data(options))


if __name__ == '__main__':
    unittest.main()
