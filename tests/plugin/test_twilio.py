# coding: utf-8

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock
from urllib.parse import quote

from twilio.request_validator import RequestValidator
from webtest import TestApp


ROOT = Path(__file__).resolve().parents[2]
AUTH_TOKEN = 'twilio-auth-token'
APP_BASE_URL = 'https://app.example.invalid'


def load_twilio_webapi():
    """外部サービスを初期化せずにTwilio Web APIを読み込む。"""
    auth = types.ModuleType('auth')
    auth.check_token = mock.Mock(
        side_effect=lambda token: token == 'internal-api-token')
    main = types.ModuleType('main')
    main.get_bot = mock.Mock()
    settings = types.ModuleType('settings')
    settings.GCP_SETTINGS = {
        'services': {'app': {'base_url': APP_BASE_URL}},
    }

    module_name = 'tests_target_twilio_webapi'
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / 'plugin' / 'twilio' / 'webapi.py')
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
        'auth': auth,
        'main': main,
        'settings': settings,
        module_name: module,
    }):
        spec.loader.exec_module(module)
    return module, auth, main


def load_twilio_interface():
    """Twilio SDKの送信処理を代用品へ差し替えてinterfaceを読み込む。"""
    twilio = types.ModuleType('twilio')
    twilio_rest = types.ModuleType('twilio.rest')
    twilio_rest.Client = mock.Mock()
    twilio.rest = twilio_rest

    hub = types.ModuleType('hub')
    hub.register_interface_factory = mock.Mock()
    commands = types.ModuleType('commands')
    commands.invoke_runtime_construct_response = mock.Mock(return_value=False)
    utility = types.ModuleType('utility')
    utility.merge_params = lambda base, extra: {**base, **extra}

    context = types.ModuleType('context')

    class ActionContext:
        def __init__(self, bot_name, service, interface, user, action, attrs):
            self.bot_name = bot_name
            self.service = service
            self.interface = interface
            self.user = user
            self.action = action
            self.attrs = attrs

        def get_interface(self, _name):
            return self.interface

    context.ActionContext = ActionContext

    users = types.ModuleType('users')

    class User:
        def __init__(self, service, user_id):
            self.service = service
            self.user_id = user_id

    users.User = User

    module_name = 'tests_target_twilio_interface'
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / 'plugin' / 'twilio' / 'interface.py')
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
        'twilio': twilio,
        'twilio.rest': twilio_rest,
        'hub': hub,
        'commands': commands,
        'utility': utility,
        'context': context,
        'users': users,
        module_name: module,
    }):
        spec.loader.exec_module(module)
    return module, twilio_rest, commands


def load_twilio_default_commands():
    """Twilioクライアントを呼ばずに既定コマンドを読み込む。"""
    settings = types.ModuleType('settings')
    settings.GCP_SETTINGS = {
        'services': {'app': {'base_url': f'{APP_BASE_URL}/'}},
    }
    hub = types.ModuleType('hub')
    hub.register_handler = mock.Mock()
    commands = types.ModuleType('commands')
    commands.Default_Builder = object
    commands.CommandEntry = lambda **kwargs: kwargs
    commands.register_commands = mock.Mock()

    module_name = 'tests_target_twilio_default_commands'
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / 'plugin' / 'twilio' / 'default_commands.py')
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
        'settings': settings,
        'hub': hub,
        'commands': commands,
        module_name: module,
    }):
        spec.loader.exec_module(module)
    return module


class FakeTwilioTarget:
    def __init__(self):
        self.interface = mock.Mock()
        self.interface.params = {'twilio_auth_token': AUTH_TOKEN}
        self.context = object()
        self.interface.create_context_from_twilio_event.return_value = self.context
        self.bot = mock.Mock()
        self.bot.get_interface.return_value = self.interface
        self.bot.handle_action.return_value = '<Response>handled</Response>'


class TwilioWebAPITest(unittest.TestCase):
    def setUp(self):
        self.module, self.auth, self.main = load_twilio_webapi()
        self.client = TestApp(self.module.app)
        self.target = FakeTwilioTarget()
        self.main.get_bot.return_value = self.target.bot

    def signed_post(self, path, params, expect_errors=False, signature=None,
                    headers=None):
        external_url = f'{APP_BASE_URL}{path}'
        if signature is None:
            signature = RequestValidator(AUTH_TOKEN).compute_signature(
                external_url, params)
        request_headers = {'X-Twilio-Signature': signature}
        if headers:
            request_headers.update(headers)
        return self.client.post(
            path,
            params=params,
            headers=request_headers,
            expect_errors=expect_errors,
        )

    def reset_target_calls(self):
        self.target.bot.reset_mock()
        self.target.interface.reset_mock()
        self.target.interface.params = {'twilio_auth_token': AUTH_TOKEN}
        self.target.interface.create_context_from_twilio_event.return_value = (
            self.target.context)
        self.target.bot.get_interface.return_value = self.target.interface
        self.target.bot.handle_action.return_value = '<Response>handled</Response>'

    def test_external_routes_require_valid_twilio_signature(self):
        routes = [
            ('/twilio/callback/testbot?token=internal-api-token', {
                'From': '+819000000000',
                'To': '+815000000000',
                'Body': 'hello',
            }),
            ('/twilio/dial_content/testbot/action', {
                'From': '+815000000000',
                'To': '+819000000000',
            }),
            ('/twilio/dial_completed_callback/testbot/action', {
                'From': '+815000000000',
                'To': '+819000000000',
                'CallStatus': 'completed',
                'CallDuration': '2',
            }),
        ]

        for path, params in routes:
            with mock.patch.object(self.module.logging, 'info') as log_info:
                with self.subTest(path=path, signature='missing'):
                    response = self.client.post(
                        path, params=params, expect_errors=True)
                    self.assertEqual(response.status_int, 403)
                with self.subTest(path=path, signature='invalid'):
                    response = self.signed_post(
                        path, params, signature='invalid', expect_errors=True)
                    self.assertEqual(response.status_int, 403)
                log_info.assert_not_called()
            self.reset_target_calls()

    def test_ascii_japanese_and_emoji_forms_use_official_signature(self):
        for message in ('hello', '日本語の本文', '絵文字🙂'):
            with self.subTest(message=message):
                params = {
                    'From': '+819000000000',
                    'To': '+815000000000',
                    'Body': message,
                }
                with self.assertLogs(level='INFO') as captured:
                    response = self.signed_post(
                        '/twilio/callback/testbot', params)

                self.assertEqual(response.status_int, 200)
                self.target.interface.create_context_from_twilio_event.assert_called_once_with(
                    '+819000000000', '+815000000000', False, message)
                self.assertIn(
                    'Twilio callback:', '\n'.join(captured.output))
                self.assertIn(
                    'From=%2B819000000000', '\n'.join(captured.output))
                self.reset_target_calls()

    def test_query_url_is_signed_and_business_values_use_request_params(self):
        path = '/twilio/callback/testbot?Body=query-action'
        params = {
            'From': '+819000000000',
            'To': '+815000000000',
        }

        response = self.signed_post(path, params)

        self.assertEqual(response.status_int, 200)
        self.target.interface.create_context_from_twilio_event.assert_called_once_with(
            '+819000000000', '+815000000000', False, 'query-action')

    def test_message_notification_returns_ok_only_after_signature_check(self):
        params = {'Message': 'queued'}
        with mock.patch.object(self.module.logging, 'info') as log_info:
            response = self.signed_post('/twilio/callback/testbot', params)

        self.assertEqual(response.text, 'OK')
        self.target.bot.check_reload.assert_not_called()
        self.target.bot.handle_action.assert_not_called()
        log_info.assert_not_called()

        response = self.client.post(
            '/twilio/callback/testbot',
            params=params,
            expect_errors=True,
        )
        self.assertEqual(response.status_int, 403)

    def test_sms_voice_callback_order_and_existing_log_are_preserved(self):
        order = []
        self.target.bot.check_reload.side_effect = lambda: order.append('reload')

        def create_context(*_args):
            order.append('context')
            return self.target.context

        def handle_action(_context):
            order.append('handle')
            return '<Response>handled</Response>'

        self.target.interface.create_context_from_twilio_event.side_effect = (
            create_context)
        self.target.bot.handle_action.side_effect = handle_action
        params = {
            'From': '+819000000000',
            'To': '+815000000000',
            'CallSid': 'CA00000000000000000000000000000000',
            'SpeechResult': '音声入力',
        }

        with mock.patch.object(
                self.module.logging, 'info',
                side_effect=lambda *_args: order.append('log')) as log_info:
            response = self.signed_post('/twilio/callback/testbot', params)

        self.assertEqual(response.status_int, 200)
        self.assertEqual(order, ['log', 'reload', 'context', 'handle'])
        log_info.assert_called_once()
        self.target.interface.create_context_from_twilio_event.assert_called_once_with(
            '+819000000000', '+815000000000', True, '音声入力')

    def test_missing_from_or_to_is_not_changed_to_new_400_response(self):
        missing_to = {
            'From': '+819000000000',
            'Body': 'hello',
        }
        response = self.signed_post('/twilio/callback/testbot', missing_to)
        self.assertEqual(response.status_int, 200)
        self.target.interface.create_context_from_twilio_event.assert_called_once_with(
            '+819000000000', None, False, 'hello')

        self.reset_target_calls()
        missing_from = {
            'To': '+815000000000',
            'Body': 'hello',
        }
        response = self.signed_post(
            '/twilio/callback/testbot', missing_from, expect_errors=True)
        self.assertEqual(response.status_int, 500)

    def test_anonymous_number_rejection_still_runs_after_reload(self):
        params = {
            'From': 'anonymous',
            'To': '+815000000000',
            'CallSid': 'CA00000000000000000000000000000000',
        }

        response = self.signed_post('/twilio/callback/testbot', params)

        self.assertEqual(response.status_int, 200)
        self.assertIn('<Reject reason="rejected"></Reject>', response.text)
        self.target.bot.check_reload.assert_called_once_with()
        self.target.interface.create_context_from_twilio_event.assert_not_called()

    def test_dial_content_keeps_phone_reversal_and_decoded_message(self):
        message = 'next-scene'
        path = f'/twilio/dial_content/testbot/{quote(message, safe="")}'
        params = {
            'From': '+815000000000',
            'To': '+819000000000',
        }

        response = self.signed_post(path, params)

        self.assertEqual(response.status_int, 200)
        self.target.interface.create_context_from_twilio_event.assert_called_once_with(
            '+819000000000', '+815000000000', True, message)

    def test_dial_completion_keeps_duration_boundary_and_callback_order(self):
        cases = [
            ({'CallStatus': 'completed'}, 'finished:NG'),
            ({'CallStatus': 'completed', 'CallDuration': '1'}, 'finished:NG'),
            ({'CallStatus': 'completed', 'CallDuration': '2'}, 'finished:OK'),
            ({'CallStatus': 'busy', 'CallDuration': '99'}, 'finished:NG'),
        ]
        for status_params, expected_action in cases:
            with self.subTest(status_params=status_params):
                params = {
                    'From': '+815000000000',
                    'To': '+819000000000',
                    **status_params,
                }
                response = self.signed_post(
                    '/twilio/dial_completed_callback/testbot/finished',
                    params,
                )

                self.assertEqual(response.status_int, 200)
                self.target.interface.create_context_from_twilio_event.assert_called_once_with(
                    '+819000000000', '+815000000000', True,
                    expected_action)
                self.reset_target_calls()

    def test_invalid_call_duration_is_not_silently_changed_to_ng(self):
        params = {
            'From': '+815000000000',
            'To': '+819000000000',
            'CallStatus': 'completed',
            'CallDuration': 'invalid',
        }

        response = self.signed_post(
            '/twilio/dial_completed_callback/testbot/finished',
            params,
            expect_errors=True,
        )

        self.assertEqual(response.status_int, 500)
        self.target.bot.handle_action.assert_not_called()

    def test_internal_callback_requires_header_token_and_uses_request_params(self):
        path = '/twilio/internal_callback/testbot?Message=query-action'
        params = {
            'From': '+819000000000',
            'To': '+815000000000',
            'token': 'internal-api-token',
        }

        response = self.client.post(path, params=params, expect_errors=True)
        self.assertEqual(response.status_int, 401)
        self.main.get_bot.assert_not_called()

        response = self.client.post(
            path,
            params=params,
            headers={'X-API-Token': 'invalid'},
            expect_errors=True,
        )
        self.assertEqual(response.status_int, 401)

        response = self.client.post(
            path,
            params=params,
            headers={'X-API-Token': 'internal-api-token'},
        )
        self.assertEqual(response.status_int, 200)
        self.target.interface.create_context_from_twilio_event.assert_called_once_with(
            '+819000000000', '+815000000000', False, 'query-action')


class TwilioInterfaceTest(unittest.TestCase):
    def test_public_setting_keys_are_mapped_without_prevalidating_client(self):
        module, twilio_rest, _commands = load_twilio_interface()
        params = {
            'sid': 'account-sid',
            'auth_token': 'auth-token',
            'phone_number': '+815000000000',
        }
        interface = module.TwilioPlugin_Interface('testbot', params)

        interface.get_twilio_client()

        twilio_rest.Client.assert_called_once_with('account-sid', 'auth-token')
        self.assertEqual(interface.params['sms_from'], '+815000000000')
        self.assertEqual(interface.params['dial_from'], '+815000000000')
        self.assertNotIn('twilio_sid', params)

        twilio_rest.Client.reset_mock()
        empty_interface = module.TwilioPlugin_Interface('testbot', {})
        empty_interface.get_twilio_client()
        twilio_rest.Client.assert_called_once_with('', '')

    def test_voice_action_and_twiml_meaning_are_preserved(self):
        module, _twilio_rest, _commands = load_twilio_interface()
        interface = module.TwilioPlugin_Interface('testbot', {})

        context = interface.create_context_from_twilio_event(
            '+819000000000', '+815000000000', True, None)
        self.assertEqual(context.action, '#tel:+815000000000')
        self.assertEqual(interface.get_retry_count(), 3)

        context.is_voicecall = False
        result = interface.respond_reaction(context, [
            (('案内役', 'こんにちは'), None),
        ])
        self.assertEqual(
            result,
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response><Message>案内役:\nこんにちは</Message></Response>',
        )


class TwilioDefaultCommandsTest(unittest.TestCase):
    def setUp(self):
        self.module = load_twilio_default_commands()
        self.runtime = self.module.TwilioDefaultCommandsPlugin_Runtime()
        self.client = mock.Mock()
        self.interface = mock.Mock()
        self.interface.bot_name = 'testbot'
        self.interface.params = {
            'sms_from': '+815000000001',
            'dial_from': '+815000000002',
        }
        self.interface.get_twilio_client.return_value = self.client
        self.context = mock.Mock()
        self.context.from_tel = '+819000000000'
        self.context.get_interface.return_value = self.interface

    def test_dial_uses_external_base_url_python3_quote_and_existing_log(self):
        with self.assertLogs(level='INFO') as captured:
            self.runtime.run_command(
                self.context,
                None,
                '@dial',
                ['次の場面/一', '完了/通知'],
            )

        self.client.calls.create.assert_called_once_with(
            to='+819000000000',
            from_='+815000000002',
            url=(
                f'{APP_BASE_URL}/twilio/dial_content/testbot/'
                '%E6%AC%A1%E3%81%AE%E5%A0%B4%E9%9D%A2%2F%E4%B8%80'
            ),
            timeout=5,
            status_callback=(
                f'{APP_BASE_URL}/twilio/dial_completed_callback/testbot/'
                '%E5%AE%8C%E4%BA%86%2F%E9%80%9A%E7%9F%A5'
            ),
            status_callback_event=['completed'],
        )
        output = '\n'.join(captured.output)
        self.assertIn('TwiML url:', output)
        self.assertIn(APP_BASE_URL, output)

    def test_sms_keeps_existing_destination_and_sender(self):
        self.runtime.run_command(
            self.context, None, '@sms', ['送信本文'])

        self.client.messages.create.assert_called_once_with(
            to='+819000000000',
            from_='+815000000001',
            body='送信本文',
        )


if __name__ == '__main__':
    unittest.main()
