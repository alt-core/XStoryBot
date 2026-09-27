"""実CLIでTSV切替・媒体の書出し・公開範囲・既存siteの保全を確認する。"""

import json
from pathlib import Path
import subprocess
import sys
import unittest

from PIL import Image
from tests import test_local_scenario as fixture
from tools import export_webchat_trial as trial


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'tools' / 'export_webchat_trial.py'


class TrialExportTest(unittest.TestCase):
    def setUp(self):
        self.case = fixture.LocalScenarioCliTest('test_実QuickReplyの再提示と選択を保存し別caseと文字入力を隔離する')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.output = self.case.root / 'site'
        config = self.case.config['*']
        config['cloud'] = {'provider': 'aws'}
        config.pop('local')
        config['constants'] = {'private_unused': 'SYNTHETIC_SECRET_NOT_FOR_EXPORT'}
        bot = config['bots']['bot']
        bot['scenario'] = {'type': 'google_sheets', 'params': {
            'sheet_id': 'must-not-fetch', 'key_file_json': self.case.credential.name,
            'script_sheet': '^trial$', 'constant_sheet': '^\\$trial$',
        }}
        bot['interfaces'] = [
            {'type': 'webchat', 'params': {'start_action': '##line.follow', 'scenario_compatibility_epoch': '1',
                'signing_key': 'SYNTHETIC_KEY_NOT_FOR_EXPORT',
                'liff_apps': {'menu': {'bot': 'bot', 'url': 'https://pages.example.test/menu'}}}},
            {'type': 'liff', 'params': {'ignore_unhandled_action': True}},
        ]
        self.case.rows = [
            ['##line.follow', '開始'], ['', '＞', '左', '右'], ['', '左です'], ['', '/else'], ['', '右です'], ['', '/end'],
            ['##please', '選択してください'], ['', '@show_quick_reply_choices'],
            ['##liff.open', '{{"event":"open"}}'],
        ]
        self.case.manifest_path.write_text(json.dumps({'sheets': [
            {'name': 'trial', 'path': self.case.tsv_path.name},
            {'name': '本編', 'path': 'private.tsv'},
            {'name': '$trial', 'path': 'constants.tsv'},
        ]}, ensure_ascii=False))
        (self.case.root / 'private.tsv').write_text('開始\tPRIVATE_STORY_NOT_FOR_EXPORT\n')
        (self.case.root / 'constants.tsv').write_text('unused\tvalue\n\tSYNTHETIC_UNUSED_SHEET_VALUE\n')
        self.case._write_rows()
        self.case._write_settings()

    def run_export(self, expected=0, extra=()):
        completed = subprocess.run([
            sys.executable, str(CLI), '--settings', str(self.case.settings_path), '--bot', 'bot',
            '--tsv', str(self.case.manifest_path), '--output', str(self.output), '--timeout', '10', *extra,
        ], env=self.case.environment, cwd=self.case.root, capture_output=True, text=True, timeout=25)
        self.assertNotIn(fixture.GUARD_MARKER, completed.stdout + completed.stderr)
        self.assertEqual(expected, completed.returncode, completed.stdout + '\n' + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(expected == 0, result['ok'])
        for warning in result.get('warnings', []):
            self.assertIn(f"警告 [{warning['code']}] {warning['location']}", completed.stderr)
        return result

    def test_Sheets設定のTSV上書きで資格情報やクラウドへ接続せず公開する(self):
        result = self.run_export()
        self.assertEqual([], result['warnings'])
        self.assertEqual(['trial', '$trial'], result['sheets'])
        content = b'\n'.join((self.output / name).read_bytes() for name in result['files'])
        for forbidden in (b'SYNTHETIC_SECRET_NOT_FOR_EXPORT', b'SYNTHETIC_KEY_NOT_FOR_EXPORT', b'PRIVATE_STORY_NOT_FOR_EXPORT', b'SYNTHETIC_UNUSED_SHEET_VALUE', str(self.case.root).encode()):
            self.assertNotIn(forbidden, content)
        program = json.loads((self.output / f"assets/{result['revision']}/scenario.json").read_text())
        self.assertEqual('bot', program['liff_apps']['menu']['bot'])
        html = (self.output / 'index.html').read_text()
        self.assertIn('data-webchat-trial="1"', html)
        app = (self.output / f"assets/{result['revision']}/app.js").read_text()
        self.assertIn('createTrialClient({ bot, programUrl:', app)
        self.assertIn('xstorybot-webchat-richmenu:trial:', app)

    def test_体験版も題名とメモリー保存とテーマを同じ指定で配布する(self):
        theme = self.case.root / 'theme'
        theme.mkdir()
        (theme / 'style.css').write_text(':root {--accent: #123456;}')
        result = self.run_export(extra=['--title', '短い体験', '--storage', 'memory', '--theme', str(theme)])
        html = (self.output / 'index.html').read_text()
        self.assertIn('<title>短い体験</title>', html)
        self.assertIn('<h1 id="chat-title">短い体験</h1>', html)
        self.assertIn('data-webchat-storage="memory"', html)
        root = self.output / 'assets' / result['revision']
        self.assertEqual((theme / 'style.css').read_bytes(), (root / 'theme/style.css').read_bytes())
        self.assertIn("programUrl: new URL('./scenario.json', import.meta.url), storage", (root / 'app.js').read_text())

    def test_参照媒体だけを同梱し再生成で旧assetと利用者fileを保つ(self):
        image = self.case.root / 'picture.png'
        Image.new('RGB', (12, 12), '#6699aa').save(image)
        self.case.config['*']['local'] = {'assets': {'https://media.example.test/picture.png': image.name}}
        self.case.rows = [['##line.follow', '@image', 'https://media.example.test/picture.png'], ['', '画像です']]
        self.case._write_rows()
        self.case._write_settings()
        first = self.run_export()
        media = [name for name in first['files'] if name.startswith('media/')]
        self.assertTrue(media)
        data = json.loads((self.output / f"assets/{first['revision']}/scenario.json").read_text())
        self.assertNotIn('127.0.0.1', json.dumps(data))
        self.assertIn('../../media/', json.dumps(data))
        own = self.output / 'site-note.txt'
        own.write_text('利用者のファイル')
        self.case.rows[-1][1] = '修正後の台詞'
        self.case._write_rows()
        second = self.run_export()
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertTrue((self.output / f"assets/{first['revision']}/scenario.json").exists())
        self.assertEqual('利用者のファイル', own.read_text())

    def test_未対応の台本は元の行で診断し公開済み入口を変えない(self):
        self.run_export()
        before = (self.output / 'index.html').read_bytes()
        self.case.rows += [['未使用の枝', '@set', '$flag', 'true']]
        self.case._write_rows()
        result = self.run_export(1)
        self.assertIn('trial!', result['error'])
        self.assertIn('@set', result['error'])
        self.assertEqual(before, (self.output / 'index.html').read_bytes())

    def test_別BotのLIFFを拒否し無関係なsiteも上書きしない(self):
        self.case.config['*']['bots']['bot']['interfaces'][0]['params']['liff_apps']['menu']['bot'] = 'other'
        self.case._write_settings()
        self.assertIn('同じBot', self.run_export(1)['error'])
        self.case.config['*']['bots']['bot']['interfaces'][0]['params']['liff_apps']['menu']['bot'] = 'bot'
        self.case._write_settings()
        self.output.mkdir()
        (self.output / 'index.html').write_text('既存サイト')
        self.run_export(2)
        self.assertEqual('既存サイト', (self.output / 'index.html').read_text())


    def test_設定継承とローカルURLを警告し明示設定なら継承警告を消す(self):
        config = self.case.config['*']
        params = config['bots']['bot']['interfaces'][0]['params']
        config['plugins']['webchat'] = {'enabled': True, 'scenario_compatibility_epoch': 'shared-v7'}
        del params['scenario_compatibility_epoch']
        params['liff_apps']['menu']['url'] = 'http://127.0.0.1:8767/liff/index.html'
        self.case._write_settings()
        result = self.run_export()
        self.assertEqual({'api-webchat-enabled', 'inherited-compatibility-epoch', 'local-url'},
                         {warning['code'] for warning in result['warnings']})
        program = json.loads((self.output / f"assets/{result['revision']}/scenario.json").read_text())
        self.assertEqual('shared-v7', program['epoch'])
        self.assertEqual(params['liff_apps']['menu']['url'], program['liff_apps']['menu']['url'])

        params.update(enabled=False, scenario_compatibility_epoch='trial-v1')
        params['liff_apps']['menu']['url'] = 'https://pages.example.test/menu'
        self.case._write_settings()
        updated = self.run_export()
        self.assertEqual([], updated['warnings'])
        program = json.loads((self.output / f"assets/{updated['revision']}/scenario.json").read_text())
        self.assertEqual('trial-v1', program['epoch'])


class TrialWarningsTest(unittest.TestCase):
    def test_API有効判定は通常版と同じ表記を受け付ける(self):
        config = {'options': {}, 'plugins': {}, 'bots': {'trial': {'interfaces': [
            {'type': 'webchat', 'params': {'scenario_compatibility_epoch': 'trial-v1'}},
        ]}}}
        own = config['bots']['trial']['interfaces'][0]['params']
        for enabled, expected in ((True, True), ('true', True), ('1', True),
                                  (False, False), ('false', False), ('0', False)):
            with self.subTest(enabled=enabled):
                own['enabled'] = enabled
                warnings = trial.configuration_warnings(config, 'trial')
                self.assertEqual(expected, any(item['code'] == 'api-webchat-enabled' for item in warnings))

    def test_ローカルURL警告は場所だけを示し本文や正規表現を誤検出しない(self):
        program = {
            'liff_apps': {'menu': {'url': 'http://127.0.0.1:8767/page?token=SYNTHETIC_QUERY'}},
            'richmenu': {'areas': [{'action': {'type': 'uri', 'href': 'http://localhost:8080/help'}}]},
            'menu_actions': [{'uri': 'http://[::1]:8080/help'}],
            'messages': [{'text': 'http://127.0.0.1:8767/example', 'image_url': '../../media/a.png'},
                         {'actions': [{'href': 'https://pages.example.test/help'}]}],
            'test': {'kind': 'regex', 'value': r'http://127\.0\.0\.1/\w'},
        }
        warnings = trial.local_url_warnings(program)
        self.assertEqual([
            'scenario.liff_apps.menu.url',
            'scenario.richmenu.areas[0].action.href',
            'scenario.menu_actions[0].uri',
        ], [item['location'] for item in warnings])
        self.assertNotIn('SYNTHETIC_QUERY', json.dumps(warnings))


if __name__ == '__main__':
    unittest.main()
