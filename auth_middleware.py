"""管理画面の共通フォーム認証。"""

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from functools import wraps
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type
from bottle import HTTPError, request, response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from cloud_backend import create_credential_source
from cloud_backend.contracts import CredentialSourceError
import settings


SESSION_COOKIE_NAME = '__Secure-xsbot-dashboard'
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60
SESSION_SALT = 'xstorybot-dashboard-session-v1'
MINIMUM_SESSION_SECRET_BYTES = 32

_password_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
_credential_source = None
_admin_auth_config = None
_serializer = None


def _auth_error(status_code, message):
    return HTTPError(status_code, message, Cache_Control='no-store')


def initialize():
    """後方互換用の初期化入口。秘密値は最初の認証時まで取得しない。"""
    return None


def _source():
    global _credential_source
    if _credential_source is None:
        _credential_source = create_credential_source()
    return _credential_source


def _load_admin_auth_config():
    global _admin_auth_config
    if _admin_auth_config is not None:
        return _admin_auth_config

    raw_config = _source().get_admin_auth_json()
    try:
        config = json.loads(raw_config)
    except (TypeError, ValueError) as error:
        raise CredentialSourceError('管理者認証JSONが不正です') from error
    if not isinstance(config, Mapping):
        raise CredentialSourceError('管理者認証JSONはobjectで指定してください')

    users = config.get('users')
    if not isinstance(users, Mapping) or not users:
        raise CredentialSourceError('管理者認証JSONのusersを設定してください')
    normalized_users = {}
    for username, password_hash in users.items():
        if not isinstance(username, str) or not username:
            raise CredentialSourceError('管理者ユーザー名は空でない文字列で指定してください')
        if not isinstance(password_hash, str):
            raise CredentialSourceError('管理者パスワードハッシュは文字列で指定してください')
        if not password_hash.startswith('$argon2id$'):
            raise CredentialSourceError('管理者パスワードハッシュはArgon2idで指定してください')
        try:
            _password_hasher.check_needs_rehash(password_hash)
        except InvalidHashError as error:
            raise CredentialSourceError('管理者パスワードハッシュが不正です') from error
        normalized_users[username] = password_hash

    session_secret = config.get('session_secret')
    if (not isinstance(session_secret, str)
            or len(session_secret.encode('utf-8')) < MINIMUM_SESSION_SECRET_BYTES):
        raise CredentialSourceError('管理画面のCookie署名鍵は32 byte以上で指定してください')

    _admin_auth_config = {
        'users': normalized_users,
        'session_secret': session_secret,
    }
    return _admin_auth_config


def _session_serializer():
    global _serializer
    if _serializer is None:
        config = _load_admin_auth_config()
        _serializer = URLSafeTimedSerializer(
            config['session_secret'],
            salt=SESSION_SALT,
            signer_kwargs={'digest_method': hashlib.sha256},
        )
    return _serializer


def verify_credentials(username, password):
    """ユーザー名とパスワードが一致した場合だけTrueを返す。"""
    if not isinstance(username, str) or not isinstance(password, str):
        return False
    users = _load_admin_auth_config()['users']
    password_hash = users.get(username)
    candidate_hash = (
        password_hash if password_hash is not None else next(iter(users.values())))
    try:
        verified = _password_hasher.verify(candidate_hash, password)
    except (VerificationError, InvalidHashError):
        verified = False
    return password_hash is not None and verified


def create_session(username):
    """署名済みCookie値とCSRFトークンを生成する。"""
    csrf_token = secrets.token_urlsafe(32)
    value = _session_serializer().dumps({
        'username': username,
        'csrf_token': csrf_token,
    })
    return value, csrf_token


def set_session_cookie(value):
    response.set_cookie(
        SESSION_COOKIE_NAME,
        value,
        max_age=SESSION_MAX_AGE_SECONDS,
        path='/dashboard',
        secure=True,
        httponly=True,
        samesite='lax',
    )


def clear_session_cookie():
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path='/dashboard',
        secure=True,
        httponly=True,
        samesite='lax',
    )


def load_session():
    """有効なCookieを検証し、セッション内容を返す。"""
    value = request.get_cookie(SESSION_COOKIE_NAME)
    if not value:
        raise _auth_error(401, 'Authentication required')
    try:
        session = _session_serializer().loads(
            value, max_age=SESSION_MAX_AGE_SECONDS)
    except SignatureExpired:
        raise _auth_error(401, 'Session expired')
    except BadSignature:
        raise _auth_error(401, 'Invalid session')

    if not isinstance(session, Mapping):
        raise _auth_error(401, 'Invalid session')
    username = session.get('username')
    csrf_token = session.get('csrf_token')
    if (not isinstance(username, str) or not username
            or not isinstance(csrf_token, str) or not csrf_token):
        raise _auth_error(401, 'Invalid session')
    return dict(session)


def _normalized_origin(value):
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return None
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    return f'{parsed.scheme.lower()}://{parsed.netloc.lower()}'


def require_same_origin():
    """設定済みアプリURLとOriginが一致することを確認する。"""
    expected = _normalized_origin(
        settings.SERVICE_SETTINGS.get('app', {}).get('base_url', ''))
    received = _normalized_origin(request.headers.get('Origin', ''))
    if expected is None or received is None or received != expected:
        raise _auth_error(403, 'Invalid origin')


def require_csrf(session):
    """状態変更リクエストのCSRFトークンを検証する。"""
    received = request.headers.get('X-CSRF-Token', '')
    expected = session.get('csrf_token', '')
    if (not isinstance(received, str) or not isinstance(expected, str)
            or not received or not hmac.compare_digest(received, expected)):
        raise _auth_error(403, 'Invalid CSRF token')


def auth_required(state_changing=False):
    """管理画面セッションを必要とするエンドポイントのデコレータ。"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            session = load_session()
            response.set_header('Cache-Control', 'no-store')
            request.dashboard_user = {
                'username': session['username'],
                'csrf_token': session['csrf_token'],
            }
            if state_changing:
                require_same_origin()
                require_csrf(session)
            return func(*args, **kwargs)

        wrapper.dashboard_auth_required = True
        wrapper.dashboard_state_changing = state_changing
        return wrapper
    return decorator


def reset_for_test():
    """単体テストでプロセス内cacheを初期化する。"""
    global _credential_source, _admin_auth_config, _serializer
    _credential_source = None
    _admin_auth_config = None
    _serializer = None
