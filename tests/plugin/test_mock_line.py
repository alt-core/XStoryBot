# coding: utf-8

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


TARGET = Path(__file__).resolve().parents[2] / 'plugin' / 'mock_line' / 'interface.py'


def load_mock_line_module():
    requests = types.ModuleType('requests')

    class RequestException(Exception):
        def __init__(self, *args, response=None):
            super().__init__(*args)
            self.response = response

    requests.RequestException = RequestException

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
    users = types.ModuleType('users')

    class User:
        def __init__(self, service, user_id):
            self.service = service
            self.user_id = user_id

    users.User = User
    hub = types.ModuleType('hub')
    hub.register_interface_factory = mock.Mock()
    utility = types.ModuleType('utility')
    utility.merge_params = lambda base, extra: {**base, **extra}

    module_name = 'tests_target_mock_line'
    spec = importlib.util.spec_from_file_location(module_name, TARGET)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
        'requests': requests,
        'context': context,
        'users': users,
        'hub': hub,
        'utility': utility,
        module_name: module,
    }):
        spec.loader.exec_module(module)
    return module, RequestException, User


class MockLinePluginTest(unittest.TestCase):
    def setUp(self):
        self.module, self.RequestException, self.User = load_mock_line_module()
        self.interface = self.module.MockLinePlugin_Interface('testbot', {
            'error_rate': 0,
            'rate_limit_threshold': 3,
            'logging_enabled': False,
            'retry_count': 2,
        })
        user = self.User('mock_line', 'user,U123')
        self.context = self.interface.create_context(user, 'action', {})
        self.reactions = [(('sender', 'message'), None)]

    def test_rate_limit_starts_at_the_threshold(self):
        with mock.patch.object(self.module.time, 'time', return_value=100.0), \
                mock.patch.object(self.module.time, 'sleep'), \
                mock.patch.object(self.module.random, 'random', return_value=1.0), \
                mock.patch.object(self.module.random, 'uniform', return_value=0.0):
            self.assertEqual(
                self.interface.respond_reaction(self.context, self.reactions), 'OK')
            self.assertEqual(
                self.interface.respond_reaction(self.context, self.reactions), 'OK')
            with self.assertRaises(self.RequestException) as captured:
                self.interface.respond_reaction(self.context, self.reactions)

        self.assertEqual(captured.exception.response.status_code, 429)
        self.assertEqual(self.interface.request_count, 3)

    def test_message_history_and_context_source_are_preserved(self):
        with mock.patch.object(self.module.time, 'time', return_value=200.0), \
                mock.patch.object(self.module.time, 'sleep'), \
                mock.patch.object(self.module.random, 'random', return_value=1.0), \
                mock.patch.object(self.module.random, 'uniform', return_value=0.0):
            self.interface.respond_reaction(self.context, self.reactions)

        message = self.interface.get_last_message()
        self.assertEqual(message['context']['source_type'], 'user')
        self.assertEqual(message['context']['source_id'], 'U123')
        self.assertEqual(message['context']['action'], 'action')
        self.assertEqual(message['reactions'], self.reactions)
        self.assertEqual(self.interface.get_retry_count(), 2)

        self.interface.clear_messages()
        self.assertIsNone(self.interface.get_last_message())

    def test_forced_rate_limit_uses_429_response(self):
        self.interface.set_force_error('rate_limit')
        with mock.patch.object(self.module.time, 'time', return_value=300.0):
            with self.assertRaises(self.RequestException) as captured:
                self.interface.respond_reaction(self.context, self.reactions)
        self.assertEqual(captured.exception.response.status_code, 429)
        self.assertEqual(self.interface.error_count, 1)


if __name__ == '__main__':
    unittest.main()
