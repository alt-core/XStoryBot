"""監視による再build・server切替を、待機や外部通信なしで確認する。"""

import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools import local_watch
from tools.local_support import LocalInputError


class LocalWatchTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.input = self.root / 'scenario.tsv'
        self.font = self.root / 'font.ttf'
        self.input.write_text('initial', encoding='utf-8')
        self.font.write_bytes(b'font')
        self.info = {
            'files': [self.input], 'fonts': [self.font],
            'identity': (str(self.root / 'output'), 'http://127.0.0.1:8765/local-media', 'bot'),
            'source_type': 'tsv',
        }

    def _built(self, name, ok=True):
        return {'result': {'ok': ok, 'name': name, 'exit_code': 0 if ok else 1},
                'settings_path': str(self.root / (name + '.yaml')), 'environment': 'test'}

    def _run(self, build, on_sleep, inspect=None, start=None):
        clock = SimpleNamespace(now=0, count=0)

        def sleep(seconds):
            clock.now += seconds
            clock.count += 1
            if clock.count > 100:
                self.fail('監視loopが終了しませんでした')
            on_sleep(clock.count)

        with patch.object(local_watch.time, 'monotonic', lambda: clock.now), \
                patch.object(local_watch.time, 'sleep', sleep), \
                patch.object(local_watch, '_start_server', start or Mock(return_value=Mock(poll=lambda: None))) as launch, \
                patch.object(local_watch, '_stop_server') as stop, \
                patch('sys.stderr', io.StringIO()):
            result = local_watch.run_watch(SimpleNamespace(), build, inspect or (lambda: self.info))
        return result, launch, stop

    def test_fontの連続変更で再buildし失敗時は旧版を保って修正を待つ(self):
        build = Mock(side_effect=[self._built('first'), self._built('bad', False), self._built('last')])
        first, last = Mock(poll=lambda: None), Mock(poll=lambda: None)
        start = Mock(side_effect=[first, last])
        fixed = []

        def changes(count):
            if count == 1:
                self.font.write_bytes(b'new-font')
            elif count == 2:
                self.font.write_bytes(b'edited-font')
            elif build.call_count == 2 and not fixed and count >= 9:
                self.assertEqual(1, start.call_count)
                self.input.write_text('fixed-content', encoding='utf-8')
                fixed.append(True)
            elif build.call_count == 3:
                raise KeyboardInterrupt

        result, _, stop = self._run(build, changes, start=start)
        self.assertEqual([{'identity': self.info['identity']}] * 3,
                         [call.kwargs for call in build.call_args_list])
        self.assertEqual('last', result['name'])
        self.assertEqual(0, result['exit_code'])
        self.assertEqual([first, last], [call.args[0] for call in stop.call_args_list if call.args[0] is not None])

    def test_build中の追加編集は古い候補を起動せず取り直す(self):
        def build(identity):
            if not hasattr(build, 'changed'):
                build.changed = True
                self.input.write_text('newer-content', encoding='utf-8')
                return self._built('stale')
            return self._built('latest')

        start = Mock(return_value=Mock(poll=lambda: None))

        def stop_when_ready(count):
            if start.called:
                raise KeyboardInterrupt

        result, _, _ = self._run(Mock(side_effect=build), stop_when_ready, start=start)
        self.assertEqual('latest', result['name'])
        self.assertEqual(['latest'], [call.args[0]['result']['name'] for call in start.call_args_list])

    def test_不正manifestの部分監視を残し新しい参照file作成で復帰する(self):
        missing = self.root / 'new-manifest.json'
        phase = {'value': 'initial'}
        build = Mock(side_effect=[self._built('first'), self._built('fixed')])

        def inspect():
            if phase['value'] == 'invalid':
                return {'files': [missing], 'error': 'manifestを読めません'}
            return {**self.info, 'files': [self.input, missing]}

        def changes(count):
            if count == 1:
                phase['value'] = 'invalid'
                self.input.write_text('new-reference', encoding='utf-8')
            elif count == 7:
                self.assertEqual(1, build.call_count)
                phase['value'] = 'fixed'
                missing.write_text('{}', encoding='utf-8')
            elif build.call_count == 2:
                raise KeyboardInterrupt

        result, _, _ = self._run(build, changes, inspect=inspect)
        self.assertEqual('fixed', result['name'])

    def test_監視中のroot変更とSheets切替はbuildせず修正後に再開する(self):
        build = Mock(side_effect=[self._built('first'), self._built('fixed')])
        original = dict(self.info)

        def changes(count):
            if count == 1:
                self.info['identity'] = ('other', original['identity'][1], 'bot')
                self.input.write_text('changed-root', encoding='utf-8')
            elif count == 7:
                self.assertEqual(1, build.call_count)
                self.info.update(original, source_type='google_sheets')
                self.input.write_text('changed-source', encoding='utf-8')
            elif count == 13:
                self.assertEqual(1, build.call_count)
                self.info.update(original)
                self.input.write_text('restored', encoding='utf-8')
            elif build.call_count == 2:
                raise KeyboardInterrupt

        result, _, _ = self._run(build, changes)
        self.assertEqual('fixed', result['name'])

    def test_新server起動失敗は旧設定で一度復旧しCtrlCでその子を停止する(self):
        build = Mock(side_effect=[self._built('first'), self._built('bad-server')])
        first, restored = Mock(poll=lambda: None), Mock(poll=lambda: None)
        start = Mock(side_effect=[first, LocalInputError('起動失敗'), restored])

        def changes(count):
            if count == 1:
                self.input.write_text('edited', encoding='utf-8')
            elif start.call_count == 3:
                raise KeyboardInterrupt

        result, _, stop = self._run(build, changes, start=start)
        self.assertEqual('first', result['name'])
        self.assertEqual(['first', 'bad-server', 'first'], [call.args[0]['result']['name'] for call in start.call_args_list])
        self.assertIs(restored, stop.call_args.args[0])

    def test_旧版の復旧にも失敗したら再起動を繰り返さない(self):
        build = Mock(side_effect=[self._built('first'), self._built('new')])
        start = Mock(side_effect=[Mock(poll=lambda: None), LocalInputError('新版失敗'),
                                  LocalInputError('復旧失敗')])

        def changes(count):
            if count == 1:
                self.input.write_text('edited', encoding='utf-8')

        with self.assertRaisesRegex(LocalInputError, '復旧失敗'):
            self._run(build, changes, start=start)
        self.assertEqual(3, start.call_count)

    def test_生成fileは監視せず入力削除後の再作成は検出する(self):
        build = Mock(side_effect=[self._built('first'), self._built('missing', False), self._built('fixed')])

        def changes(count):
            if count == 1:
                (self.root / 'generated-result.json').write_text('{}', encoding='utf-8')
            elif count == 7:
                self.assertEqual(1, build.call_count)
                self.input.unlink()
            elif count == 13:
                self.assertEqual(2, build.call_count)
                self.input.write_text('restored', encoding='utf-8')
            elif build.call_count == 3:
                raise KeyboardInterrupt

        result, _, _ = self._run(build, changes)
        self.assertEqual('fixed', result['name'])

    def test_build中のCtrlCでも稼働中のserverを回収する(self):
        process = Mock(poll=lambda: None)
        build = Mock(side_effect=[self._built('first'), KeyboardInterrupt])

        def changes(count):
            if count == 1:
                self.input.write_text('edited', encoding='utf-8')

        result, _, stop = self._run(build, changes, start=Mock(return_value=process))
        self.assertEqual(0, result['exit_code'])
        self.assertIs(process, stop.call_args.args[0])

    def test_初回Sheetsや不正入力はserverを起動しない(self):
        for info in ({'files': [], 'error': '不正な設定'}, {**self.info, 'source_type': 'google_sheets'}):
            with self.subTest(info=info), patch.object(local_watch, '_start_server') as start:
                with self.assertRaises(LocalInputError):
                    local_watch.run_watch(SimpleNamespace(), Mock(), lambda: info)
                start.assert_not_called()

    def test_起動通知を待ち子の環境を固定する(self):
        process = Mock(poll=lambda: None)

        def launch(*args, **kwargs):
            env = kwargs['env']
            self.assertEqual('local', env['XSBOT_CLOUD_PROVIDER'])
            self.assertEqual('test', env['XSBOT_DEPLOY_ENV'])
            self.assertEqual(str(self.root / 'ready.yaml'), env['XSBOT_SETTINGS_FILE'])
            Path(env['XSBOT_LOCAL_READY_FILE']).write_text('ready', encoding='ascii')
            return process

        with patch.object(local_watch.subprocess, 'Popen', side_effect=launch):
            self.assertIs(process, local_watch._start_server(self._built('ready')))
        process.terminate.assert_not_called()

    def test_起動前終了と起動timeoutは子を回収する(self):
        for exited in (False, True):
            process = Mock(poll=lambda: 2 if exited else None)
            with self.subTest(exited=exited), \
                    patch.object(local_watch.subprocess, 'Popen', return_value=process), \
                    patch.object(local_watch.time, 'monotonic', side_effect=[0, 1, 20]), \
                    patch.object(local_watch.time, 'sleep'), \
                    patch.object(local_watch, '_stop_server') as stop:
                with self.assertRaises(LocalInputError):
                    local_watch._start_server(self._built('failed'))
                stop.assert_called_once_with(process)

    def test_停止の上限を超えた自分の子だけkillする(self):
        process = Mock(poll=lambda: None)
        process.wait.side_effect = [subprocess.TimeoutExpired('人工server', 5), 0]
        local_watch._stop_server(process)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(2, process.wait.call_count)


if __name__ == '__main__':
    unittest.main()
