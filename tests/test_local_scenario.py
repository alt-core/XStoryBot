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

    def _load_config(self, tsv=None):
        args = SimpleNamespace(
            command='verify', settings=str(self.settings_path), bot='bot', tsv=tsv)
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
        self.assertEqual('/v2/bot/message/reply', records[0]['path'])
        self.assertEqual('postback', records[0]['body']['messages'][0]['quickReply']['items'][1]['action']['type'])

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


if __name__ == '__main__':
    unittest.main()
