# coding: utf-8

import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


class AbortError(Exception):
    def __init__(self, status, body):
        super().__init__(status, body)
        self.status = status
        self.body = body


class FakeBottle:
    def route(self, *_args, **_kwargs):
        return lambda func: func

    def post(self, *_args, **_kwargs):
        return lambda func: func


class ResponseState:
    def __init__(self):
        self.headers = {}
        self.content_type = None
        self.status = 200


class User:
    def __init__(self, service, user_id):
        self.service = service
        self.user_id = user_id

    def __str__(self):
        return f'{self.service}:{self.user_id}'


def load_liff_webapi():
    request = types.SimpleNamespace(headers={}, json=None)
    response = ResponseState()
    bottle = types.ModuleType('bottle')
    bottle.request = request
    bottle.response = response
    bottle.Bottle = FakeBottle
    bottle.abort = lambda status, body=None: (_ for _ in ()).throw(AbortError(status, body))

    requests = types.ModuleType('requests')
    requests.get = mock.Mock()
    auth = types.ModuleType('auth')
    utility = types.ModuleType('utility')
    utility.make_error_json = lambda code, msg: {'code': code, 'message': msg}
    utility.make_ok_json = lambda value: ('ok', value)
    utility.make_ng_json = lambda value: ('ng', value)
    main = types.ModuleType('main')
    main.get_bot = mock.Mock()
    users = types.ModuleType('users')
    users.User = User

    module_name = 'tests_target_liff_webapi'
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / 'plugin' / 'liff' / 'webapi.py')
    module = importlib.util.module_from_spec(spec)
    replacements = {
        'bottle': bottle,
        'requests': requests,
        'auth': auth,
        'utility': utility,
        'main': main,
        'users': users,
        module_name: module,
    }
    with mock.patch.dict(sys.modules, replacements):
        spec.loader.exec_module(module)
    return module, request, response, requests, main


def load_liff_interface():
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

    context.ActionContext = ActionContext

    module_name = 'tests_target_liff_interface'
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / 'plugin' / 'liff' / 'interface.py')
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
        'hub': hub,
        'commands': commands,
        'utility': utility,
        'context': context,
        module_name: module,
    }):
        spec.loader.exec_module(module)
    return module


class ProfileResponse:
    def __init__(self, status_code=200, data=None, error=None):
        self.status_code = status_code
        self.data = data
        self.error = error

    def json(self):
        if self.error is not None:
            raise self.error
        return self.data


class FakeInterface:
    allow_origin = 'https://liff.example.invalid'
    action_prefix = '##liff.'

    def __init__(self):
        self.context_args = None

    def create_context(self, user, action, attrs):
        self.context_args = (user, action, attrs)
        return types.SimpleNamespace(user=user, action=action, attrs=attrs)


class FakeBot:
    def __init__(self, result='result-json'):
        self.interface = FakeInterface()
        self.result = result
        self.reload_calls = 0
        self.context = None

    def get_interface(self, name):
        return self.interface if name == 'liff' else None

    def check_reload(self):
        self.reload_calls += 1

    def handle_action(self, context):
        self.context = context
        return self.result


class LiffWebAPITest(unittest.TestCase):
    def setUp(self):
        (self.module, self.request, self.response,
         self.requests, self.main) = load_liff_webapi()
        self.bot = FakeBot()
        self.main.get_bot.return_value = self.bot
        self.request.headers = {'Authorization': 'Bearer access-token'}
        self.request.json = {'action': '選択/1'}
        self.requests.get.return_value = ProfileResponse(
            data={'userId': 'U1234567890'})

    def test_success_keeps_timeout_json_semantics_and_logs(self):
        with self.assertLogs(level='INFO') as captured:
            result = self.module.send_message('testbot')

        self.assertEqual(result, ('ok', 'result-json'))
        self.requests.get.assert_called_once_with(
            'https://api.line.me/v2/profile',
            headers={
                'Content-Type': 'application/json; charset=UTF-8',
                'Authorization': 'Bearer access-token',
            },
            timeout=120,
        )
        user, action, attrs = self.bot.interface.context_args
        self.assertEqual((user.service, user.user_id), ('line', 'user,U1234567890'))
        self.assertEqual(action, '##liff.選択/1')
        self.assertEqual(attrs, {})
        self.assertEqual(self.bot.reload_calls, 1)
        self.assertEqual(
            self.response.headers['Access-Control-Allow-Origin'],
            'https://liff.example.invalid',
        )
        output = '\n'.join(captured.output)
        self.assertIn('LIFF send_message: U1234567890 ##liff.選択/1', output)
        self.assertIn('LIFF result: result-json', output)

    def test_profile_and_json_errors_propagate(self):
        self.requests.get.side_effect = RuntimeError('接続失敗')
        with self.assertRaisesRegex(RuntimeError, '接続失敗'):
            self.module.send_message('testbot')

        self.requests.get.side_effect = None
        self.requests.get.return_value = ProfileResponse(error=ValueError('不正JSON'))
        with self.assertRaisesRegex(ValueError, '不正JSON'):
            self.module.send_message('testbot')

    def test_existing_bearer_parser_and_none_result_are_preserved(self):
        self.request.headers = {'Authorization': 'Bearer'}
        with self.assertRaises(IndexError):
            self.module.send_message('testbot')

        self.request.headers = {'Authorization': 'Bearer access-token'}
        self.bot.result = None
        self.assertEqual(
            self.module.send_message('testbot'),
            ('ng', 'Error occurred'),
        )

    def test_missing_input_json_returns_existing_bad_request(self):
        self.request.json = None
        result = self.module.send_message('testbot')
        self.assertEqual(result, 'Bad Request')
        self.assertEqual(self.response.status, 400)
        self.assertEqual(self.bot.reload_calls, 1)


class LiffInterfaceTest(unittest.TestCase):
    def test_plain_reactions_are_returned_as_json_array(self):
        module = load_liff_interface()
        interface = module.LiffPlugin_Interface('testbot', {
            'allow_origin': 'https://liff.example.invalid',
        })
        context = types.SimpleNamespace(response=None)

        result = interface.respond_reaction(context, [
            (('sender', 'こんにちは'), None),
            ((None, '次の行'), None),
        ])

        self.assertEqual(json.loads(result), ['こんにちは', '次の行'])
        self.assertEqual(interface.action_prefix, '##liff.')
        self.assertEqual(interface.get_retry_count(), 3)


if __name__ == '__main__':
    unittest.main()
