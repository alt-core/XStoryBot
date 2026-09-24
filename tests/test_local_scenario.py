"""設定は直接検証し、保存・会話・起動境界は実CLIで確認する。"""

import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from plugin.scenario_table import SheetSelector
from plugin.tsv import TsvPlugin_Loader
from tools.local_scenario import load_config, save_settings
from tools.local_support import LocalInputError, read_json, validate_suite


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = PROJECT_ROOT / 'tools' / 'local_scenario.py'
GUARD_MARKER = 'LOCAL_TEST_FORBIDDEN'


class LocalScenarioCliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings_path = self.root / 'project.yaml'
        self.tsv_path = self.root / 'story.tsv'
        self.manifest_path = self.root / 'manifest.json'
        self.suite_path = self.root / 'suite.json'
        self.session = self.root / 'session'
        self.credential = self.root / 'credentials-must-not-be-read.json'
        self.credential.write_text('{"synthetic": true}', encoding='utf-8')
        self.guard_directory = self.root / 'startup-guard'
        self.guard_directory.mkdir()
        # 全workerに継承する監査。エンジンやproviderの実装は差し替えない。
        (self.guard_directory / 'sitecustomize.py').write_text('''
import os
import sys

def reject_external(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr'):
        raise AssertionError('LOCAL_TEST_FORBIDDEN network')
    if event == 'import' and any(
            args[0] == name or args[0].startswith(name + '.')
            for name in ('google.auth', 'google.oauth2', 'boto3', 'botocore', 'cloud_backend.gcp', 'cloud_backend.aws')):
        raise AssertionError('LOCAL_TEST_FORBIDDEN cloud import: ' + args[0])
    if event == 'open' and args[0] == os.environ.get('LOCAL_TEST_CREDENTIAL_FILE'):
        raise AssertionError('LOCAL_TEST_FORBIDDEN credential file')

sys.addaudithook(reject_external)
''', encoding='utf-8')
        self.environment = {
            **os.environ,
            'PYTHONPATH': str(self.guard_directory),
            'PYTHONDONTWRITEBYTECODE': '1',
            'XSBOT_DEPLOY_ENV': 'test',
            'XSBOT_CLOUD_PROVIDER': 'aws',
            'XSBOT_SETTINGS_FILE': str(self.credential),
            'XSBOT_AWS_RUNTIME_SECRETS_PARAMETER': '/must-not-fetch/local-test',
            'GOOGLE_APPLICATION_CREDENTIALS': str(self.credential),
            'LOCAL_TEST_CREDENTIAL_FILE': str(self.credential),
        }
        self.config = {
            '*': {
                'cloud': {'provider': 'local'},
                'local': {
                    'storage_root': 'storage',
                    'public_base_url': 'http://127.0.0.1:8765/local-media',
                },
                'options': {'scenario_version': 3, 'timezone': 'Asia/Tokyo', 'reset_keyword': '!reset'},
                'plugins': {
                    'line': {'alt_text': '選択してください'},
                    'line.quick_reply': {
                        'command': ['＞'], 'default_reply': '次へ',
                        'please_select_quick_reply_label': '##please',
                        'ignore_pattern': '^失敗$',
                    },
                },
                'bots': {
                    'bot': {
                        'state_namespace': 'story-player',
                        'scenario': {'type': 'tsv', 'params': {'manifest': 'manifest.json'}},
                    },
                },
            },
        }
        self._write_settings()
        self.manifest_path.write_text(json.dumps({
            'sheets': [{'name': 'story', 'path': 'story.tsv'}],
        }), encoding='utf-8')
        self.rows = [
            ['##line.follow', 'どちらにしますか'],
            ['', '＞', '左', '右'],
            ['', '左を選んだ'], ['', '@set', '$choice', '"left"'],
            ['', '/else'], ['', '右を選んだ'], ['', '@set', '$choice', '"right"'],
            ['', '/end'], ['', '選択済み'],
            ['##please', '候補から選んでください'], ['', '@show_quick_reply_choices'],
            ['a@@b', '文字列をそのまま受信'],
            ['失敗', '@raise', '人工的な実行エラー'],
        ]
        self._write_rows()

    def _write_settings(self):
        self.settings_path.write_text(
            yaml.safe_dump(self.config, allow_unicode=True, sort_keys=False), encoding='utf-8')

    def _write_rows(self):
        with self.tsv_path.open('w', encoding='utf-8', newline='') as target:
            csv.writer(target, dialect='excel-tab', lineterminator='\n').writerows(self.rows)

    def _load_config(self, tsv=None, command='verify'):
        args = SimpleNamespace(
            command=command, settings=str(self.settings_path), bot='bot', tsv=tsv)
        with patch.dict(os.environ, self.environment, clear=True):
            return load_config(args)

    def _run(self, command='verify', cases=None, extra=(), expected_code=0):
        arguments = [
            sys.executable, str(CLI), command,
            '--settings', self.settings_path.name, '--bot', 'bot', '--timeout', '10',
        ]
        if command == 'verify':
            self.suite_path.write_text(json.dumps({
                'schema_version': 1, 'cases': cases,
            }, ensure_ascii=False), encoding='utf-8')
            arguments.extend(['--suite', self.suite_path.name])
        completed = subprocess.run(
            [*arguments, *extra], cwd=self.root, env=self.environment,
            capture_output=True, text=True, timeout=25,
        )
        self.assertNotIn(GUARD_MARKER, completed.stdout + completed.stderr)
        self.assertEqual(
            expected_code, completed.returncode,
            f'stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}')
        try:
            result = json.loads(completed.stdout)
        except ValueError:
            self.fail(f'stdoutがJSON一件ではありません:\n{completed.stdout}\n{completed.stderr}')
        self.assertEqual(expected_code, result['exit_code'])
        self.assertEqual(expected_code == 0, result['ok'])
        return result

    @staticmethod
    def _case(name, steps):
        return {'name': name, 'seed': 0, 'steps': steps}

    @staticmethod
    def _ask():
        return {
            'input': {'type': 'start'},
            'expect': {
                'texts': ['どちらにしますか'],
                'choices': [
                    {'type': 'postback', 'label': '左'},
                    {'type': 'postback', 'label': '右'},
                ],
            },
        }

    @staticmethod
    def _right(text='右を選んだ'):
        return {
            'input': {'type': 'choice', 'index': 1},
            'expect': {
                'texts': [text, '選択済み'],
                'flags': {'$choice': 'right'},
                'absent_flags': ['$$line.quick_reply'],
            },
        }

    def test_実QuickReplyの再提示と選択を保存し別caseと文字入力を隔離する(self):
        # 同じ実起動で、TSV切替・秘密除去・親環境からの隔離も確認する。
        self.config['*']['bots']['bot']['scenario'] = {
            'type': 'google_sheets',
            'params': {'sheet_id': 'must-not-request', 'key_file_json': self.credential.name},
        }
        self.config['*']['bots']['bot']['interfaces'] = [
            {'type': 'line', 'params': {
                'line_access_token': 'SYNTHETIC_PRIVATE_TOKEN',
                'sender_icon_urls': {'案内人': 'https://example.invalid/icon.png'},
            }},
        ]
        self.config['*']['plugins']['google_sheets'] = {
            'service_account': 'SYNTHETIC_UNUSED_GOOGLE_CREDENTIAL',
        }
        self.config['*']['plugins']['tsv'] = {'evaluate_formula': True}
        self.manifest_path.write_text(json.dumps({'sheets': [
            {'name': 'story', 'path': 'story.tsv'},
            {'name': '_shared', 'path': 'shared.tsv'},
        ]}), encoding='utf-8')
        (self.root / 'shared.tsv').write_text('どちら\n左\n右\n', encoding='utf-8')
        self.rows[0][1] = '=_shared!A1&"にしますか"'
        self.rows[1][2:] = ['=_shared!A2', '=_shared!A3']
        from tests.test_richmenu_spec import menu_settings
        self.config['*']['bots']['bot'].update(menu_settings())
        self.config['*']['constants'] = {'menu_url': 'https://pages.example.test/'}
        self.rows[0][0] = ''
        self.rows.insert(0, ['##line.follow', '@richmenu', 'main'])
        self._write_rows()
        self._write_settings()
        result = self._run(cases=[
            self._case('選択する', [
                self._ask(),
                {'input': {'type': 'text', 'text': 'わからない'},
                 'expect': {'texts': ['候補から選んでください']}},
                self._right(),
            ]),
            self._case('白紙から文字入力', [
                {'input': {'type': 'text', 'text': 'a@@b'},
                 'expect': {'texts': ['文字列をそのまま受信'],
                            'absent_flags': ['$choice', '$$line.quick_reply']}},
            ]),
        ], extra=['--tsv', self.manifest_path.name])

        self.assertEqual('tsv', result['source']['type'])
        self.assertEqual(str(self.manifest_path), result['source']['path'])
        snapshot = (Path(result['run_directory']) / 'source/settings.yaml').read_text(encoding='utf-8')
        self.assertNotIn('SYNTHETIC_PRIVATE_TOKEN', snapshot)
        self.assertNotIn('SYNTHETIC_UNUSED_GOOGLE_CREDENTIAL', snapshot)
        self.assertEqual(
            {'案内人': 'https://example.invalid/icon.png'},
            yaml.safe_load(snapshot)['*']['bots']['bot']['interfaces'][0]['params']['sender_icon_urls'])
        first, second = result['cases']
        self.assertNotEqual(first['database'], second['database'])
        self.assertTrue(Path(first['database']).is_file())
        self.assertTrue(Path(second['database']).is_file())
        self.assertEqual('right', first['case']['steps'][-1]['player']['flags']['$choice'])
        self.assertNotIn('$choice', second['case']['steps'][0]['player']['flags'])
        records = first['case']['steps'][0]['payloads']
        self.assertTrue(any(record['path'].endswith('/richmenu/xsb-local-main') for record in records))
        reply = next(record for record in records if record['path'] == '/v2/bot/message/reply')
        self.assertEqual('postback', reply['body']['messages'][0]['quickReply']['items'][1]['action']['type'])

    def test_別呼出しで同じSQLiteを読み更新前のchoiceを更新後に実行する(self):
        options = ['--session', str(self.session)]
        first = self._run(cases=[self._case('開始', [self._ask()])], extra=options)
        self.rows[5][1] = '更新した右'
        self._write_rows()

        second = self._run(
            cases=[self._case('別名の続き', [self._right('更新した右')])], extra=options)

        self.assertEqual(first['cases'][0]['database'], second['cases'][0]['database'])
        self.assertNotEqual(first['input_hash'], second['input_hash'])
        step = second['cases'][0]['case']['steps'][0]
        self.assertEqual(first['input_hash'], step['previous_input_hash'])
        self.assertEqual('right', step['player']['flags']['$choice'])

    def test_メニューのURLを展開せずビルドし未定義の動的名はverifyで失敗する(self):
        from tests.test_richmenu_spec import menu_settings
        self.config['*']['bots']['bot'].update(menu_settings())
        self._write_settings()
        self.rows = [
            ['##line.follow', '@richmenu', 'richmenu-old'], ['', '開始'],
            ['good', '@set', '$menu', '"MAIN"'], ['', '@richmenu', '{$menu}'], ['', '切替済み'],
            ['bad', '@set', '$menu', '"missing"'], ['', '@richmenu', '{$menu}'], ['', '進行しました'],
        ]
        self._write_rows()
        result = self._run(cases=[self._case('メニュー', [
            {'input': {'type': 'start'}, 'expect': {'texts': ['開始']}},
            {'input': {'type': 'text', 'text': 'good'}, 'expect': {'texts': ['切替済み']}},
            {'input': {'type': 'text', 'text': 'bad'}, 'expect': {'texts': ['進行しました']}},
        ])], expected_code=1)
        first, good, bad = result['cases'][0]['case']['steps']
        self.assertTrue(first['passed'])
        self.assertTrue(good['passed'])
        self.assertEqual('runtime', bad['phase'])
        self.assertIn('リッチメニュー missing', bad['error'])
        self.assertNotIn('payloads', bad)
        self.assertEqual('MAIN', bad['player']['flags']['$menu'])

    def test_実行失敗後は以前のchoiceを再利用しない(self):
        options = ['--session', str(self.session)]
        failed = self._run(cases=[self._case('実行失敗', [
            self._ask(),
            {'input': {'type': 'text', 'text': '失敗'}, 'expect': {'texts': []}},
        ])], extra=options, expected_code=1)
        self.assertTrue(failed['cases'][0]['case']['steps'][0]['passed'])
        failed_step = failed['cases'][0]['case']['steps'][1]
        self.assertEqual('runtime', failed_step['phase'])
        self.assertIn('人工的な実行エラー', failed_step['error'])

        resumed = self._run(cases=[self._case('古い選択', [self._right()])],
                            extra=options, expected_code=1)
        resumed_step = resumed['cases'][0]['case']['steps'][0]
        self.assertEqual('input', resumed_step['phase'])
        self.assertIn('直前の選択肢がありません', resumed_step['error'])
        self.assertNotIn('$choice', resumed_step['player']['flags'])

    def test_期待値なしと未知inputはエンジン起動前にJSONエラーにする(self):
        invalid_steps = [
            [{'input': {'type': 'start'}}],
            [{'input': {'type': 'unknown'}, 'expect': {'texts': []}}],
        ]
        for steps in invalid_steps:
            with self.subTest(steps=steps):
                with self.assertRaises(LocalInputError):
                    validate_suite({'schema_version': 1, 'cases': [self._case('不正入力', steps)]})
        # CLIの例外整形・exit 2・保存開始前の拒否は一回の実起動で残す。
        result = self._run(cases=[self._case('期待値なし', invalid_steps[0])], expected_code=2)
        self.assertEqual('LocalInputError', result['error_type'])
        self.assertFalse((self.root / 'storage').exists())

    def test_build失敗は元TSV位置付きJSONを返し古い成功URIを使わない(self):
        success = self._run(command='build')
        self.assertTrue(success['scenario_uri'].startswith('local://'))
        self.rows = [['##line.follow', '{$missing} の後に }']]
        self._write_rows()

        failure = self._run(command='build', expected_code=1)

        self.assertEqual('build', failure['phase'])
        self.assertIn('文字列の書式が不正', failure['error'])
        self.assertIn(str(self.tsv_path), failure['error'])
        self.assertIn('!1行目', failure['error'])
        self.assertNotIn('scenario_uri', failure)

    def test_TSV上書きはSheets資格情報と親AWS設定を使わない(self):
        self.config['*']['bots']['bot']['scenario'] = {
            'type': 'google_sheets',
            'params': {'sheet_id': 'must-not-request', 'key_file_json': self.credential.name},
        }
        self._write_settings()

        config, environment = self._load_config(tsv=str(self.manifest_path))

        scenario = config['bots']['bot']['scenario']
        self.assertEqual('tsv', scenario['type'])
        self.assertEqual(str(self.manifest_path), scenario['params']['manifest'])
        self.assertNotIn('key_file_json', scenario['params'])
        self.assertEqual({'provider': 'local'}, config['cloud'])
        self.assertEqual('test', environment)
        self.assertFalse((self.root / 'storage').exists())

    def test_Bot固有の非秘密設定を保持し未使用資格情報をsnapshotへ残さない(self):
        self.config['*']['bots']['bot']['interfaces'] = [
            {'type': 'line', 'params': {
                'line_access_token': 'SYNTHETIC_PRIVATE_TOKEN',
                'sender_icon_urls': {'案内人': 'https://example.invalid/icon.png'},
            }},
        ]
        self.config['*']['plugins']['google_sheets'] = {
            'service_account': 'SYNTHETIC_UNUSED_GOOGLE_CREDENTIAL',
        }
        self._write_settings()
        config, _environment = self._load_config()
        path = self.root / 'snapshot.yaml'
        save_settings(path, config)
        text = path.read_text(encoding='utf-8')
        self.assertNotIn('SYNTHETIC_PRIVATE_TOKEN', text)
        self.assertNotIn('SYNTHETIC_UNUSED_GOOGLE_CREDENTIAL', text)
        config = yaml.safe_load(text)['*']
        self.assertEqual(
            {'案内人': 'https://example.invalid/icon.png'},
            config['bots']['bot']['interfaces'][0]['params']['sender_icon_urls'])

    def test_webchatはLIFF連携先の実行設定だけを引き継ぐ(self):
        self.config['*']['plugins']['liff'] = {'action_prefix': '##menu.'}
        self.config['*']['bots']['bot']['interfaces'] = [{'type': 'webchat', 'params': {
            'liff_apps': {'menu': {'bot': 'menu', 'url': 'https://pages.example.test/menu'}},
        }}]
        self.config['*']['bots']['menu'] = {
            'interfaces': [
                {'type': 'liff'},
                {'type': 'webchat', 'params': {'scenario_uri': 'local://test/scenario/unused',
                                               'signing_key': 'SYNTHETIC_UNUSED_KEY'}},
                {'type': 'line', 'params': {'line_access_token': 'SYNTHETIC_LINE_TOKEN'}},
            ],
            'scenario': {'type': 'google_sheets', 'params': {'key_file_json': 'UNUSED_GOOGLE_CREDENTIAL'}},
        }
        self._write_settings()
        config, _environment = self._load_config(command='webchat')
        self.assertEqual({'bot', 'menu'}, set(config['bots']))
        peer = config['bots']['menu']
        self.assertEqual(['liff', 'webchat'], [item['type'] for item in peer['interfaces']])
        self.assertEqual('local://test/scenario/unused', peer['interfaces'][1]['params']['scenario_uri'])
        serialized = json.dumps(config)
        for value in ('SYNTHETIC_UNUSED_KEY', 'SYNTHETIC_LINE_TOKEN', 'UNUSED_GOOGLE_CREDENTIAL'):
            self.assertNotIn(value, serialized)
        verified, _environment = self._load_config()
        self.assertEqual({'bot'}, set(verified['bots']))

    def test_メニュー定義と定数をsnapshotへ持ち越す(self):
        from tests.test_richmenu_spec import menu_settings
        self.config['*']['bots']['bot'].update(menu_settings())
        self.config['*']['constants'] = {'menu_url': 'https://liff.line.me/example'}
        self.config['*']['plugins']['webchat'] = {'constants': {'menu_url': 'https://pages.example.test/'}}
        self._write_settings()
        args = SimpleNamespace(settings=str(self.settings_path), bot='bot', env='', tsv=None, command='build')
        config, _environment = load_config(args)
        self.assertEqual('main', config['bots']['bot']['default_richmenu'])
        self.assertIn('main', config['bots']['bot']['richmenus'])
        self.assertEqual(self.config['*']['constants'], config['constants'])
        self.assertEqual(self.config['*']['plugins']['webchat']['constants'], config['plugins']['webchat']['constants'])

    def test_インライン資格情報と非有限JSONは値を展開せず拒否する(self):
        self.config['*']['bots']['bot']['scenario'] = {
            'type': 'google_sheets', 'params': {
                'sheet_id': 'unused',
                'key_file_json': '{"private_key":"SYNTHETIC_PRIVATE_KEY"}',
            },
        }
        self._write_settings()
        with self.assertRaises(LocalInputError) as caught:
            self._load_config()
        self.assertIn('ファイルpath', str(caught.exception))
        self.assertNotIn('SYNTHETIC_PRIVATE_KEY', str(caught.exception))
        self.suite_path.write_text(json.dumps({'schema_version': 1, 'cases': [
            self._case('非有限数', [
                {'input': {'type': 'start'}, 'expect': {'flags': {'$value': float('nan')}}},
            ]),
        ]}), encoding='utf-8')
        with self.assertRaisesRegex(LocalInputError, '有限数'):
            read_json(self.suite_path)

    def test_loader設定は共通optionよりpluginとBotの指定を優先する(self):
        self.config['*']['options']['ignore_sheet'] = '^story$'
        self._write_settings()
        config, environment = self._load_config()
        params = config['bots']['bot']['scenario']['params']
        self.assertEqual('^story$', params['ignore_sheet'])
        self.assertEqual([], SheetSelector(params).select(['story'], environment))

        self.config['*']['plugins']['tsv'] = {'ignore_sheet': '^_'}
        self._write_settings()
        config, environment = self._load_config()
        params = config['bots']['bot']['scenario']['params']
        self.assertEqual('^_', params['ignore_sheet'])
        self.assertEqual(
            [('story', 'story', False)], SheetSelector(params).select(['story'], environment))

        self.config['*']['bots']['bot']['scenario']['params']['ignore_sheet'] = '^story$'
        self._write_settings()
        config, environment = self._load_config()
        params = config['bots']['bot']['scenario']['params']
        self.assertEqual('^story$', params['ignore_sheet'])
        self.assertEqual([], SheetSelector(params).select(['story'], environment))

    def test_TSV切替は元sourceの共有設定だけを継承しBot指定を優先する(self):
        common = {'evaluate_formula': False, 'script_sheet': '^common$',
                  'constant_sheet': '^common_config$', 'ignore_sheet': '^common_ignore$'}
        source = {'evaluate_formula': True, 'script_sheet': '^source$',
                  'constant_sheet': '^source_config$', 'ignore_sheet': '^source_ignore$'}
        target = {'evaluate_formula': False, 'script_sheet': '^target$',
                  'constant_sheet': '^target_config$', 'ignore_sheet': '^target_ignore$'}
        bot = {'evaluate_formula': True, 'script_sheet': '^bot$',
               'constant_sheet': '^bot_config$', 'ignore_sheet': '^bot_ignore$'}
        self.config['*']['options'].update(common)
        self.config['*']['plugins']['google_sheets'] = {
            'sheet_id': 'unused-plugin-sheet', 'key_file_json': 'unused-plugin-key.json',
            'source_only': '持ち越さない',
        }
        self.config['*']['bots']['bot']['scenario'] = {
            'type': 'google_sheets', 'params': {
                'sheet_id': 'unused-bot-sheet', 'key_file_json': 'unused-bot-key.json',
                'source_only': '持ち越さない',
            },
        }
        plugin_params = self.config['*']['plugins']['google_sheets']
        bot_params = self.config['*']['bots']['bot']['scenario']['params']
        for stage, expected in (('common', common), ('source', source), ('target', target), ('bot', bot)):
            with self.subTest(stage=stage):
                if stage == 'source':
                    plugin_params.update(source)
                elif stage == 'target':
                    self.config['*']['plugins']['tsv'] = {**target, 'manifest': 'unused-manifest.json'}
                elif stage == 'bot':
                    bot_params.update(bot)
                self._write_settings()
                config, _environment = self._load_config(tsv=str(self.manifest_path))
                scenario = config['bots']['bot']['scenario']
                self.assertEqual('tsv', scenario['type'])
                self.assertEqual(expected, {key: scenario['params'][key] for key in expected})
                self.assertEqual(str(self.manifest_path), scenario['params']['manifest'])
                for key in ('sheet_id', 'key_file_json', 'source_only'):
                    self.assertNotIn(key, scenario['params'])

    def test_TSV同士のmanifest切替でもBot固有の共有設定を保つ(self):
        self.config['*']['plugins']['tsv'] = {
            'evaluate_formula': True, 'script_sheet': '^story$',
            'constant_sheet': '^config$', 'ignore_sheet': '^plugin_ignore$',
            'manifest': 'unused-plugin.json',
        }
        self.config['*']['bots']['bot']['scenario']['params'].update({
            'evaluate_formula': False, 'ignore_sheet': '^bot_ignore$', 'manifest': 'unused-bot.json',
        })
        self._write_settings()

        config, _environment = self._load_config(tsv=str(self.manifest_path))

        params = config['bots']['bot']['scenario']['params']
        self.assertIs(False, params['evaluate_formula'])
        self.assertEqual('^story$', params['script_sheet'])
        self.assertEqual('^config$', params['constant_sheet'])
        self.assertEqual('^bot_ignore$', params['ignore_sheet'])
        self.assertEqual(str(self.manifest_path), params['manifest'])

    def test_SheetsからTSVへ切り替えても定数とセル参照の解釈を保つ(self):
        self.config['*']['bots']['bot']['scenario'] = {
            'type': 'google_sheets', 'params': {
                'sheet_id': 'unused', 'key_file_json': 'unused.json',
                'evaluate_formula': True, 'script_sheet': '^story$',
                'constant_sheet': '^config$', 'ignore_sheet': '^_',
            },
        }
        self._write_settings()
        self.rows = [['開始', '=config!B2']]
        self._write_rows()
        with (self.root / 'config.tsv').open('w', encoding='utf-8', newline='') as target:
            csv.writer(target, dialect='excel-tab').writerows([
                ['greeting', 'value'], ['', '="旅人"&"さん"'],
            ])
        self.manifest_path.write_text(json.dumps({'sheets': [
            {'name': 'story', 'path': 'story.tsv'}, {'name': 'config', 'path': 'config.tsv'},
            {'name': '_ignored', 'path': 'missing.tsv'},
        ]}), encoding='utf-8')
        config, environment = self._load_config(tsv=str(self.manifest_path))
        loader = TsvPlugin_Loader(config['bots']['bot']['scenario']['params'])

        with patch.dict(sys.modules, {'settings': SimpleNamespace(DEPLOY_ENV=environment)}):
            tables, constants = loader.load_scenario()

        self.assertEqual([('story', [['開始', '旅人さん']])], tables)
        self.assertEqual({'greeting': '旅人さん'}, constants)


if __name__ == '__main__':
    unittest.main()
