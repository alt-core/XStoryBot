"""実EC署名と隔離した公開鍵loaderで、LIFFの本人確認を検証する。"""

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric import ec
import jwt
import requests

from plugin.liff import id_token


class LiffIdTokenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.first = ec.generate_private_key(ec.SECP256R1())
        cls.second = ec.generate_private_key(ec.SECP256R1())

    def setUp(self):
        self.wall = 1_800_000_000
        self.monotonic = 0
        self.loader = Mock(return_value={'first': self.first.public_key()})
        self.cache = id_token._SigningKeys(self.loader)
        for target, value in (
                ('time', lambda: self.wall), ('monotonic', lambda: self.monotonic)):
            timer = patch.object(id_token.time, target, side_effect=value)
            timer.start()
            self.addCleanup(timer.stop)
        cache = patch.object(id_token, '_signing_keys', self.cache)
        cache.start()
        self.addCleanup(cache.stop)

    def token(self, claims=None, key=None, headers=None, omit=()):
        payload = {
            'iss': id_token.ISSUER, 'aud': '12345', 'sub': 'U-test',
            'iat': self.wall - 10, 'exp': self.wall + 3600,
        }
        payload.update(claims or {})
        for field in omit:
            payload.pop(field)
        return jwt.encode(payload, key or self.first, algorithm='ES256',
                          headers={'kid': 'first', **(headers or {})})

    def verify(self, token, max_age=None):
        return id_token.verify_id_token(token, '12345', max_age)

    def test_通常tokenはsubを返し既知鍵では外部要求を増やさない(self):
        token = self.token()
        for _ in range(20):
            self.assertEqual('U-test', self.verify(token))
        self.loader.assert_called_once()

    def test_標準はLINEのexpを厳守し時計ずれの猶予で期限を延長しない(self):
        token = self.token({'iat': self.wall - 172800, 'exp': self.wall + 1})
        self.assertEqual('U-test', self.verify(token))
        self.wall += 1
        with self.assertRaises(id_token.InvalidIdTokenError):
            self.verify(token)
        with self.assertRaises(id_token.InvalidIdTokenError):
            self.verify(self.token({'exp': self.wall - 1}))

    def test_明示した独自期間だけLINEのexpを越えてiatから受理する(self):
        token = self.token({'iat': self.wall - 7200, 'exp': self.wall - 3600})
        with self.assertRaises(id_token.InvalidIdTokenError):
            self.verify(token)
        self.assertEqual('U-test', self.verify(token, 86400))
        self.assertEqual('U-test', self.verify(token, '7201'))
        with self.assertRaises(id_token.InvalidIdTokenError):
            self.verify(token, 7200)

    def test_明示した1日の境界からは拒否し呼出しによって期限を延ばさない(self):
        token = self.token({'iat': self.wall - 86400 + 1})
        self.assertEqual('U-test', self.verify(token, 86400))
        self.wall += 1
        with self.assertRaises(id_token.InvalidIdTokenError):
            self.verify(token, 86400)

    def test_未来iatは小さい時計ずれだけを許す(self):
        self.assertEqual('U-test', self.verify(self.token({'iat': self.wall + 30})))
        with self.assertRaises(id_token.InvalidIdTokenError):
            self.verify(self.token({'iat': self.wall + 30.001}))

    def test_署名改ざんと別issuerやaudienceや不正subを拒否する(self):
        candidates = [self.token(key=self.second)]
        for claims in ({'iss': 'https://wrong.invalid'}, {'aud': 'other'},
                       {'aud': ['12345']}, {'sub': ''}, {'sub': 1}, {'sub': None}):
            candidates.append(self.token(claims))
        for token in candidates:
            with self.subTest(token_index=candidates.index(token)), \
                    self.assertRaises(id_token.InvalidIdTokenError):
                self.verify(token)

    def test_iatの欠損や不正数値で期間判定を無効にできない(self):
        candidates = [self.token(omit=('iat',))]
        for value in (True, False, None, '1800000000', float('nan'),
                      float('inf'), -float('inf'), -1, 10 ** 500):
            candidates.append(self.token({'iat': value}))
        for index, token in enumerate(candidates):
            with self.subTest(index=index), self.assertRaises(id_token.InvalidIdTokenError):
                self.verify(token)

    def test_expの不正数値を標準でも独自期間でも受理しない(self):
        for value in (True, False, None, '1800000000', float('nan'),
                      float('inf'), -float('inf'), 10 ** 500):
            token = self.token({'exp': value})
            for max_age in (None, 86400):
                with self.subTest(value_type=type(value).__name__, max_age=max_age), \
                        self.assertRaises(id_token.InvalidIdTokenError):
                    self.verify(token, max_age)

    def test_期限判定を置換しても正式IDtokenの必須fieldは省略できない(self):
        for field in ('iss', 'aud', 'sub', 'exp'):
            with self.subTest(field=field), self.assertRaises(id_token.InvalidIdTokenError):
                self.verify(self.token(omit=(field,)))

    def test_設定期限は正の有限数とし数値env文字列を受理する(self):
        self.assertIsNone(id_token.validate_max_age_seconds(None))
        self.assertEqual(86400.0, id_token.validate_max_age_seconds('86400'))
        for value in (True, False, 0, -1, float('nan'), float('inf'),
                      '', 'nan', 'inf', 'invalid', 10 ** 500):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                self.verify(self.token(), value)
        self.loader.assert_not_called()

    def test_非ES256や不正tokenは鍵を探す前に拒否する(self):
        payload = {'iss': id_token.ISSUER, 'aud': '12345', 'sub': 'U-test', 'iat': self.wall}
        candidates = [
            jwt.encode(payload, 'x' * 32, algorithm='HS256', headers={'kid': 'first'}),
            jwt.encode(payload, None, algorithm='none', headers={'kid': 'first'}),
            jwt.encode(payload, self.first, algorithm='ES256'),
            None, '', 'opaque-access-token', '日本語', b'bytes-token',
        ]
        for index, token in enumerate(candidates):
            with self.subTest(index=index), self.assertRaises(id_token.InvalidIdTokenError):
                self.verify(token)
        self.loader.assert_not_called()

    def test_未知kidは更新を連発せず短い間隔の後に回転鍵を取得する(self):
        self.verify(self.token())
        new = self.token(key=self.second, headers={'kid': 'second'})
        self.monotonic = 1
        for _ in range(20):
            with self.assertRaises(id_token.InvalidIdTokenError):
                self.verify(new)
        self.loader.assert_called_once()
        self.loader.return_value = {'first': self.first.public_key(), 'second': self.second.public_key()}
        self.monotonic = 10
        self.assertEqual('U-test', self.verify(new))
        self.assertEqual(2, self.loader.call_count)
        self.assertEqual('U-test', self.verify(self.token()))
        self.assertEqual(2, self.loader.call_count)

    def test_期限付きで既知kidも取り直し個別鍵を永久保持しない(self):
        old = self.token()
        self.verify(old)
        self.loader.return_value = {'first': self.second.public_key()}
        self.monotonic = 300
        new = self.token(key=self.second)
        self.assertEqual('U-test', self.verify(new))
        with self.assertRaises(id_token.InvalidIdTokenError):
            self.verify(old)
        self.assertEqual(2, self.loader.call_count)

    def test_更新失敗は未期限鍵を保持し期限延長や期限切れ鍵での強行をしない(self):
        old = self.token()
        self.verify(old)
        self.loader.side_effect = id_token.IdTokenKeysError('秘密を含むloader例外')
        self.monotonic = 10
        with self.assertRaises(id_token.IdTokenKeysError):
            self.verify(self.token(key=self.second, headers={'kid': 'second'}))
        self.assertEqual('U-test', self.verify(old))
        self.assertEqual(0, self.cache._snapshot[1])
        self.assertEqual(2, self.loader.call_count)
        self.monotonic = 300
        with self.assertRaises(id_token.IdTokenKeysError):
            self.verify(old)
        self.monotonic = 301
        with self.assertRaises(id_token.IdTokenKeysError):
            self.verify(old)
        self.assertEqual(0, self.cache._snapshot[1])
        self.assertEqual(3, self.loader.call_count)

    def test_初回の取得失敗も更新間隔を守り例外の秘密を出さない(self):
        self.loader.side_effect = id_token.IdTokenKeysError('secret-token')
        for _ in range(20):
            with self.assertRaises(id_token.IdTokenKeysError) as raised:
                self.verify(self.token())
            self.assertNotIn('secret-token', str(raised.exception))
        self.loader.assert_called_once()

    def test_並行要求でもロック内で再判定して鍵の更新は1回だけ行う(self):
        entered, release = threading.Event(), threading.Event()

        def load():
            entered.set()
            if not release.wait(2):
                raise RuntimeError('テストの解除が届きません')
            return {'first': self.first.public_key()}

        self.loader.side_effect = load
        token = self.token()
        with ThreadPoolExecutor(max_workers=8) as executor:
            first = executor.submit(self.verify, token)
            self.assertTrue(entered.wait(2))
            rest = [executor.submit(self.verify, token) for _ in range(7)]
            release.set()
            self.assertEqual(['U-test'] * 8, [future.result() for future in [first, *rest]])
        self.loader.assert_called_once()

    def test_遅い取得失敗は並行要求で共有し完了後の間隔を守る(self):
        for expired in (False, True):
            with self.subTest(expired_cache=expired):
                self.monotonic = 0
                self.loader.reset_mock(side_effect=True)
                self.cache = id_token._SigningKeys(self.loader)
                token = self.token()
                with patch.object(id_token, '_signing_keys', self.cache):
                    if expired:
                        self.verify(token)
                        self.monotonic = 300
                    before = self.cache._snapshot
                    self.loader.reset_mock()
                    entered, release = threading.Event(), threading.Event()
                    ready = threading.Barrier(9)
                    started_at = self.monotonic

                    def load():
                        entered.set()
                        if not release.wait(2):
                            raise RuntimeError('テストの解除が届きません')
                        # 実際には待たず、取得に再試行間隔より長い時間がかかった場合を再現する。
                        self.monotonic += 10.1
                        raise id_token.IdTokenKeysError('遅い取得失敗')

                    def authenticate():
                        ready.wait(2)
                        try:
                            self.verify(token)
                        except id_token.IdTokenKeysError:
                            return 'keys-unavailable'
                        raise AssertionError('鍵取得失敗で認証を通してはいけません')

                    self.loader.side_effect = load
                    with ThreadPoolExecutor(max_workers=8) as executor:
                        requests = [executor.submit(authenticate) for _ in range(8)]
                        try:
                            ready.wait(2)
                            self.assertTrue(entered.wait(2))
                        finally:
                            release.set()
                        self.assertEqual(['keys-unavailable'] * 8, [future.result() for future in requests])
                    self.loader.assert_called_once()
                    self.assertAlmostEqual(started_at + 10.1, self.monotonic)
                    self.assertEqual(before, self.cache._snapshot)
                    finished_at = self.monotonic
                    self.loader.side_effect = None
                    self.monotonic = finished_at + 9.999
                    with self.assertRaises(id_token.IdTokenKeysError):
                        self.verify(token)
                    self.loader.assert_called_once()
                    self.monotonic = finished_at + 10
                    self.assertEqual('U-test', self.verify(token))
                    self.assertEqual(2, self.loader.call_count)

    def test_未知kidの更新が通信待ちでも未期限known認証を止めない(self):
        known = self.token()
        self.verify(known)
        self.monotonic = 10
        entered, release = threading.Event(), threading.Event()

        def load():
            entered.set()
            if not release.wait(2):
                raise RuntimeError('テストの解除が届きません')
            return {'first': self.first.public_key(), 'second': self.second.public_key()}

        self.loader.side_effect = load
        unknown = self.token(key=self.second, headers={'kid': 'second'})
        with ThreadPoolExecutor(max_workers=2) as executor:
            updating = executor.submit(self.verify, unknown)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual('U-test', executor.submit(self.verify, known).result(timeout=1))
                self.assertFalse(updating.done())
            finally:
                release.set()
            self.assertEqual('U-test', updating.result())
        self.assertEqual(2, self.loader.call_count)

    def test_公開鍵URLは固定でtoken内の鍵URLを使わない(self):
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.first.public_key()))
        response = SimpleNamespace(status_code=200, json=lambda: {'keys': [{**jwk, 'kid': 'first'}]})
        token = self.token(headers={'jku': 'https://wrong.invalid/keys', 'x5u': 'https://wrong.invalid/cert'})
        with patch.object(id_token.requests, 'get', return_value=response) as request, \
                patch.object(id_token, '_signing_keys', id_token._SigningKeys()):
            self.assertEqual('U-test', self.verify(token))
        request.assert_called_once_with(id_token.JWKS_URL, timeout=10, allow_redirects=False)

    def test_鍵取得の通信やJSONや鍵形式の失敗は認証へ進めない(self):
        responses = [
            SimpleNamespace(status_code=302), SimpleNamespace(status_code=500),
            SimpleNamespace(status_code=200, json=lambda: None),
            SimpleNamespace(status_code=200, json=lambda: {'keys': []}),
            SimpleNamespace(status_code=200, json=Mock(side_effect=ValueError('secret-token'))),
        ]
        for index, response in enumerate(responses):
            with self.subTest(index=index), patch.object(id_token.requests, 'get', return_value=response), \
                    self.assertRaises(id_token.IdTokenKeysError) as raised:
                id_token._fetch_keys()
            self.assertNotIn('secret-token', str(raised.exception))
        with patch.object(id_token.requests, 'get', side_effect=requests.Timeout('secret-token')), \
                self.assertRaises(id_token.IdTokenKeysError) as raised:
            id_token._fetch_keys()
        self.assertNotIn('secret-token', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
