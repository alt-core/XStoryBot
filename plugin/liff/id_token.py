"""LINEのID tokenを検証し、標準の期限と明示された独自期間を区別する。"""

import math
import threading
import time

import jwt
import requests


ISSUER = 'https://access.line.me'
JWKS_URL = 'https://api.line.me/oauth2/v2.1/certs'
KEYS_TTL_SECONDS = 300
KEYS_REFRESH_INTERVAL_SECONDS = 10
CLOCK_SKEW_SECONDS = 30


class InvalidIdTokenError(ValueError):
    """本人・発行先・署名・独自の利用期間を確認できない。"""


class IdTokenKeysError(RuntimeError):
    """認証用の公開鍵を取得できず、認証を開始できない。"""


def validate_max_age_seconds(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('id_token_max_age_secondsは正の有限数にしてください')
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('id_token_max_age_secondsは正の有限数にしてください') from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('id_token_max_age_secondsは正の有限数にしてください')
    return seconds


def _fetch_keys():
    try:
        response = requests.get(JWKS_URL, timeout=10, allow_redirects=False)
        if response.status_code != 200:
            raise IdTokenKeysError('LINEの認証用公開鍵を取得できません')
        data = response.json()
        raw_keys = data.get('keys') if isinstance(data, dict) else None
        if not isinstance(raw_keys, list):
            raise IdTokenKeysError('LINEの認証用公開鍵を取得できません')
        keys = {}
        for item in raw_keys:
            if (isinstance(item, dict) and item.get('kty') == 'EC'
                    and item.get('crv') == 'P-256' and item.get('alg', 'ES256') == 'ES256'
                    and item.get('use') in (None, 'sig')
                    and isinstance(item.get('kid'), str) and item['kid']):
                keys[item['kid']] = jwt.PyJWK.from_dict(item, algorithm='ES256').key
        if not keys:
            raise IdTokenKeysError('LINEの認証用公開鍵を取得できません')
        return keys
    except (requests.RequestException, jwt.PyJWTError, ValueError, TypeError):
        raise IdTokenKeysError('LINEの認証用公開鍵を取得できません') from None


class _SigningKeys:
    """既知鍵の期限と、未検証kidによる更新の間隔を別々に管理する。"""

    def __init__(self, loader=None):
        self._loader = loader or _fetch_keys
        self._snapshot = ({}, None)
        self._attempted_at = None
        self._last_failed = False
        self._lock = threading.Lock()

    def get(self, kid):
        keys, loaded_at = self._snapshot
        if (loaded_at is not None and time.monotonic() - loaded_at < KEYS_TTL_SECONDS
                and kid in keys):
            return keys[kid]
        with self._lock:
            now = time.monotonic()
            keys, loaded_at = self._snapshot
            fresh = (loaded_at is not None and now - loaded_at < KEYS_TTL_SECONDS)
            if fresh and kid in keys:
                return keys[kid]
            if (self._attempted_at is None
                    or now - self._attempted_at >= KEYS_REFRESH_INTERVAL_SECONDS):
                try:
                    refreshed = self._loader()
                except IdTokenKeysError:
                    # 更新失敗で未期限の既知鍵まで失わず、期限も延長しない。
                    self._last_failed = True
                else:
                    keys = dict(refreshed)
                    self._snapshot = (keys, time.monotonic())
                    self._last_failed = False
                    fresh = True
                finally:
                    # 遅い失敗を待っていた要求が直後に再取得しないよう、完了から数える。
                    self._attempted_at = time.monotonic()
            if not fresh or self._last_failed:
                raise IdTokenKeysError('LINEの認証用公開鍵を取得できません')
            if kid not in keys:
                raise InvalidIdTokenError('ID tokenを検証できません')
            return keys[kid]


_signing_keys = _SigningKeys()


def verify_id_token(token, channel_id, max_age_seconds=None):
    max_age = validate_max_age_seconds(max_age_seconds)
    if not isinstance(channel_id, str) or not channel_id:
        raise ValueError('LINE LoginチャネルIDが必要です')
    if not isinstance(token, str) or not token or not token.isascii():
        raise InvalidIdTokenError('ID tokenを検証できません')
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get('kid')
        if header.get('alg') != 'ES256' or not isinstance(kid, str) or not kid:
            raise InvalidIdTokenError('ID tokenを検証できません')
        key = _signing_keys.get(kid)
        # 時刻の型と期限は下で厳密に検査し、nbfの時計ずれをexpへ適用しない。
        claims = jwt.decode(
            token, key, algorithms=['ES256'], issuer=ISSUER, audience=channel_id,
            leeway=CLOCK_SKEW_SECONDS,
            options={'verify_exp': False, 'verify_iat': False, 'strict_aud': True,
                     'require': ['iss', 'aud', 'sub', 'iat', 'exp']})
        issued = claims['iat']
        expires = claims['exp']
        if type(issued) not in (int, float) or type(expires) not in (int, float):
            raise InvalidIdTokenError('ID tokenを検証できません')
        try:
            finite = math.isfinite(issued) and math.isfinite(expires)
        except OverflowError:
            finite = False
        now = time.time()
        if (not finite or issued < 0 or issued > now + CLOCK_SKEW_SECONDS
                or not isinstance(claims['sub'], str)
                or not claims['sub']):
            raise InvalidIdTokenError('ID tokenを検証できません')
        # 時計ずれの猶予はiat・nbfだけに使い、標準のexpを延長しない。
        expired = now >= expires if max_age is None else now - issued >= max_age
        if expired:
            raise InvalidIdTokenError('ID tokenを検証できません')
        return claims['sub']
    except jwt.PyJWTError:
        raise InvalidIdTokenError('ID tokenを検証できません') from None
