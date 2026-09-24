"""親へのSIGTERMで、所有する子だけを終了・回収する。"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
