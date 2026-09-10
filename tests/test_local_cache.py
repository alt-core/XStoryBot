"""永続するbuild cacheとケース別の会話保存を確認する。"""

import copy
import hashlib
import json
import os
from pathlib import Path
import pickle
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cloud_backend.local.object_store import LocalObjectStore
from cloud_backend.local.state_store import LocalStateStore
from tools import local_scenario
from tools.local_support import write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_URL = 'http://127.0.0.1:8765/local-media'
PLAYER_ID = 'bot:line:user,local-player'


class LocalCacheTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = {
            'cloud': {'provider': 'local'}, 'auth': {}, 'constants': {},
            'local': {'storage_root': str(self.root / 'storage'),
                      'public_base_url': BASE_URL, 'assets': {}, 'allow_external_media': False},
            'options': {'scenario_version': 3, 'reset_keyword': '!reset'},
            'plugins': {
                'line': {'alt_text': '確認'},
                'line.image_text': {'default_frame': 'small', 'frames': {'small': {
                    'more_mode': 'quick_between', 'size_x': 200, 'size_y': 120, 'font_size': 20,
                }}},
            },
            'bots': {'bot': {'interfaces': [], 'state_namespace': 'bot',
                             'scenario': {'type': 'tsv', 'params': {'manifest': 'unused.json'}}}},
        }
        self.calls = []
        self.incoming_players = []
        self.common_failure = None
        self.build_number = 0

    @staticmethod
    def _case(name):
        return {'name': name, 'seed': 17, 'steps': [
            {'input': {'type': 'start'}, 'expect': {'absent_flags': ['$missing']}},
        ]}

    def _execute(self, cases, session=None):
        settings_path = self.root / 'settings.yaml'
        local_scenario.save_settings(settings_path, self.config)
        suite = self.root / 'suite.json'
        write_json(suite, {'schema_version': 1, 'cases': cases})
        arguments = ['verify', '--settings', str(settings_path), '--bot', 'bot', '--suite', str(suite)]
        if session is not None:
            arguments.extend(['--session', str(session)])
        with patch.object(local_scenario, 'run_worker', side_effect=self._worker):
            return local_scenario.execute(local_scenario.parse_args(arguments))

    def _worker(self, config, environment, request, directory, timeout):
        phase = request['phase']
        case = request.get('case')
        self.calls.append((phase, case['name'] if case else None))
        if phase == 'source':
            source = Path(request['source_path'])
            source.parent.mkdir(parents=True)
            source.write_bytes(b'fixed-input')
            return {'ok': True, 'exit_code': 0,
                    'source': {'type': 'tsv', 'sha256': 'fixed-input'}}
        local = config['local']
        objects = LocalObjectStore(local['storage_root'], BASE_URL)
        state = LocalStateStore(local.get('state_storage_root', objects.storage_root))
        uri = request.get('scenario_uri')
        asset_urls = request.get('asset_urls', {})
        if uri is None:
            self.build_number += 1
            body = f'compiled-{self.build_number}'.encode('ascii')
            uri = objects.store_scenario('scenario/' + hashlib.md5(body).hexdigest(), body)
            url = objects.store_public('imagemap/page/1040', b'original-image', 'image/png')
            state.save_global_bot_variables('bot', uri)
            state.put_image_text_stat('cached-text', {'url': url})
            state.set_build_cache('build-result', 'success')
            if self.common_failure:
                return {'ok': False, 'exit_code': 1, 'phase': self.common_failure,
                        'error': '共通buildの失敗'}
        elif case is not None:
            self.assertNotEqual(state.storage_root, objects.storage_root)
            self.assertIsNone(state.get_global_bot_variables('bot'))
            self.assertIsNone(state.get_image_text_stat('cached-text'))
            self.assertEqual(f'compiled-{self.build_number}'.encode('ascii'), objects.load_scenario(uri))
        if case is not None:
            self.incoming_players.append(state.load_player_status(PLAYER_ID))
            self.assertEqual((None, None), state.get_next_label(PLAYER_ID))
            state.force_put_player_status(PLAYER_ID, {'case': case['name']})
            state.set_next_label(PLAYER_ID, case['name'], '次へ')
            write_json(state.storage_root / 'metadata/line-session.json', {'case': case['name']})
        return {'ok': True, 'exit_code': 0, 'scenario_uri': uri, 'asset_urls': asset_urls,
                'storage_root': str(state.storage_root), 'object_storage_root': str(objects.storage_root)}

    def _assert_no_player_data(self, root):
        with sqlite3.connect(root / 'state.sqlite3') as connection:
            kinds = {kind for kind, in connection.execute('SELECT DISTINCT kind FROM records')}
        self.assertTrue({'global', 'cache', 'image_text'}.issubset(kinds))
        self.assertTrue({'player', 'next_label'}.isdisjoint(kinds))
        self.assertFalse((root / 'metadata/line-session.json').exists())

    def test_一caseを繰り返してもbuild保存先を再利用しセーブは毎回白紙にする(self):
        parent = LocalStateStore(self.config['local']['storage_root'])
        parent.force_put_player_status(PLAYER_ID, {'existing': True})
        parent.set_next_label(PLAYER_ID, 'existing', '以前の選択')
        write_json(parent.storage_root / 'metadata/line-session.json', {'existing': True})
        first = self._execute([self._case('a')])
        second = self._execute([self._case('b')])
        self.assertTrue(first['ok'] and second['ok'])
        self.assertEqual(2, self.build_number)
        self.assertEqual([None, None], self.incoming_players)
        cache_root = parent.storage_root / 'verify-cache/bot'
        self._assert_no_player_data(cache_root)
        roots = []
        for name, result in (('a', first), ('b', second)):
            item = result['cases'][0]
            self.assertEqual(str(cache_root), item['object_storage_root'])
            root = Path(item['storage_root'])
            roots.append(root)
            self.assertEqual(root / 'state.sqlite3', Path(item['database']))
            self.assertEqual({'case': name}, LocalStateStore(root).load_player_status(PLAYER_ID).data)
            self.assertEqual((name, '次へ'), LocalStateStore(root).get_next_label(PLAYER_ID))
            self.assertFalse((root / 'public').exists())
            self.assertFalse((root / 'scenario').exists())
            self.assertFalse((Path(result['run_directory']) / 'baseline').exists())
        self.assertNotEqual(*roots)
        self.assertEqual({'existing': True}, parent.load_player_status(PLAYER_ID).data)
        self.assertEqual(('existing', '以前の選択'), parent.get_next_label(PLAYER_ID))
        self.assertEqual({'existing': True}, json.loads(
            (parent.storage_root / 'metadata/line-session.json').read_text()))

    def test_複数caseにもbuildは一度だけでケース間にNextLabelとsessionを混ぜない(self):
        result = self._execute([self._case('a'), self._case('b')])
        self.assertTrue(result['ok'])
        self.assertEqual(1, self.build_number)
        self.assertEqual(4, len(self.calls))
        self.assertEqual([None, None], self.incoming_players)
        self.assertEqual(result['cases'][0]['scenario_uri'], result['cases'][1]['scenario_uri'])
        for name, item in zip(('a', 'b'), result['cases']):
            root = Path(item['storage_root'])
            self.assertEqual((name, '次へ'), LocalStateStore(root).get_next_label(PLAYER_ID))
            self.assertEqual({'case': name}, json.loads((root / 'metadata/line-session.json').read_text()))

    def test_明示sessionは専用rootでbuildと会話を行い既存セーブを引き継ぐ(self):
        session = self.root / 'session'
        LocalStateStore(session).force_put_player_status(PLAYER_ID, {'saved': True})
        result = self._execute([self._case('one')], session=session)
        self.assertTrue(result['ok'])
        self.assertEqual(2, len(self.calls))
        self.assertEqual({'saved': True}, self.incoming_players[0].data)
        self.assertEqual(str(session), result['cases'][0]['storage_root'])
        self.assertEqual(str(session), result['cases'][0]['object_storage_root'])
        self.assertFalse((Path(self.config['local']['storage_root']) / 'verify-cache').exists())

    def test_共通buildまたはloadの失敗では古い成果物からcaseを実行しない(self):
        self._execute([self._case('old')])
        for phase in ('build', 'load'):
            with self.subTest(phase=phase):
                self.calls.clear()
                self.common_failure = phase
                result = self._execute([self._case('a'), self._case('b')])
                self.assertFalse(result['ok'])
                self.assertEqual((1, phase, []), (result['exit_code'], result['phase'], result['cases']))
                self.assertEqual([('source', None), ('build', None)], self.calls)
                self.assertFalse((Path(result['run_directory']) / 'cases').exists())

    def test_実buildは別runでも描画を再利用し変更分だけ更新して固定URIで進行する(self):
        font = self.root / 'font.ttf'
        shutil.copyfile(PROJECT_ROOT / 'plugin/render_text/font/ipaexg.ttf', font)
        self.config['plugins']['line.image_text']['frames']['small']['font_path'] = str(font)
        rows = [
            ['##line.follow', '@set', '$checked', 'true'], ['', '@random', '##one', '##two'],
            ['##one', 'one'], ['', '@set', '$route', '"one"'],
            ['', '@imagetext', '絵甲'], ['', '@imagetext', '絵乙'],
            ['##two', 'two'], ['', '@set', '$route', '"two"'],
            ['', '@imagetext', '絵甲'], ['', '@imagetext', '絵乙'],
        ]
        source = self.root / 'source.pickle'
        source.write_bytes(pickle.dumps(([('story', rows)], {}), protocol=4))
        cache_root = self.root / 'cache'
        cold = self._real_work('first-build', cache_root, source)
        self.assertEqual(4, cold['render_calls'])
        self._assert_no_player_data(cache_root)
        first = self._real_work('first-case', cache_root, source, built=cold)
        warm = self._real_work('second-build', cache_root, source, exercise_updates=True)
        self.assertEqual(cold['scenario_uri'], warm['scenario_uri'])
        self.assertEqual([False], warm['build_forces'])
        updates = warm['updates']
        for name in ('changed', 'font_changed', 'recovered'):
            self.assertTrue(updates[name]['ok'], updates[name])
        changed = updates['changed']
        self.assertEqual(1, changed['render_calls'])
        self.assertNotEqual(cold['scenario_uri'], changed['scenario_uri'])
        # Globalsが新しいURIを指しても、先行runのcaseは渡された固定URIを読む。
        second = self._real_work('second-case', cache_root, source, built=cold)
        self.assertEqual(first['case'], second['case'])
        self.assertEqual([], second['build_forces'])
        self.assertEqual(0, second['render_calls'])
        self.assertTrue(second['case']['steps'][0]['player']['flags']['$checked'])
        self.assertTrue(second['artifacts'])
        self.assertEqual([], second['unresolved_media'])
        self.assertIsNone(LocalStateStore(self.root / 'second-case-state').get_global_bot_variables('bot'))
        font_changed = updates['font_changed']
        self.assertEqual([True], font_changed['build_forces'])
        self.assertEqual(4, font_changed['render_calls'])
        failed = updates['failed']
        self.assertFalse(failed['ok'])
        self.assertEqual('build', failed['phase'])
        self.assertEqual(2, failed['render_calls'])
        self.assertFalse(failed['marker_exists'])
        # 最後に成功したfontへ戻しても、途中失敗で混ざったcacheは再利用しない。
        recovered = updates['recovered']
        self.assertEqual([True], recovered['build_forces'])
        self.assertEqual(4, recovered['render_calls'])
        self._assert_no_player_data(cache_root)

    def _real_work(self, name, cache_root, source, built=None, exercise_updates=False):
        directory = self.root / name
        directory.mkdir()
        config = copy.deepcopy(self.config)
        config['local']['storage_root'] = str(cache_root)
        request = {'phase': 'build', 'bot': 'bot', 'source_path': str(source),
                   'run_id': name, 'input_hash': 'fixed',
                   'render_signature': local_scenario.render_input_signature(config)}
        if built is not None:
            config['local']['state_storage_root'] = str(self.root / (name + '-state'))
            request.update(case=self._case('seed'), scenario_uri=built['scenario_uri'],
                           asset_urls=built['asset_urls'])
        settings_path = directory / 'settings.yaml'
        local_scenario.save_settings(settings_path, config)
        request_path = directory / 'request.json'
        write_json(request_path, request)
        script = '''
import contextlib, json, pickle, sys
from pathlib import Path
from unittest.mock import patch
from tools.local_scenario import _build, render_input_signature
from plugin.render_text import renderer
from runtime import BotRuntime
import settings
original_render = renderer.render_text_to_png
original_build = BotRuntime.build_scenario
calls = 0
forces = []
forbid_render = False
fail_render_after = None
def render(*args, **kwargs):
    global calls
    calls += 1
    if forbid_render:
        raise AssertionError('cacheまたは固定URIからの実行では再描画しません')
    if fail_render_after is not None and calls > fail_render_after:
        raise ValueError('描画の途中失敗')
    return original_render(*args, **kwargs)
def build(self, *args, **kwargs):
    forces.append(kwargs['options'].get('force', False))
    return original_build(self, *args, **kwargs)
with open(sys.argv[1], encoding='utf-8') as stream:
    request = json.load(stream)
def run(forbid=False, fail_after=None):
    global calls, forces, forbid_render, fail_render_after
    calls, forces, forbid_render, fail_render_after = 0, [], forbid, fail_after
    request['render_signature'] = render_input_signature(settings.settings)
    with contextlib.redirect_stdout(sys.stderr), patch.object(renderer, 'render_text_to_png', side_effect=render), patch.object(BotRuntime, 'build_scenario', build):
        result = _build(request)
    result.update(render_calls=calls, build_forces=forces)
    return result
result = run(forbid=sys.argv[2] == 'updates' or 'scenario_uri' in request)
if sys.argv[2] == 'updates':
    # 同じcacheへの入力変更だけを同一processにまとめる。更新後のURI同一性は比較しない。
    source = Path(request['source_path'])
    tables, constants = pickle.loads(source.read_bytes())
    for _, rows in tables:
        for row in rows:
            if row[-1] == '絵甲':
                row[-1] = '絵丙'
    source.write_bytes(pickle.dumps((tables, constants), protocol=4))
    updates = {'changed': run()}
    font = Path(settings.PLUGINS['line.image_text']['frames']['small']['font_path'])
    original_font = font.read_bytes()
    font.write_bytes(original_font + b'font-content-changed')
    updates['font_changed'] = run()
    font.write_bytes(original_font)
    updates['failed'] = run(fail_after=1)
    updates['failed']['marker_exists'] = (Path(settings.BACKEND_SETTINGS['storage_root']) / 'metadata/render-inputs.json').exists()
    font.write_bytes(original_font + b'font-content-changed')
    updates['recovered'] = run()
    result['updates'] = updates
print(json.dumps(result, ensure_ascii=False))
'''
        process = subprocess.run(
            [sys.executable, '-B', '-c', script, str(request_path),
             'updates' if exercise_updates else 'single'],
            cwd=PROJECT_ROOT,
            env={**os.environ, 'XSBOT_CLOUD_PROVIDER': 'local', 'XSBOT_DEPLOY_ENV': '',
                 'XSBOT_SETTINGS_FILE': str(settings_path), 'PYTHONDONTWRITEBYTECODE': '1'},
            capture_output=True, text=True, timeout=20)
        self.assertEqual(0, process.returncode, process.stderr)
        result = json.loads(process.stdout)
        self.assertTrue(result['ok'], result)
        return result


if __name__ == '__main__':
    unittest.main()
