import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bottle import HTTPError


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class InvalidIdTokenError(Exception):
    pass


class ExpiredIdTokenError(InvalidIdTokenError):
    pass


class RevokedIdTokenError(InvalidIdTokenError):
    pass


def load_auth_middleware():
    """外部サービスを初期化せずに認証middlewareを読み込む。"""
    firebase_admin = types.ModuleType('firebase_admin')
    firebase_auth = types.ModuleType('firebase_admin.auth')
    firebase_credentials = types.ModuleType('firebase_admin.credentials')
    settings = types.ModuleType('settings')

    firebase_admin.initialize_app = Mock(return_value=object())
    firebase_auth.verify_id_token = Mock()
    firebase_auth.InvalidIdTokenError = InvalidIdTokenError
    firebase_auth.ExpiredIdTokenError = ExpiredIdTokenError
    firebase_auth.RevokedIdTokenError = RevokedIdTokenError
    firebase_credentials.Certificate = Mock(return_value=object())
    firebase_admin.auth = firebase_auth
    firebase_admin.credentials = firebase_credentials
    settings.AUTH_SETTINGS = {
        'firebase_credentials_path': '/tmp/test-firebase-key.json',
        'allowed_emails': ['admin@example.com'],
    }

    replacements = {
        'firebase_admin': firebase_admin,
        'firebase_admin.auth': firebase_auth,
        'firebase_admin.credentials': firebase_credentials,
        'settings': settings,
    }
    previous = {name: sys.modules.get(name) for name in replacements}
    module_name = '_test_auth_middleware'
    previous_module = sys.modules.get(module_name)
    try:
        sys.modules.update(replacements)
        spec = importlib.util.spec_from_file_location(
            module_name, PROJECT_ROOT / 'auth_middleware.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module

    return module, firebase_admin, firebase_auth, firebase_credentials, settings


class AuthMiddlewareTest(unittest.TestCase):
    def setUp(self):
        (self.module, self.firebase_admin, self.firebase_auth,
         self.firebase_credentials, self.settings) = load_auth_middleware()
        self.request = types.SimpleNamespace(headers={})
        self.module.request = self.request

    def protected_endpoint(self, endpoint=None):
        if endpoint is None:
            endpoint = Mock(return_value='OK')
        return self.module.auth_required()(endpoint), endpoint

    def assert_http_error(self, status_code, body, func):
        with self.assertRaises(HTTPError) as raised:
            func()
        self.assertEqual(raised.exception.status_code, status_code)
        self.assertEqual(raised.exception.body, body)

    def test_initialize_uses_only_explicit_certificate(self):
        self.module.initialize()
        self.module.initialize()

        self.firebase_credentials.Certificate.assert_called_once_with(
            '/tmp/test-firebase-key.json')
        self.firebase_admin.initialize_app.assert_called_once_with(
            self.firebase_credentials.Certificate.return_value)
        self.assertIs(self.module._app,
                      self.firebase_admin.initialize_app.return_value)

    def test_invalid_certificate_failure_is_not_hidden(self):
        self.firebase_credentials.Certificate.side_effect = ValueError('invalid key')

        with self.assertRaisesRegex(ValueError, 'invalid key'):
            self.module.initialize()
        self.firebase_admin.initialize_app.assert_not_called()

    def test_empty_allowlist_fails_when_decorator_is_applied(self):
        self.settings.AUTH_SETTINGS['allowed_emails'] = []

        with self.assertRaisesRegex(ValueError, 'allowed_emails'):
            self.module.auth_required()

    def test_empty_environment_value_fails_when_decorator_is_applied(self):
        self.settings.AUTH_SETTINGS['allowed_emails'] = ['']

        with self.assertRaisesRegex(ValueError, 'allowed_emails'):
            self.module.auth_required()

    def test_allowlist_is_captured_without_normalization(self):
        wrapped, endpoint = self.protected_endpoint()
        self.settings.AUTH_SETTINGS['allowed_emails'] = ['other@example.com']
        self.request.headers = {'Authorization': 'Bearer valid-token'}
        self.firebase_auth.verify_id_token.return_value = {
            'email': 'admin@example.com',
        }

        self.assertEqual(wrapped(), 'OK')
        endpoint.assert_called_once_with()

    def test_missing_or_wrong_scheme_is_rejected_before_sdk_call(self):
        wrapped, endpoint = self.protected_endpoint()

        for auth_header in ('', 'Basic token', 'bearer token'):
            with self.subTest(auth_header=auth_header):
                self.request.headers = {'Authorization': auth_header}
                self.assert_http_error(401, 'No token provided', wrapped)

        self.firebase_auth.verify_id_token.assert_not_called()
        endpoint.assert_not_called()

    def test_empty_and_whitespace_tokens_are_passed_unmodified(self):
        wrapped, endpoint = self.protected_endpoint()
        self.firebase_auth.verify_id_token.side_effect = InvalidIdTokenError()

        for auth_header, expected in (
                ('Bearer ', ''),
                ('Bearer  token\t', ' token\t')):
            with self.subTest(auth_header=auth_header):
                self.request.headers = {'Authorization': auth_header}
                self.assert_http_error(401, 'Invalid token', wrapped)
                self.firebase_auth.verify_id_token.assert_called_with(expected)

        endpoint.assert_not_called()

    def test_exception_classes_are_caught_from_specific_to_general(self):
        wrapped, endpoint = self.protected_endpoint()
        self.request.headers = {'Authorization': 'Bearer invalid-token'}

        for error, body in (
                (ExpiredIdTokenError(), 'Expired token'),
                (RevokedIdTokenError(), 'Revoked token'),
                (InvalidIdTokenError(), 'Invalid token')):
            with self.subTest(body=body):
                self.firebase_auth.verify_id_token.side_effect = error
                self.assert_http_error(401, body, wrapped)

        endpoint.assert_not_called()

    def test_generic_verification_error_is_fixed_and_logged(self):
        wrapped, endpoint = self.protected_endpoint()
        self.request.headers = {'Authorization': 'Bearer secret-token'}
        self.firebase_auth.verify_id_token.side_effect = RuntimeError(
            '内部の接続先を含む詳細')

        with patch.object(self.module.logging, 'exception') as log_exception:
            self.assert_http_error(401, 'Token verification failed', wrapped)

        log_exception.assert_called_once_with(
            'Firebase IDトークンの検証に失敗しました')
        self.assertNotIn('secret-token', str(log_exception.call_args))
        endpoint.assert_not_called()

    def test_email_match_is_exact_and_rejected_value_is_not_logged(self):
        wrapped, endpoint = self.protected_endpoint()
        self.request.headers = {'Authorization': 'Bearer valid-token'}

        for email in ('Admin@example.com', 'admin@example.com '):
            with self.subTest(email=email):
                self.firebase_auth.verify_id_token.return_value = {'email': email}
                self.firebase_auth.verify_id_token.side_effect = None
                with patch.object(self.module.logging, 'warning') as warning:
                    self.assert_http_error(403, 'Unauthorized email', wrapped)
                warning.assert_called_once_with(
                    '許可されていないアカウントからダッシュボードへアクセスされました')
                self.assertNotIn(email, str(warning.call_args))

        endpoint.assert_not_called()

    def test_decoded_user_is_stored_on_request(self):
        wrapped, endpoint = self.protected_endpoint()
        decoded = {'email': 'admin@example.com', 'uid': 'user-1'}
        self.request.headers = {'Authorization': 'Bearer valid-token'}
        self.firebase_auth.verify_id_token.return_value = decoded

        self.assertEqual(wrapped(), 'OK')

        self.firebase_auth.verify_id_token.assert_called_once_with('valid-token')
        self.assertIs(self.request.firebase_user, decoded)
        endpoint.assert_called_once_with()

    def test_endpoint_exception_is_not_converted_to_unauthorized(self):
        endpoint = Mock(side_effect=RuntimeError('endpoint failure'))
        wrapped, endpoint = self.protected_endpoint(endpoint)
        self.request.headers = {'Authorization': 'Bearer valid-token'}
        self.firebase_auth.verify_id_token.return_value = {
            'email': 'admin@example.com',
        }

        with self.assertRaisesRegex(RuntimeError, 'endpoint failure'):
            wrapped()

        endpoint.assert_called_once_with()

    def test_endpoint_http_error_keeps_its_status(self):
        endpoint = Mock(side_effect=HTTPError(404, 'not found'))
        wrapped, endpoint = self.protected_endpoint(endpoint)
        self.request.headers = {'Authorization': 'Bearer valid-token'}
        self.firebase_auth.verify_id_token.return_value = {
            'email': 'admin@example.com',
        }

        self.assert_http_error(404, 'not found', wrapped)
        endpoint.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
