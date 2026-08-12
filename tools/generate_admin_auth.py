#!/usr/bin/env python3
"""管理画面認証JSONを対話的に生成する。"""

import argparse
import getpass
import json
import secrets

from argon2 import PasswordHasher
from argon2.low_level import Type


PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def generate(username, password, confirmation, secret_factory=None):
    """入力を検証し、Parameter Storeや環境変数へ渡すJSONを返す。"""
    if not isinstance(username, str) or not username.strip():
        raise ValueError('ユーザー名を入力してください')
    username = username.strip()
    if not isinstance(password, str) or len(password) < 15:
        raise ValueError('パスワードは15文字以上で入力してください')
    if password != confirmation:
        raise ValueError('確認用パスワードが一致しません')
    if secret_factory is None:
        secret_factory = lambda: secrets.token_urlsafe(48)
    session_secret = secret_factory()
    if (not isinstance(session_secret, str)
            or len(session_secret.encode('utf-8')) < 32):
        raise ValueError('Cookie署名鍵は32 byte以上で生成してください')
    return json.dumps({
        'users': {username: PASSWORD_HASHER.hash(password)},
        'session_secret': session_secret,
    }, ensure_ascii=False, separators=(',', ':'))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='管理画面認証JSONを対話的に生成します。')
    parser.parse_args(argv)
    username = input('管理者ユーザー名: ')
    password = getpass.getpass('パスワード: ')
    confirmation = getpass.getpass('パスワード（確認）: ')
    try:
        print(generate(username, password, confirmation))
    except ValueError as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
