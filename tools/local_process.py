"""ローカルCLIが起動した子processの終了と回収。"""

from contextlib import contextmanager
import signal
import subprocess


STOP_TIMEOUT = 5


def stop_process(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=STOP_TIMEOUT)


def run_process(command, *, timeout=None, **kwargs):
    process = subprocess.Popen(command, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        stop_process(process)


@contextmanager
def stop_on_sigterm():
    def stop(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
