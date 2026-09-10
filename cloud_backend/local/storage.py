"""ローカル保存内の参照検査とファイル置換。"""

import os
from pathlib import Path
import tempfile

from cloud_backend.contracts import InvalidObjectReferenceError


def prepare_root(storage_root):
    if not storage_root:
        raise ValueError('local.storage_rootを指定してください')
    root = Path(storage_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def checked_path(root, key):
    if (not isinstance(key, str) or not key or '\\' in key
            or any(ord(char) < 32 for char in key)
            or any(part in ('', '.', '..') for part in key.split('/'))):
        raise InvalidObjectReferenceError('ローカル保存のkeyが不正です')
    path = root
    for part in key.split('/'):
        path = path / part
        # 公開領域からprivateへ向くものも含め、保存内のsymlinkは使わない。
        if path.is_symlink():
            raise InvalidObjectReferenceError('ローカル保存内のsymlinkは参照できません')
    if not path.resolve().is_relative_to(root):
        raise InvalidObjectReferenceError('ローカル保存領域の外は参照できません')
    return path


def atomic_write(path, data, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data.encode('utf-8') if isinstance(data, str) else data)
        if exclusive:
            # 初回metadataの同時作成でも、完成した一方だけを公開する。
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
