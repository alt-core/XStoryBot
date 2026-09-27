"""人工台本を実Python runtimeと書出しで確認し、JS用fixtureの変化を検出する。"""

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import cloud_backend
import commands
import hub

from tests.plugin import test_webchat_liff as liff_fixture
from tests.plugin import test_webchat_runtime_e2e as engine_fixture
from plugin.webchat.interface import WebchatInterface
from plugin.liff.interface import LiffPlugin_Interface
from plugin.line import quick_reply
from tools import trial_scenario


FIXTURES = Path(__file__).parent / 'fixtures' / 'trial'


class TrialScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        liff_fixture.WebchatLiffTest.setUpClass()

    @classmethod
    def tearDownClass(cls):
        liff_fixture.WebchatLiffTest.tearDownClass()

    def setUp(self):
        self.case = liff_fixture.WebchatLiffTest('test_自己Botで状態とメニューを共有し入口だけ任意に無視する')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.data = json.loads((FIXTURES / 'story.json').read_text())
        self.scenario_module = engine_fixture.WebchatRuntimeE2ETest.scenario_module
        patcher = patch.dict(sys.modules, {'scenario': self.scenario_module})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.params = {**self.case.interface.params, 'liff_apps': {
            'menu': {'bot': 'bot', 'url': liff_fixture.PAGE},
        }}
        self.case.interface = WebchatInterface('bot', self.params)
        self.case.interface._scenario_loaded = True
        self.case.root.interfaces = {
            'webchat': self.case.interface,
            'liff': LiffPlugin_Interface('bot', {'allow_origin': '*', 'ignore_unhandled_action': True}),
        }
        self.case.root.scenario_uri = self.case.interface.scenario_uri
        self.case.root.scenario = self.scenario_module.ScenarioBuilder.build_from_tables(self.data['tables'], version=3)

    def compile(self, tables=None, constants=None, params=None):
        built = trial_scenario.build_tables(copy.deepcopy(tables or self.data['tables']), {}, {})
        return trial_scenario.compile_scenario(
            built, bot='bot', params=params or self.params, constants=constants or {},
            quick_reply={'reset_keyword': '!!reset!!'}, media_url=lambda url: url,
            liff={'action_prefix': '##liff.', 'ignore_unhandled_action': True})

    def test_書出し結果が共有fixtureと一致する(self):
        self.assertEqual(json.loads((FIXTURES / 'program.json').read_text()), self.compile())

    def test_共有fixtureの期待値を実Python版で再確認する(self):
        # CommonCommandsのresetは既存fixtureの設定値を使う。
        for case in self.data['cases']:
            with self.subTest(case=case['name']):
                response, active = None, []
                for step in case['steps']:
                    value = step['input']
                    kind = value['type']
                    if kind == 'start':
                        response = self.case.start()
                    elif kind == 'liff':
                        response = self.case.call(response['state_token'], value['action']).json
                    else:
                        if kind == 'choice':
                            action = active[-1]['quick_replies'][value['index']]
                            value = {'type': 'postback', 'postback_token': action['token']}
                        response = engine_fixture.WebchatRuntimeE2ETest._turn(value, response['state_token']).json
                    if response.get('chat_updated', True):
                        active = response['messages']
                    player = self.case.interface.load_state(response['state_token'])['player']
                    actual = {
                        'texts': [message['text'] for message in response['messages'] if message['type'] == 'text'],
                        'choices': [choice['label'] for message in active for choice in message.get('quick_replies', [])],
                        # 通常版の初期aliasを、実際に探索するシーンへ解決して比較する。
                        'scene': self.case.root.scenario.startup_scene_title if player['scene'] == '*start' else player['scene'], 'waiting': quick_reply.QUICK_REPLY_GUARD_VARIABLE in player['flags'],
                    }
                    if kind == 'liff':
                        actual.update(events=response['liff_result'], chat_updated=response['chat_updated'])
                    self.assertEqual(step['expect'], actual)

    def test_未使用の枝も元の行を示して非対応と診断する(self):
        cases = [
            ([['##line.follow', '開始'], ['別の入力', '@set', '$flag', 'true']], '2行目'),
            ([['##line.follow', '開始'], ['[$flag]条件', '本文']], '2行目'),
            ([['##line.follow', '開始'], ['A|B', '本文']], '2行目'),
            ([['##line.follow', '{$$service_name}']], '1行目'),
            ([['##line.follow', '{0}']], '1行目'),
            ([['##line.follow', '#missing']], '1行目'),
        ]
        for rows, position in cases:
            with self.subTest(rows=rows), self.assertRaises(trial_scenario.TrialBuildError) as caught:
                self.compile([('診断', rows)])
            self.assertIn('診断!' + position, str(caught.exception))

    def test_定数の優先順位と書式の対象を維持する(self):
        tables = [('本文', [['##line.follow', '話者{名前}:\n{名前} {{括弧}}']])]
        built = trial_scenario.build_tables(tables, {'名前': 'Sheet'}, {})
        params = {**self.params, 'constants': {'名前': 'Web'}}
        program = trial_scenario.compile_scenario(built, bot='bot', params=params, constants={'名前': '設定'},
            quick_reply={}, media_url=lambda url: url)
        emitted = [op for block in program['blocks'].values() for op in block['ops'] if op['op'] == 'emit'][0]
        self.assertEqual('Web {括弧}', emitted['text'])
        self.assertEqual('話者{名前}', emitted['messages'][0]['sender']['name'])
        with self.assertRaises(trial_scenario.TrialBuildError):
            self.compile([('本文', [['##line.follow', '{未定義}']])])

    def test_guardなしの選択肢に未使用の再提示先を要求しない(self):
        builder = next(builder for _service, builder in hub.builder_list
                       if isinstance(builder, quick_reply.LineQuickReplyPlugin_Builder))
        with patch.object(builder, 'command_without_guard', ['？']):
            program = self.compile([('本文', [
                ['##line.follow', '自由入力もできます'], ['', '？', '選ぶ'], ['', '選びました'],
            ])])
        waiting = next(op for block in program['blocks'].values() for op in block['ops'] if op['op'] == 'wait')
        self.assertFalse(waiting['wait']['guard'])

    def test_固定includeの循環と優先順を展開する(self):
        program = self.compile([
            ('入口', [['##line.follow', '開始'], ['共通', '入口優先']]),
            ('一', [['@include', '*二/'], ['共通', '一の本文']]),
            ('二', [['@include', '*一/'], ['二の条件', '二の本文']]),
        ])
        rules = program['scenes']['一/']['rules']
        self.assertEqual('入口優先', next(program['blocks'][rule['block']]['ops'][0]['text']
            for rule in rules if rule['test'].get('value') == '共通'))
        self.assertTrue(any(rule['test'].get('value') == '二の条件' for rule in rules))

    def test_画像テキストは生成命令で判定し診断にframeを残す(self):
        with patch.object(cloud_backend, 'create_state_store', return_value=Mock()):
            from plugin.line import image_text
        tables = [('画像台本', [
            ['##line.follow', '/画像テキスト', '本文', 'frame'],
            ['##please-select', '選択してください'], ['', '@show_quick_reply_choices'],
        ])]
        for mode in ('none', 'quick_always', 'inner'):
            with self.subTest(mode=mode), patch.object(commands, 'catalog_map', {
                    name: list(entries) for name, entries in commands.catalog_map.items()}), \
                    patch.object(commands, 'catalog', list(commands.catalog)), \
                    patch.object(image_text.ImageTextStatDB, 'get_cached_image_text_stat',
                                 return_value=('https://media.example.test/page', (1040, 520), None)):
                image_text.load_plugin({'more_message': '続き', 'frames': {'frame': {
                    'more_mode': mode, 'please_select_quick_reply_label': '##please-select',
                }}})
                if mode == 'inner':
                    with self.assertRaises(trial_scenario.TrialBuildError) as caught:
                        self.compile(tables)
                    self.assertIn('画像台本!1行目（frame: frame）', str(caught.exception))
                    self.assertIn('@@set_next_label', str(caught.exception))
                else:
                    self.assertTrue(self.compile(tables)['blocks'])


if __name__ == '__main__':
    if sys.argv[1:] == ['--write-fixture']:
        TrialScenarioTest.setUpClass()
        case = TrialScenarioTest('test_書出し結果が共有fixtureと一致する')
        try:
            case.setUp()
            (FIXTURES / 'program.json').write_text(json.dumps(case.compile(), ensure_ascii=False, indent=2) + '\n')
        finally:
            case.doCleanups()
            TrialScenarioTest.tearDownClass()
    else:
        unittest.main()
