"""ローカルCLIが起動した子processの終了と回収。"""

from contextlib import contextmanager
import signal
import subprocess
import time


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


def run_process(command, *, timeout=None, cancel_event=None, **kwargs):
    if cancel_event is not None and cancel_event.is_set():
        raise KeyboardInterrupt
    process = subprocess.Popen(command, **kwargs)
    try:
        if cancel_event is None:
            stdout, stderr = process.communicate(timeout=timeout)
        else:
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                if cancel_event.is_set():
                    raise KeyboardInterrupt
                wait = 0.1 if deadline is None else max(0, min(0.1, deadline - time.monotonic()))
                try:
                    stdout, stderr = process.communicate(timeout=wait)
                    break
                except subprocess.TimeoutExpired as error:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(
                            command, timeout, output=error.output, stderr=error.stderr) from None
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
