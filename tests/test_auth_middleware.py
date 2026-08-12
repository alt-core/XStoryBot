import json
import importlib.util
import sys
import types
import unittest
from unittest.mock import Mock, patch

from argon2 import PasswordHasher
from argon2.low_level import Type
from bottle import HTTPError

from cloud_backend.contracts import CredentialSourceError


PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
SESSION_SECRET = 's' * 32
PROJECT_ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]


def load_auth_middleware():
    settings = types.ModuleType('settings')
    settings.SERVICE_SETTINGS = {
        'app': {'base_url': 'https://app.example.test/base'},
    }
    previous = sys.modules.get('settings')
    module_name = '_test_form_auth_middleware'
    previous_module = sys.modules.get(module_name)
    try:
        sys.modules['settings'] = settings
        spec = importlib.util.spec_from_file_location(
            module_name, PROJECT_ROOT / 'auth_middleware.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop('settings', None)
        else:
            sys.modules['settings'] = previous
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module


class FakeRequest:
    def __init__(self):
        self.headers = {}
        self.cookies = {}

    def get_cookie(self, name):
        return self.cookies.get(name)


class AuthMiddlewareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = PASSWORD_HASHER.hash('correct-password')

    def setUp(self):
        self.module = load_auth_middleware()
        self.module.reset_for_test()
        self.source = Mock()
        self.source.get_admin_auth_json.return_value = json.dumps({
            'users': {'admin': self.password_hash},
            'session_secret': SESSION_SECRET,
        })
        self.module._credential_source = self.source
        self.request = FakeRequest()
        self.module.request = self.request

    def tearDown(self):
        self.module.reset_for_test()

    def assert_http_error(self, status_code, function):
        with self.assertRaises(HTTPError) as raised:
            function()
        self.assertEqual(status_code, raised.exception.status_code)
        self.assertEqual(
            'no-store', raised.exception.get_header('Cache-Control'))

    def test_initializeは秘密値を取得しない(self):
        self.module.initialize()

        self.source.get_admin_auth_json.assert_not_called()

    def test_Argon2idの承認済みparameterで検証する(self):
        self.assertEqual(2, self.module._password_hasher.time_cost)
        self.assertEqual(19 * 1024, self.module._password_hasher.memory_cost)
        self.assertEqual(1, self.module._password_hasher.parallelism)
        self.assertEqual(Type.ID, self.module._password_hasher.type)
        self.assertTrue(self.module.verify_credentials(
            'admin', 'correct-password'))
        self.assertFalse(self.module.verify_credentials(
            'admin', 'wrong-password'))

    def test_不明なuserでもpassword検証を一回行う(self):
        original = self.module._password_hasher
        hasher = Mock()
        hasher.verify.side_effect = original.verify
        self.module._password_hasher = hasher

        self.assertFalse(self.module.verify_credentials(
            'unknown', 'wrong-password'))

        hasher.verify.assert_called_once()

    def test_設定はprocess内でcacheする(self):
        self.module.verify_credentials('admin', 'correct-password')
        self.module.verify_credentials('admin', 'correct-password')

        self.source.get_admin_auth_json.assert_called_once_with()

    def test_不正な認証JSONをfail_closedにする(self):
        invalid_values = (
            '[]',
            '{}',
            '{"users":{},"session_secret":"' + SESSION_SECRET + '"}',
            json.dumps({
                'users': {'admin': 'not-an-argon2-hash'},
                'session_secret': SESSION_SECRET,
            }),
            json.dumps({
                'users': {'admin': self.password_hash},
                'session_secret': 'too-short',
            }),
        )
        for value in invalid_values:
            with self.subTest(value=value[:20]):
                self.module.reset_for_test()
                self.source.get_admin_auth_json.return_value = value
                self.module._credential_source = self.source
                with self.assertRaises(CredentialSourceError):
                    self.module.verify_credentials(
                        'admin', 'correct-password')

    def test_Argon2id以外のArgon2hashを拒否する(self):
        argon2i_hash = self.password_hash.replace(
            '$argon2id$', '$argon2i$', 1)
        self.source.get_admin_auth_json.return_value = json.dumps({
            'users': {'admin': argon2i_hash},
            'session_secret': SESSION_SECRET,
        })

        with self.assertRaisesRegex(CredentialSourceError, 'Argon2id'):
            self.module.verify_credentials('admin', 'correct-password')

    def test_署名CookieにuserとCSRFを保存して12時間で検証する(self):
        cookie, csrf_token = self.module.create_session('admin')
        self.request.cookies[self.module.SESSION_COOKIE_NAME] = cookie

        session = self.module.load_session()

        self.assertEqual('admin', session['username'])
        self.assertEqual(csrf_token, session['csrf_token'])
        self.assertEqual(12 * 60 * 60, self.module.SESSION_MAX_AGE_SECONDS)

    def test_改ざんCookieと期限切れCookieを拒否する(self):
        cookie, _ = self.module.create_session('admin')
        self.request.cookies[self.module.SESSION_COOKIE_NAME] = cookie + 'x'
        self.assert_http_error(401, self.module.load_session)

        self.request.cookies[self.module.SESSION_COOKIE_NAME] = cookie
        serializer = self.module._session_serializer()
        with patch.object(serializer, 'loads', side_effect=
                          self.module.SignatureExpired('expired')):
            self.assert_http_error(401, self.module.load_session)

    def test_Cookie属性を固定する(self):
        self.assertEqual(
            '__Secure-xsbot-dashboard', self.module.SESSION_COOKIE_NAME)
        fake_response = Mock()
        with patch.object(self.module, 'response', fake_response):
            self.module.set_session_cookie('signed-value')

        fake_response.set_cookie.assert_called_once_with(
            self.module.SESSION_COOKIE_NAME,
            'signed-value',
            max_age=12 * 60 * 60,
            path='/dashboard',
            secure=True,
            httponly=True,
            samesite='lax',
        )

    def test_状態変更はsession_Origin_CSRFの全てを要求する(self):
        cookie, csrf_token = self.module.create_session('admin')
        self.request.cookies[self.module.SESSION_COOKIE_NAME] = cookie
        endpoint = Mock(return_value='OK')
        wrapped = self.module.auth_required(
            state_changing=True)(endpoint)

        self.request.headers = {
            'Origin': 'https://app.example.test',
            'X-CSRF-Token': csrf_token,
        }
        self.assertEqual('OK', wrapped())
        self.assertEqual('admin', self.request.dashboard_user['username'])

        for headers in (
            {'X-CSRF-Token': csrf_token},
            {'Origin': 'https://attacker.example',
             'X-CSRF-Token': csrf_token},
            {'Origin': 'https://app.example.test'},
            {'Origin': 'https://app.example.test',
             'X-CSRF-Token': 'wrong'},
        ):
            with self.subTest(headers=headers):
                self.request.headers = headers
                self.assert_http_error(403, wrapped)

    def test_読取APIはOriginとCSRFを要求しない(self):
        cookie, _ = self.module.create_session('admin')
        self.request.cookies[self.module.SESSION_COOKIE_NAME] = cookie
        endpoint = Mock(return_value='OK')
        wrapped = self.module.auth_required()(endpoint)

        fake_response = Mock()
        with patch.object(self.module, 'response', fake_response):
            self.assertEqual('OK', wrapped())

        fake_response.set_header.assert_called_once_with(
            'Cache-Control', 'no-store')


if __name__ == '__main__':
    unittest.main()
