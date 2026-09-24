"""元のローカル入力を監視し、完成したScenarioへWebchatを切り替える。"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from tools.local_support import LocalInputError
from tools.local_process import stop_process as _stop_server


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLL_SECONDS = 0.2
DEBOUNCE_SECONDS = 0.5
READY_TIMEOUT = 15


def _start_server(built):
    # 起動通知はbindとScenario読込の完了後だけ届く。HTTPへの試験入力は送らない。
    with tempfile.TemporaryDirectory(prefix='xsbot-webchat-ready-') as directory:
        ready = Path(directory) / 'ready'
        environment = {
            **os.environ, 'XSBOT_CLOUD_PROVIDER': 'local',
            'XSBOT_SETTINGS_FILE': str(built['settings_path']),
            'XSBOT_DEPLOY_ENV': built['environment'],
            'XSBOT_LOCAL_READY_FILE': str(ready),
        }
        process = subprocess.Popen(
            [sys.executable, '-m', 'tools.local_webchat'], cwd=PROJECT_ROOT,
            env=environment, stdout=sys.stderr,
        )
        try:
            deadline = time.monotonic() + READY_TIMEOUT
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise LocalInputError('新しいWebchatが起動前に終了しました')
                if ready.exists():
                    return process
                time.sleep(POLL_SECONDS)
            raise LocalInputError('Webchatの起動確認がtimeoutしました')
        except BaseException:
            _stop_server(process)
            raise


def _files(info):
    return {Path(path) for path in info.get('files', []) + info.get('fonts', [])}


def _fingerprint(files):
    result = {}
    for path in files:
        try:
            stat = path.stat()
            result[path] = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError as error:
            # 未作成・削除・一時的に読めない入力も、復帰を検出するため残す。
            result[path] = ('unavailable', error.errno)
    return result


def _validate(info, identity=None):
    if info.get('error'):
        raise LocalInputError(info['error'])
    if info['source_type'] != 'tsv':
        raise LocalInputError('--watchはTSV入力だけに対応しています（Sheetsの更新は監視しません）')
    if identity is not None and tuple(info['identity']) != identity:
        raise LocalInputError('--watch中はstorage_root・public_base_url・Botを変更できません')


def run_watch(args, build_callback, inspect_callback):
    """毎回取り直す入力検査とbuildを受け取り、Ctrl-Cまで監視する。"""
    info = inspect_callback()
    _validate(info)
    identity = tuple(info['identity'])
    files = _files(info)
    observed = _fingerprint(files)
    pending = time.monotonic() - DEBOUNCE_SECONDS
    process = None
    active = None
    result = {'ok': False, 'exit_code': 2, 'command': 'webchat'}
    print('入力を監視します。更新は次の入力から反映します。終了: Ctrl-C', file=sys.stderr)
    try:
        while True:
            if process is not None and process.poll() is not None:
                raise LocalInputError('監視中のWebchatが終了しました')
            current = _fingerprint(files)
            if current != observed:
                observed = current
                pending = time.monotonic()
            if pending is not None and time.monotonic() - pending >= DEBOUNCE_SECONDS:
                pending = None
                before = observed
                try:
                    info = inspect_callback()
                    if info.get('error'):
                        files |= _files(info)
                    else:
                        files = _files(info)
                    before = _fingerprint(files)
                    _validate(info, identity)
                    built = build_callback(identity=identity)
                except (LocalInputError, OSError, ValueError) as error:
                    print(f'入力・buildを確認してください: {error}', file=sys.stderr)
                    built = None
                observed = _fingerprint(files)
                if observed != before:
                    # build中にも編集された候補は配信せず、最新の入力を取り直す。
                    pending = time.monotonic()
                    print('build中の編集を検出しました。入力を取り直します。', file=sys.stderr)
                elif built is not None and built['result']['ok']:
                    _stop_server(process)
                    process = None
                    try:
                        process = _start_server(built)
                    except (LocalInputError, OSError) as error:
                        if active is None:
                            raise
                        print(f'{error}。直前のWebchatへ戻します。', file=sys.stderr)
                        process = _start_server(active)
                    else:
                        active = built
                        result = built['result']
                        print('Webchatへ反映しました。保存状態は維持します。', file=sys.stderr)
                elif built is not None:
                    print('buildに失敗しました。入力の修正を待ちます。', file=sys.stderr)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        return {**result, 'ok': True, 'exit_code': 0}
    finally:
        _stop_server(process)
