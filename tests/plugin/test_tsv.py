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
from plugin import tsv, tsv_values
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

    def _write_sync_tables(self, named_values):
        entries = []
        baseline_directory = self.root / '.sheets-sync'
        baseline_directory.mkdir(exist_ok=True)
        for sheet_id, (name, values) in enumerate(named_values):
            filename = f'sheet-{sheet_id}.tsv'
            tsv_values.write_tsv(self.root / filename, values)
            entries.append({'sheet_id': sheet_id, 'name': name, 'path': filename})
            (baseline_directory / f'base-{sheet_id}.json').write_text(json.dumps({
                'sha256': tsv_values.content_hash(values), 'values': values,
            }), encoding='utf-8')
        self.manifest.write_text(json.dumps({'sheets': [
            {'name': entry['name'], 'path': entry['path']} for entry in entries
        ]}), encoding='utf-8')
        (self.root / 'sheets-sync.json').write_text(json.dumps({
            'schema_version': 1, 'spreadsheet_id': 'artificial', 'sheets': entries,
        }), encoding='utf-8')

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

    def test_式評価は既定で無効(self):
        rows = [['開始', '=A2&"さん"'], ['次へ', '=SUM(A1:A2)']]
        self._write_tables([('story', rows)])
        self.assertEqual([('story', rows)], self._load()[0])

    def test_同期コピーの未変更文字列は式に見えても参照時も再解釈しない(self):
        self._write_sync_tables([('story', [
            [{'stringValue': '開始'}, {'stringValue': '一行目\n二行目'}],
            [{'stringValue': '次へ'}, {'stringValue': '=B2'}],
            [{'stringValue': '連結'}, {'formulaValue': '=B2&"!"'}],
            [{'stringValue': '空白'}, {'stringValue': '  =B2'}],
            [{'stringValue': '空白参照'}, {'formulaValue': '=B4&"?"'}],
        ])])
        rows = self._load(evaluate_formula=True)[0][0][1]
        self.assertEqual(['=B2', '=B2!', '  =B2', '  =B2?'],
                         [row[1] for row in rows[1:]])
        self.assertEqual(2, rows[1].source_position[1])

    def test_補助シートのliteralも遅延判定し必要なbaselineだけ一度読む(self):
        self._write_sync_tables([
            ('story', [[{'stringValue': '開始'}, {'formulaValue': "='_helper'!A1&\"!\""}],
                       [{'stringValue': '次へ'}, {'formulaValue': "='_helper'!A1&\"?\""}]]),
            ('_helper', [[{'stringValue': '=A1'}]]),
            ('story.prod', [[{'stringValue': '=SUM(A1:A2)'}]]),
        ])
        (self.root / 'sheet-2.tsv').unlink()
        (self.root / '.sheets-sync/base-2.json').write_text('{', encoding='utf-8')
        original_open = Path.open
        opened = []

        def open_file(path, *args, **kwargs):
            opened.append(path.name)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', open_file):
            tables, constants = self._load(evaluate_formula=True)
        self.assertEqual([('story', [['開始', '=A1!'], ['次へ', '=A1?']])], tables)
        self.assertEqual({}, constants)
        for name in ('sheet-0.tsv', 'sheet-1.tsv', 'base-0.json', 'base-1.json'):
            self.assertEqual(1, opened.count(name))
        self.assertNotIn('sheet-2.tsv', opened)
        self.assertNotIn('base-2.json', opened)

    def test_同じコピーを再読込すると編集した文字列は式になり戻せばliteralになる(self):
        self._write_sync_tables([('story', [[{'stringValue': '花子'}, {'stringValue': '=A1'}]])])
        loader = tsv.TsvPlugin_Loader({'manifest': str(self.manifest), 'evaluate_formula': True})
        with patch.dict(sys.modules, {'settings': self.settings}):
            self.assertEqual('=A1', loader.load_scenario()[0][0][1][0][1])
            for value, expected in (('=A1&"さん"', '花子さん'), ('=A1', '=A1')):
                with (self.root / 'sheet-0.tsv').open('w', encoding='utf-8', newline='') as target:
                    csv.writer(target, dialect='excel-tab').writerow(['花子', value])
                self.assertEqual(expected, loader.load_scenario()[0][0][1][0][1])

    def test_同期情報や必要なbaselineの不正を通常TSVへfallbackしない(self):
        self._write_sync_tables([('story', [[{'stringValue': '開始'}, {'stringValue': '=B1'}]])])
        metadata = self.root / 'sheets-sync.json'
        baseline = self.root / '.sheets-sync/base-0.json'
        invalid_hash = json.loads(baseline.read_text())
        invalid_hash['sha256'] = 'invalid'
        for path, contents in ((metadata, None), (metadata, '{'), (baseline, None),
                               (baseline, json.dumps(invalid_hash))):
            with self.subTest(file=path.name, missing=contents is None):
                original = path.read_bytes()
                if contents is None:
                    path.unlink()
                else:
                    path.write_text(contents, encoding='utf-8')
                try:
                    for evaluate_formula in (False, True):
                        with self.subTest(evaluate_formula=evaluate_formula):
                            with self.assertRaises((ValueError, OSError)) as caught:
                                self._load(evaluate_formula=evaluate_formula)
                            self.assertIn(str(path), str(caught.exception))
                finally:
                    path.write_bytes(original)

    def test_同期コピーの数値と真偽値はpush候補と同じ表記で読める(self):
        base = [[{'stringValue': '開始'}, {'boolValue': True}, {'numberValue': 1}],
                [{'stringValue': '参照'}, {'formulaValue': '=C1&"回"'}]]
        self._write_sync_tables([('story', base)])
        raw = [['開始', 'TRUE', '+01'], ['参照', '=C1&"回"']]
        with (self.root / 'sheet-0.tsv').open('w', encoding='utf-8', newline='') as target:
            csv.writer(target, dialect='excel-tab').writerows(raw)
        candidate = tsv_values.local_values(raw, base)

        rows = self._load(evaluate_formula=False)[0][0][1]
        self.assertEqual([
            [tsv_values.to_text(cell) for cell in row] for row in candidate
        ], rows)
        self.assertEqual(['開始', 'True', '1'], rows[0])
        evaluated = self._load(evaluate_formula=True)[0][0][1]
        self.assertEqual(['開始', 'True', '1'], evaluated[0])
        self.assertEqual('1回', evaluated[1][1])

    def test_同期コピーの式追加と解除もpush候補と揃える(self):
        self._write_sync_tables([('story', [[
            {'stringValue': '花子'}, {'formulaValue': '=A1'}, {'stringValue': '=B1'},
        ]])])
        with (self.root / 'sheet-0.tsv').open('w', encoding='utf-8', newline='') as target:
            csv.writer(target, dialect='excel-tab').writerow(['花子', '普通の文字列', '  =A1&"さん"'])

        self.assertEqual(
            ['花子', '普通の文字列', '=A1&"さん"'], self._load()[0][0][1][0])
        self.assertEqual(
            ['花子', '普通の文字列', '花子さん'], self._load(evaluate_formula=True)[0][0][1][0])

    def test_同期コピーの型変換で空行と末尾行と物理位置を削らない(self):
        self._write_sync_tables([('story', [
            [{'stringValue': '一行目\n二行目'}, {'numberValue': 1}], [],
            [{'stringValue': '次へ'}, {'stringValue': '=SUM(A1:A2)'}],
            [{'stringValue': '削除する行'}],
        ])])
        raw = [['一行目\n二行目', '+01', ''], [], ['次へ', '=SUM(A1:A2)', ''], [], ['', '', '']]
        with (self.root / 'sheet-0.tsv').open('w', encoding='utf-8', newline='') as target:
            csv.writer(target, dialect='excel-tab').writerows(raw)

        for evaluate_formula in (False, True):
            with self.subTest(evaluate_formula=evaluate_formula):
                rows = self._load(evaluate_formula=evaluate_formula)[0][0][1]
                self.assertEqual([3, 0, 3, 0, 3], [len(row) for row in rows])
                self.assertEqual([0, 2, 3, 4, 5], [row.source_position[1] for row in rows])
                self.assertTrue(all(isinstance(row, SourceRow) for row in rows))
                self.assertEqual('1', rows[0][1])
                self.assertEqual('=SUM(A1:A2)', rows[2][1])
                self.assertEqual([], rows[3])

    def test_同期コピーの数値と真偽値を通常の文字列へ変更できる(self):
        for cell in ({'numberValue': 1}, {'boolValue': True}):
            self._write_sync_tables([('story', [[{'stringValue': '開始'}, cell]])])
            with (self.root / 'sheet-0.tsv').open('w', encoding='utf-8', newline='') as target:
                csv.writer(target, dialect='excel-tab').writerow(['開始', '解釈できない値'])
            for evaluate_formula in (False, True):
                with self.subTest(cell=cell, evaluate_formula=evaluate_formula):
                    self.assertEqual([('story', [['開始', '解釈できない値']])],
                                     self._load(evaluate_formula=evaluate_formula)[0])

    def test_同期した数値と真偽値の定数表で説明行追加と削除と移動ができる(self):
        self._write_sync_tables([('$constants', [
            [{'stringValue': 'items'}, {'stringValue': 'list'}],
            [{}, {'numberValue': 1}], [{}, {'numberValue': 2}],
            [{'stringValue': 'enabled'}, {'stringValue': 'value'}],
            [{}, {'boolValue': True}],
        ])])
        rows = [['items', 'list'], ['', '1'], ['', '2'], ['enabled', 'value'], ['', 'True']]
        for name, edited, expected in (
                ('説明行追加', [[';説明', '台本の定数']] + rows,
                 {'items': [1, 2], 'enabled': True}),
                ('要素削除', rows[:1] + rows[2:], {'items': [2], 'enabled': True}),
                ('ブロック移動', rows[3:] + rows[:3], {'enabled': True, 'items': [1, 2]})):
            with (self.root / 'sheet-0.tsv').open('w', encoding='utf-8', newline='') as target:
                csv.writer(target, dialect='excel-tab').writerows(edited)
            for evaluate_formula in (False, True):
                with self.subTest(edit=name, evaluate_formula=evaluate_formula):
                    self.assertEqual(([], expected), self._load(evaluate_formula=evaluate_formula))

    def test_通常TSVは数値表記と真偽値表記と27列目を変えない(self):
        raw = [['開始', 'TRUE', '+01'] + [''] * 23 + ['27列目']]
        self._write_tables([('story', raw)])
        for evaluate_formula in (False, True):
            with self.subTest(evaluate_formula=evaluate_formula):
                self.assertEqual([('story', raw)], self._load(evaluate_formula=evaluate_formula)[0])

    def test_同期コピーの範囲外の空列paddingは読めるが値は拒否する(self):
        self._write_sync_tables([('story', [[{'stringValue': '開始'}, {'stringValue': '本文'}]])])
        path = self.root / 'sheet-0.tsv'
        for extra in ('', '範囲外の値'):
            with path.open('w', encoding='utf-8', newline='') as target:
                csv.writer(target, dialect='excel-tab').writerow(['開始', '本文'] + [''] * 24 + [extra] * 100)
            original = path.read_bytes()
            for evaluate_formula in (False, True):
                with self.subTest(extra=extra, evaluate_formula=evaluate_formula):
                    if extra:
                        with self.assertRaisesRegex(ValueError, 'AA1'):
                            self._load(evaluate_formula=evaluate_formula)
                    else:
                        rows = self._load(evaluate_formula=evaluate_formula)[0][0][1]
                        self.assertEqual([['開始', '本文'] + [''] * 24], rows)
                        self.assertEqual(0, rows[0].source_position[1])
            self.assertEqual(original, path.read_bytes())

    def test_セル参照と連結は物理シートのCSV行を使う(self):
        self._write_tables([
            ('story', [
                ['開始', '一行目\n二行目'],
                ['次へ', '旅人'],
                ['', '= $b$2 & "さん、" & B1 & "。"'],
                ['', '= "引用: ""はい"" & 終了"'],
                ['', '=Z999'],
            ]),
            ('story.test', [['環境別', '=story!B2&"!"'], ['', '=A1']]),
        ])
        rows = self._load(evaluate_formula=True)[0][0][1]
        self.assertEqual('旅人さん、一行目\n二行目。', rows[2][1])
        self.assertEqual('引用: "はい" & 終了', rows[3][1])
        self.assertEqual('', rows[4][1])
        self.assertEqual('旅人!', rows[5][1])
        self.assertEqual('環境別', rows[6][1])
        self.assertEqual(3, rows[2].source_position[1])

    def test_参照された補助シートだけを読み定数も解決する(self):
        self._write_tables([
            ('story', [['開始', "='_作家''資料.prod'!B1"]]),
            ("_作家'資料.prod", [['未使用', "='_helper'!A1&\"さん\"", '=SUM(A1:A2)']]),
            ('_helper', [['旅人']]),
            ('$constants', [['title', 'value'], ['', '=story!B1']]),
        ])
        manifest = json.loads(self.manifest.read_text())
        manifest['sheets'].append({'name': 'story.prod', 'path': 'missing.tsv'})
        self.manifest.write_text(json.dumps(manifest))
        original_open = Path.open
        opened = []

        def open_file(path, *args, **kwargs):
            opened.append(path.name)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', open_file):
            tables, constants = self._load(evaluate_formula=True)

        self.assertEqual([('story', [['開始', '旅人さん']])], tables)
        self.assertEqual({'title': '旅人さん'}, constants)
        for name in ('0.tsv', '1.tsv', '2.tsv', '3.tsv'):
            self.assertEqual(1, opened.count(name))
        self.assertNotIn('missing.tsv', opened)

    def test_文字列の型を推測せず式の結果を再解釈しない(self):
        self._write_tables([('story', [
            ['001', 'TRUE', '1/9', '2026-01-09'],
            ['=A1&B1&C1&D1', '="=SUM(A1:A2)"'],
            ['=B2', '=A3'],
        ])])
        rows = self._load(evaluate_formula=True)[0][0][1]
        self.assertEqual('001TRUE1/92026-01-09', rows[1][0])
        self.assertEqual(['=SUM(A1:A2)', '=SUM(A1:A2)'], rows[2])

    def test_IMAGEは評価せず参照でコピーできる(self):
        image = '=IMAGE("https://example.invalid/image.png")'
        self._write_tables([('story', [['開始', image], ['次へ', '=B1']])])
        rows = self._load(evaluate_formula=True)[0][0][1]
        self.assertEqual(image, rows[0][1])
        self.assertEqual(image, rows[1][1])

    def test_不正な式はセル座標と元ファイルの物理行を表示する(self):
        for formula in ('=', '=A1&', '=A1+B1', '=A1:B2', '=SUM(A1:A2)', '="未完了', '=A0'):
            with self.subTest(formula=formula):
                self._write_tables([('story', [['開始', '一行目\n二行目'], ['次へ', formula]])])
                with self.assertRaises(ValueError) as caught:
                    self._load(evaluate_formula=True)
                message = str(caught.exception)
                self.assertIn('story!B2', message)
                self.assertIn(f'{self.root / "0.tsv"}:3行目', message)
                self.assertIn('対応する式', message)

    def test_存在しないシートと循環参照を拒否する(self):
        for formula, expected in (
                ('=missing!A1', 'manifestにありません'),
                ('=A1', '循環参照'),
                ('=B1', '循環参照')):
            with self.subTest(formula=formula):
                self._write_tables([('story', [[formula, "='_helper'!A1"]]),
                                    ('_helper', [['=story!A1']])])
                with self.assertRaisesRegex(ValueError, expected):
                    self._load(evaluate_formula=True)

    def test_長い参照連鎖を再帰せず解決し次回は編集を読み直す(self):
        rows = [[f'=A{index + 2}'] for index in range(3000)] + [['終端']]
        self._write_tables([('story', rows)])
        loaded = self._load(evaluate_formula=True)[0][0][1]
        self.assertTrue(all(row == ['終端'] for row in loaded))
        rows[-1] = ['更新']
        self._write_tables([('story', rows)])
        self.assertEqual('更新', self._load(evaluate_formula=True)[0][0][1][0][0])

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
