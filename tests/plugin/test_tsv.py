"""TSVとSheetsで同じ表の順序・定数・DSL解釈になることを確認する。"""

import csv
import json
import pickle
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import cloud_backend
import commands
import common_commands
import expression
import hub
from plugin import tsv
from plugin.line import default_commands, quick_reply
from plugin.scenario_table import SourceRow
from tests.plugin.test_google_sheets import (
    FakeSession, load_google_sheets_module, sheet_metadata,
)
from tests.test_scenario_formatting import _load_module


class TsvLoaderTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.manifest = self.root / 'manifest.json'
        self.settings = types.SimpleNamespace(DEPLOY_ENV='test')

    def _write_tables(self, named_tables):
        declarations = []
        for index, (title, rows) in enumerate(named_tables):
            path = self.root / f'{index}.tsv'
            with path.open('w', encoding='utf-8', newline='') as target:
                csv.writer(target, dialect='excel-tab', lineterminator='\n').writerows(rows)
            declarations.append({'name': title, 'path': path.name})
        self.manifest.write_text(json.dumps({'sheets': declarations}), encoding='utf-8')

    def _load(self, **params):
        loader = tsv.TsvPlugin_Loader({'manifest': str(self.manifest), **params})
        with patch.dict(sys.modules, {'settings': self.settings}):
            return loader.load_scenario()

    def test_引用と空セルと空行を保ち物理開始行を記録する(self):
        rows = [
            [], ['開始', '一行目\n二行目', ''],
            ['', '@set', '$name', '"旅人"'],
            ['次へ', '本文\t続き', '=IMAGE("https://example.invalid/image.png")'],
        ]
        self._write_tables([('story', rows)])

        tables, constants = self._load()

        self.assertEqual(tables, [('story', rows)])
        self.assertEqual(constants, {})
        loaded_rows = tables[0][1]
        self.assertEqual([0, 1, 3, 4], [row.source_position[1] for row in loaded_rows])
        self.assertTrue(all(isinstance(row, SourceRow) for row in loaded_rows))
        self.assertIn('story', loaded_rows[1].source_position[0])
        self.assertIn(str(self.root / '0.tsv'), loaded_rows[1].source_position[0])
        self.assertEqual(loaded_rows[1].source_path, str(self.root / '0.tsv'))

        restored = pickle.loads(pickle.dumps((tables, constants)))
        self.assertEqual(restored, (tables, constants))
        self.assertEqual(
            restored[0][0][1][1].source_position,
            loaded_rows[1].source_position)

    def test_環境別の入力順と定数の後勝ちをSheetsと揃える(self):
        named_tables = [
            ('story.test', [['環境別', '先に読む']]),
            ('other', [['別の表', '本文']]),
            ('story', [[], ['共通', '後に読む']]),
            ('story.prod', [['本番', '対象外']]),
            ('_ignored', [['無視', '対象外']]),
            ('$constants', [
                ['title', 'value'], ['', '共通'],
                ['choices', 'list'], ['', 'A'], ['', 'B'],
                ['flags', 'dict'], ['', 'base', 'true'], ['', 'changed', '1'],
                ['people', 'list_table', 'name', 'score'], ['', '', '旅人', '2'],
                ['items', 'dict_table', 'price'], ['', 'key', '3.5'],
            ]),
            ('$constants.test', [
                ['title', 'value'], ['', '環境別'],
                ['flags', 'dict'], ['', 'changed', '2'], ['', 'extra', 'null'],
            ]),
        ]
        self._write_tables(named_tables)
        tables, constants = self._load()
        module, _credentials = load_google_sheets_module()
        selected = [pair for pair in named_tables if pair[0] not in ('story.prod', '_ignored')]
        session = FakeSession([
            sheet_metadata([title for title, _rows in named_tables]),
            {'valueRanges': [{'values': rows} for _title, rows in selected]},
        ])
        loader = module.GoogleSheetPlugin_Loader({})
        loader.get_session = lambda: session

        self.assertEqual((tables, constants), loader._get_table_from_google_sheets('test-sheet'))
        self.assertEqual(['story', 'other'], [title for title, _rows in tables])
        self.assertEqual([['環境別', '先に読む'], [], ['共通', '後に読む']], tables[0][1])
        self.assertEqual(constants, {
            'title': '環境別', 'choices': ['A', 'B'],
            'flags': {'base': True, 'changed': 2, 'extra': None},
            'people': [{'name': '旅人', 'score': 2}],
            'items': {'key': {'price': 3.5}},
        })

    def test_選択条件はSheetsと同じで対象外ファイルを開かない(self):
        self._write_tables([
            ('scene.TEST', [['開始', '本文']]),
            ('constant', [['answer', 'value'], ['', '42']]),
        ])
        manifest = json.loads(self.manifest.read_text())
        manifest['sheets'].extend([
            {'name': 'scene.prod', 'path': 'missing-prod.tsv'},
            {'name': 'skip.test', 'path': 'missing-ignore.tsv'},
            {'name': 'unused', 'path': 'missing-other.tsv'},
        ])
        self.manifest.write_text(json.dumps(manifest))

        tables, constants = self._load(
            script_sheet='^scene$', constant_sheet='^constant$', ignore_sheet='^skip')

        self.assertEqual([('scene', [['開始', '本文']])], tables)
        self.assertEqual({'answer': 42}, constants)

    def test_不正なmanifestと重複宣言は読み込み前に拒否する(self):
        invalid_manifests = [
            [], {}, {'sheets': [], 'options': {}}, {'sheets': {}},
            {'sheets': [{'name': '', 'path': 'x.tsv'}]},
            {'sheets': [{'name': 'story', 'path': None}]},
            {'sheets': [{'name': 'story', 'path': 'x.tsv', 'extra': True}]},
            {'sheets': [
                {'name': 'story', 'path': 'x.tsv'},
                {'name': 'story', 'path': 'y.tsv'},
            ]},
        ]
        for value in invalid_manifests:
            with self.subTest(value=value):
                self.manifest.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    self._load()

    def test_閉じていない引用は元ファイルと開始行を表示する(self):
        self._write_tables([('story', [['開始', '本文']])])
        path = self.root / '0.tsv'
        with path.open('a', encoding='utf-8') as target:
            target.write('次へ\t"閉じていない\n引用')

        with self.assertRaises(ValueError) as caught:
            self._load()

        self.assertIn(str(path), str(caught.exception))
        self.assertIn('story', str(caught.exception))
        self.assertIn('!2行目', str(caught.exception))

    def test_loader_factoryはparamsを結合して登録する(self):
        with patch.dict(hub.scenario_loader_factory_map, {}, clear=True):
            tsv.load_plugin({'manifest': str(self.manifest), 'ignore_sheet': '^skip'})
            loader = hub.create_scenario_loader('tsv', {'script_sheet': '^story'})

        self.assertIsInstance(loader, tsv.TsvPlugin_Loader)
        self.assertEqual(loader.params, {
            'manifest': str(self.manifest), 'ignore_sheet': '^skip', 'script_sheet': '^story',
        })

    def _prepare_builder(self):
        for patcher in (
            patch.multiple(
                hub, builder_list=[], runtime_list=[], method_cache={},
                interface_factory_map={}, scenario_loader_factory_map={}),
            patch.multiple(
                commands, catalog=[], catalog_map={}, object_catalog=[], object_catalog_map={}),
            patch.multiple(expression, **{
                name: getattr(expression, name) for name in (
                    'EXPRESSION_VERSION', 'TRUE_VALUE', 'FALSE_VALUE', 'NONE_VALUE')
            }),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        params = {'reset_keyword': '!reset', 'timezone': 'Asia/Tokyo', 'alt_text': 'alt'}
        common_commands.setup(params)
        default_commands.inner_load_plugin(params)
        quick_reply.load_plugin({
            'command': ['＞'], 'default_reply': '次へ',
            'please_select_quick_reply_label': '##please',
        })
        with patch.object(cloud_backend, 'create_object_store', return_value=Mock()):
            return _load_module('tests._tsv_scenario', 'scenario.py')

    def test_物理行を付けてもbuildの生成labelと論理順は変えない(self):
        module = self._prepare_builder()
        self._write_tables([
            ('story', [['開始', '一行目\n二行目'], ['', '＞'], ['', '選択後']]),
            ('story.test', [[], ['次へ', '環境別']]),
        ])
        tables, constants = self._load()
        plain_tables = [(name, [list(row) for row in rows]) for name, rows in tables]
        builds = [
            module.ScenarioBuilder.build_from_tables(values, constants, version=3)
            for values in (tables, plain_tables)
        ]

        def compiled(built):
            return [
                (str(condition.value), [(line.msg, line.options, line.children) for line in lines])
                for condition, lines in built.scenes['story/'].base_region.blocks
            ]

        self.assertEqual(builds[0].startup_scene_title, builds[1].startup_scene_title)
        self.assertEqual(compiled(builds[0]), compiled(builds[1]))

    def test_buildエラーは環境別ファイルの物理行を指す(self):
        module = self._prepare_builder()
        self._write_tables([
            ('story', [['開始', '本文']]),
            ('story.test', [[], ['次へ', '一行目\n不正 }']]),
        ])
        tables, constants = self._load()

        with self.assertRaises(module.ScenarioSyntaxError) as caught:
            module.ScenarioBuilder.build_from_tables(tables, constants, version=3)

        message = str(caught.exception)
        self.assertIn('story.test', message)
        self.assertIn(str(self.root / '1.tsv'), message)
        self.assertIn('!2行目', message)


if __name__ == '__main__':
    unittest.main()
