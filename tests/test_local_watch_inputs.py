"""監視対象の再解決と、編集途中の設定が外部処理へ進まないことを確認する。"""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from tools.local_scenario import execute, inspect_watch_inputs, parse_args
from tools.local_support import LocalInputError


class LocalWatchInputsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.settings = self.root / 'settings.yaml'
        self.config = {'*': {
            'cloud': {'provider': 'local'},
            'local': {'storage_root': 'store', 'public_base_url': 'http://127.0.0.1:8765/local-media',
                      'assets': {'https://example.invalid/icon.png': 'icon.png'}},
            'plugins': {'line': {}, 'line.image_text': {'frames': {'book': {'font_path': 'font.ttf'}}}},
            'bots': {'bot': {'scenario': {'type': 'tsv', 'params': {'manifest': 'manifest.json'}}}},
        }}
        (self.root / 'manifest.json').write_text(json.dumps({'sheets': [
            {'name': 'story', 'path': 'story.tsv'},
            {'name': 'story.test', 'path': 'test.tsv'},
            {'name': 'story.prod', 'path': 'prod.tsv'},
        ]}), encoding='utf-8')
        self._save()
        self.args = parse_args(['webchat', '--settings', str(self.settings), '--bot', 'bot', '--watch'])
        patcher = patch.dict(os.environ, {'XSBOT_DEPLOY_ENV': 'test'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _save(self):
        self.settings.write_text(yaml.safe_dump(self.config), encoding='utf-8')

    def test_元の選択入力だけを列挙し未作成assetも監視へ含める(self):
        info = inspect_watch_inputs(self.args)
        self.assertNotIn('error', info)
        self.assertEqual(
            {'settings.yaml', 'manifest.json', 'sheets-sync.json',
             'story.tsv', 'test.tsv', 'icon.png', 'font.ttf'},
            {Path(path).name for path in info['files']})
        self.assertEqual([str(self.root / 'font.ttf')], info['fonts'])
        self.assertFalse((self.root / 'store').exists())
        self.assertFalse((self.root / 'sheets-sync.json').exists())

    def test_式評価時は参照候補の全TSVを本文を読まず監視する(self):
        self.config['*']['plugins']['tsv'] = {'evaluate_formula': True}
        self._save()
        info = inspect_watch_inputs(self.args)
        self.assertNotIn('error', info)
        self.assertIn(str(self.root / 'prod.tsv'), info['files'])
        self.assertIn(str(self.root / 'sheets-sync.json'), info['files'])
        self.assertFalse((self.root / 'prod.tsv').exists())
        self.assertFalse((self.root / 'sheets-sync.json').exists())

    def test_Sheetsの式評価設定を継承したTSV切替でも参照候補を監視する(self):
        self.config['*']['bots']['bot']['scenario'] = {
            'type': 'google_sheets', 'params': {
                'sheet_id': 'unused', 'key_file_json': 'unused.json', 'evaluate_formula': True,
            },
        }
        self.args.tsv = str(self.root / 'manifest.json')
        self._save()

        info = inspect_watch_inputs(self.args)

        self.assertNotIn('error', info)
        self.assertEqual('tsv', info['source_type'])
        self.assertIn(str(self.root / 'prod.tsv'), info['files'])
        self.assertFalse((self.root / 'prod.tsv').exists())
        self.assertFalse((self.root / 'store').exists())

    def test_同期基準の全pathを監視し基準本文は読まない(self):
        entries = [{'name': name, 'path': f'sheet-{index}.tsv', 'sheet_id': index}
                   for index, name in enumerate(('story', 'story.test', 'story.prod'))]
        (self.root / 'manifest.json').write_text(json.dumps({'sheets': [
            {'name': entry['name'], 'path': entry['path']} for entry in entries
        ]}), encoding='utf-8')
        metadata = self.root / 'sheets-sync.json'
        metadata.write_text(json.dumps({
            'schema_version': 1, 'spreadsheet_id': 'synthetic', 'sheets': entries,
        }), encoding='utf-8')
        baseline_directory = self.root / '.sheets-sync'
        baseline_directory.mkdir()
        (baseline_directory / 'base-0.json').write_text('本文は検査しない', encoding='utf-8')
        original_open = Path.open

        def open_without_baseline(path, *args, **kwargs):
            if path.parent == baseline_directory:
                self.fail('監視対象の列挙では基準本文を読みません')
            return original_open(path, *args, **kwargs)

        for evaluate_formula in (False, True):
            with self.subTest(evaluate_formula=evaluate_formula):
                self.config['*']['plugins']['tsv'] = {'evaluate_formula': evaluate_formula}
                self._save()
                with patch.object(Path, 'open', open_without_baseline):
                    info = inspect_watch_inputs(self.args)
                self.assertNotIn('error', info)
                self.assertIn(str(metadata), info['files'])
                self.assertTrue({str(baseline_directory / f'base-{index}.json') for index in range(3)}
                                .issubset(info['files']))
                self.assertEqual(evaluate_formula, str(self.root / 'sheet-2.tsv') in info['files'])
        self.assertFalse((baseline_directory / 'base-2.json').exists())

    def test_新manifestの欠落やJSON不正でも元pathを返して修正を検出できる(self):
        self.config['*']['bots']['bot']['scenario']['params']['manifest'] = 'new.json'
        self._save()
        for contents in (None, '{'):
            if contents is not None:
                (self.root / 'new.json').write_text(contents, encoding='utf-8')
            info = inspect_watch_inputs(self.args)
            self.assertIn('error', info)
            self.assertIn(str(self.root / 'new.json'), info['files'])

    def test_編集中の設定shape不正は例外を漏らさず修正待ちにできる(self):
        original = copy.deepcopy(self.config)
        for key in ('cloud', 'bots', 'plugins', 'local'):
            with self.subTest(key=key):
                self.config = copy.deepcopy(original)
                self.config['*'][key] = None
                self._save()
                info = inspect_watch_inputs(self.args)
                self.assertIn('error', info)
                self.assertIn(str(self.settings), info['files'])

    def test_検査直後のrootやsource変更を再検査して書込や取得前に拒否する(self):
        identity = inspect_watch_inputs(self.args)['identity']
        self.config['*']['local']['storage_root'] = 'changed'
        self._save()
        with patch('tools.local_scenario.run_worker') as worker:
            with self.assertRaisesRegex(LocalInputError, 'storage_root'):
                execute(self.args, serve=False, expected_identity=identity)
            self.assertFalse((self.root / 'changed').exists())
            self.config['*']['local']['storage_root'] = 'store'
            self.config['*']['bots']['bot']['scenario'] = {
                'type': 'google_sheets', 'params': {'sheet_id': 'unused', 'key_file_json': 'unused.json'}}
            self._save()
            with self.assertRaisesRegex(LocalInputError, 'Sheets'):
                execute(self.args, serve=False, expected_identity=identity)
            worker.assert_not_called()
        self.assertFalse((self.root / 'store').exists())

    def test_編集中の不正regexを部分監視とエラーへ変換する(self):
        self.config['*']['bots']['bot']['scenario']['params']['script_sheet'] = '['
        self._save()
        info = inspect_watch_inputs(self.args)
        self.assertIn('error', info)
        self.assertIn(str(self.settings), info['files'])
        self.assertIn(str(self.root / 'manifest.json'), info['files'])


if __name__ == '__main__':
    unittest.main()
