"""親へのSIGTERMで、所有する子だけを終了・回収する。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tools.local_process import run_process


class LocalProcessTest(unittest.TestCase):
    def test_終了コードと標準出力とtimeoutを維持する(self):
        result = run_process([sys.executable, '-c', 'print("ready"); raise SystemExit(3)'],
                             stdout=subprocess.PIPE, text=True)
        self.assertEqual((3, 'ready\n'), (result.returncode, result.stdout))
        with self.assertRaises(subprocess.TimeoutExpired):
            run_process([sys.executable, '-c', 'import time; time.sleep(60)'], timeout=0.05)

    def test_停止Event付きでも出力とtimeoutを維持し停止後は起動しない(self):
        stopped = threading.Event()
        result = run_process(
            [sys.executable, '-c', 'import time; print("ready"); time.sleep(0.15); print("done")'],
            cancel_event=stopped, stdout=subprocess.PIPE, text=True)
        self.assertEqual((0, 'ready\ndone\n'), (result.returncode, result.stdout))
        with self.assertRaises(subprocess.TimeoutExpired) as failure:
            run_process([sys.executable, '-c', 'import time; time.sleep(60)'],
                        timeout=0.05, cancel_event=stopped)
        self.assertEqual(0.05, failure.exception.timeout)
        stopped.set()
        # 停止済みなら、存在しない実行ファイルも起動しない。
        with self.assertRaises(KeyboardInterrupt):
            run_process(['存在しない実行ファイル'], cancel_event=stopped)

    @unittest.skipUnless(os.name == 'posix', 'SIGTERMの親子process検証はPOSIXで行う')
    def test_SIGTERMで子を残さない(self):
        script = '''
import sys
from tools.local_process import run_process, stop_on_sigterm
child = 'import os,sys,time; open(sys.argv[1], "w").write(str(os.getpid())); time.sleep(60)'
try:
    with stop_on_sigterm():
        run_process([sys.executable, '-c', child, sys.argv[1]])
except KeyboardInterrupt:
    pass
'''
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'child.pid'
            process = subprocess.Popen([sys.executable, '-c', script, str(ready)],
                                       cwd=Path(__file__).resolve().parents[1])
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() or not ready.read_text():
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail('子processが起動しませんでした')
                    time.sleep(0.01)
                child_pid = int(ready.read_text())
                process.terminate()
                self.assertEqual(0, process.wait(timeout=7))
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()

    @unittest.skipUnless(os.name == 'posix', 'SIGTERMの親子process検証はPOSIXで行う')
    def test_並行verifyのSIGTERMで実行中の子を回収し待機caseは起動しない(self):
        script = '''
import json, sys
from pathlib import Path
from tools import local_scenario
root = Path(sys.argv[1])
config = {
    'cloud': {'provider': 'local'}, 'auth': {}, 'constants': {},
    'local': {'storage_root': str(root / 'storage'),
              'public_base_url': 'http://127.0.0.1:8765/local-media', 'assets': {}},
    'options': {}, 'plugins': {'line': {}},
    'bots': {'bot': {'interfaces': [],
                     'scenario': {'type': 'tsv', 'params': {'manifest': 'unused.json'}}}},
}
local_scenario.save_settings(root / 'settings.yaml', config)
(root / 'suite.json').write_text(json.dumps({'schema_version': 1, 'cases': [
    {'name': str(index), 'steps': [{'input': {'type': 'start'}, 'expect': {'texts': []}}]}
    for index in range(4)]}))
original = local_scenario.run_worker
def worker(config, environment, request, directory, timeout):
    if request['phase'] == 'source':
        return {'ok': True, 'exit_code': 0, 'source': {'type': 'tsv', 'sha256': 'fixed'}}
    if 'case' not in request:
        return {'ok': True, 'exit_code': 0, 'scenario_uri': 'local://fixed'}
    return original(config, environment, request, directory, timeout,
                    worker_script=root / 'case.py')
local_scenario.run_worker = worker
local_scenario.os.cpu_count = lambda: 2
raise SystemExit(local_scenario.main([
    'verify', '--settings', str(root / 'settings.yaml'), '--bot', 'bot',
    '--suite', str(root / 'suite.json')]))
'''
        child = '''
import os, sys, time
from pathlib import Path
(Path(sys.argv[2]).parent / 'child.pid').write_text(str(os.getpid()))
time.sleep(60)
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'case.py').write_text(child)
            process = subprocess.Popen(
                [sys.executable, '-c', script, directory],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            child_pids = []
            try:
                deadline = time.monotonic() + 5
                while True:
                    ready = list(root.glob('storage/runs/run-*/workers/*/child.pid'))
                    child_pids = [int(path.read_text()) for path in ready if path.read_text()]
                    if len(child_pids) == 2:
                        break
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail('並行する2つの子processが起動しませんでした')
                    time.sleep(0.01)
                process.terminate()
                stdout, stderr = process.communicate(timeout=7)
                self.assertEqual(130, process.returncode, stderr)
                self.assertEqual(130, json.loads(stdout)['exit_code'])
                self.assertEqual(2, len(list(root.glob('storage/runs/run-*/workers/*/child.pid'))))
                for pid in child_pids:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                for path in root.glob('storage/runs/run-*/workers/*/child.pid'):
                    if path.read_text():
                        try:
                            os.kill(int(path.read_text()), 9)
                        except ProcessLookupError:
                            pass
                process.communicate()
