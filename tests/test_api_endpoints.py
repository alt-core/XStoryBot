import builtins
import importlib.util
import io
import json
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from unittest.mock import Mock, call, patch
from wsgiref.util import setup_testing_defaults

from bottle import Bottle


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestResponse:
    def __init__(self, status_int, headers, body):
        self.status_int = status_int
        self.headers = dict(headers)
        self.body = body

    @property
    def text(self):
        return self.body.decode('utf-8')

    @property
    def json(self):
        return json.loads(self.text)


class TestApp:
    """標準WSGIだけを使う、外部接続なしの小さなHTTP test client。"""
    def __init__(self, app):
        self.app = app

    def request(self, method, path, params=None, headers=None,
                expect_errors=False):
        parsed = urlsplit(path)
        query = parsed.query
        body = b''
        if params:
            encoded = urlencode(params, doseq=True)
            if method in ('GET', 'HEAD', 'OPTIONS'):
                query = '&'.join(value for value in (query, encoded) if value)
            else:
                body = encoded.encode('utf-8')

        environ = {}
        setup_testing_defaults(environ)
        environ['REQUEST_METHOD'] = method
        environ['PATH_INFO'] = parsed.path
        environ['QUERY_STRING'] = query
        environ['wsgi.input'] = io.BytesIO(body)
        environ['CONTENT_LENGTH'] = str(len(body))
        if body:
            environ['CONTENT_TYPE'] = 'application/x-www-form-urlencoded'
        for name, value in (headers or {}).items():
            key = name.upper().replace('-', '_')
            if key in ('CONTENT_TYPE', 'CONTENT_LENGTH'):
                environ[key] = value
            else:
                environ[f'HTTP_{key}'] = value

        captured = {}

        def start_response(status, response_headers, exc_info=None):
            captured['status'] = status
            captured['headers'] = response_headers

        result = self.app(environ, start_response)
        try:
            response_body = b''.join(result)
        finally:
            close = getattr(result, 'close', None)
            if close:
                close()

        status_int = int(captured['status'].split(' ', 1)[0])
        if status_int >= 400 and not expect_errors:
            raise AssertionError(f'予期しないHTTPエラー: {captured["status"]}')
        return TestResponse(status_int, captured['headers'], response_body)

    def get(self, path, params=None, headers=None, expect_errors=False):
        return self.request('GET', path, params, headers, expect_errors)

    def post(self, path, params=None, headers=None, expect_errors=False):
        return self.request('POST', path, params, headers, expect_errors)

    def options(self, path, params=None, headers=None, expect_errors=False):
        return self.request('OPTIONS', path, params, headers, expect_errors)


def load_module(module_name, filename, replacements):
    """指定した依存moduleだけを差し替えてproduction moduleを読み込む。"""
    previous = {name: sys.modules.get(name) for name in replacements}
    previous_module = sys.modules.get(module_name)
    try:
        sys.modules.update(replacements)
        spec = importlib.util.spec_from_file_location(
            module_name, PROJECT_ROOT / filename)
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
    return module


class FakeUser:
    def __init__(self, service_name, user_id):
        self.service_name = service_name
        self.user_id = user_id

    @classmethod
    def deserialize(cls, value):
        if not value or ':' not in value:
            return None
        service_name, user_id = value.split(':', 1)
        if not service_name or not user_id:
            return None
        return cls(service_name, user_id)

    def __str__(self):
        return f'{self.service_name}:{self.user_id}'


class FakeInterface:
    def create_context(self, user, action, attrs):
        return types.SimpleNamespace(user=user, action=action, attrs=attrs)


class FakeBot:
    def __init__(self):
        self.check_reload = Mock()
        self.contexts = []

    def get_interface(self, service_name):
        if service_name == 'plaintext':
            return FakeInterface()
        return None

    def handle_action(self, context):
        self.contexts.append(context)
        return f'{context.user.user_id}\n'


class WebApiTest(unittest.TestCase):
    def setUp(self):
        self.main = types.ModuleType('main')
        self.auth = types.ModuleType('auth')
        self.settings = types.ModuleType('settings')
        self.users = types.ModuleType('users')
        self.bot = FakeBot()

        self.main.get_bot = Mock(return_value=self.bot)
        self.auth.check_token = Mock(side_effect=lambda token: token == 'valid-token')
        self.settings.OPTIONS = {}
        self.users.User = FakeUser
        self.users.get_group_members = Mock(return_value=[])

        self.module = load_module(
            '_test_webapi',
            'webapi.py',
            {
                'main': self.main,
                'auth': self.auth,
                'settings': self.settings,
                'users': self.users,
            },
        )
        self.client = TestApp(self.module.app)

    def action_params(self, **overrides):
        params = {
            'user': 'plaintext:user-1',
            'action': 'hello',
            'token': 'valid-token',
        }
        params.update(overrides)
        return params

    def assert_success_response(self, response):
        self.assertEqual(response.status_int, 200)
        self.assertEqual(response.headers['Content-Type'],
                         'text/plain; charset=utf-8')
        self.assertEqual(json.loads(response.text)['result'], 'Success')

    def test_get_and_post_share_action_contract(self):
        get_response = self.client.get(
            '/api/v1/bots/bot/action', params=self.action_params())
        post_response = self.client.post(
            '/api/v1/bots/bot/action', params=self.action_params())

        self.assert_success_response(get_response)
        self.assert_success_response(post_response)
        self.assertEqual([context.action for context in self.bot.contexts],
                         ['hello', 'hello'])

    def test_header_is_preferred_and_parameter_is_fallback(self):
        fallback_response = self.client.post(
            '/api/v1/bots/bot/action', params=self.action_params())
        rejected_response = self.client.post(
            '/api/v1/bots/bot/action',
            params=self.action_params(),
            headers={'X-API-Token': 'invalid-token'},
            expect_errors=True,
        )
        empty_header_response = self.client.post(
            '/api/v1/bots/bot/action',
            params=self.action_params(),
            headers={'X-API-Token': ''},
            expect_errors=True,
        )
        header_response = self.client.post(
            '/api/v1/bots/bot/action',
            params=self.action_params(token='invalid-token'),
            headers={'X-API-Token': 'valid-token'},
        )

        self.assert_success_response(fallback_response)
        self.assertEqual(rejected_response.status_int, 401)
        self.assertEqual(empty_header_response.status_int, 401)
        self.assert_success_response(header_response)

    def test_bot_lookup_precedes_token_validation(self):
        self.main.get_bot.return_value = None

        response = self.client.get(
            '/api/v1/bots/missing/action', expect_errors=True)

        self.assertEqual(response.status_int, 404)
        self.auth.check_token.assert_not_called()

    def test_detail_log_is_written_only_after_valid_token(self):
        with patch.object(self.module.logging, 'info') as info:
            response = self.client.post(
                '/api/v1/bots/bot/action',
                params=self.action_params(token='invalid-token'),
                expect_errors=True,
            )
        self.assertEqual(response.status_int, 401)
        info.assert_not_called()

        with patch.object(self.module.logging, 'info') as info:
            response = self.client.post(
                '/api/v1/bots/bot/action',
                params=self.action_params(user='invalid-user'),
                expect_errors=True,
            )
        self.assertEqual(response.status_int, 400)
        info.assert_called_once_with(
            'API call: bot_name: bot, user: invalid-user, action: hello')

    def test_missing_empty_and_whitespace_actions_are_preserved(self):
        missing_params = self.action_params()
        missing_params.pop('action')

        self.assert_success_response(self.client.post(
            '/api/v1/bots/bot/action', params=missing_params))
        self.assert_success_response(self.client.post(
            '/api/v1/bots/bot/action',
            params=self.action_params(action='')))
        self.assert_success_response(self.client.post(
            '/api/v1/bots/bot/action',
            params=self.action_params(action='  hello  ')))

        self.assertEqual([context.action for context in self.bot.contexts],
                         ['', '', '  hello  '])

    def test_group_results_skip_unknown_interface_and_wait_each_member(self):
        self.users.get_group_members.return_value = [
            FakeUser('plaintext', 'first'),
            FakeUser('unknown', 'skip'),
            FakeUser('plaintext', 'second'),
        ]
        result = []

        with patch.object(self.module.time, 'sleep') as sleep:
            self.module._do_action_iter(
                result, self.bot, FakeUser('group', 'group-1'), 'hello', {})

        self.assertEqual(''.join(result), 'first\nsecond\n')
        self.assertEqual(sleep.call_args_list,
                         [call(0.1), call(0.1), call(0.1)])

    def test_group_skips_unknown_interface_at_edges_and_nested_group(self):
        self.settings.OPTIONS = {'group_interval': 0}

        for members in (
            [FakeUser('unknown', 'skip'), FakeUser('plaintext', 'first')],
            [FakeUser('plaintext', 'first'), FakeUser('unknown', 'skip')],
        ):
            with self.subTest(members=[str(member) for member in members]):
                self.users.get_group_members.return_value = members
                result = []
                self.module._do_action_iter(
                    result, self.bot, FakeUser('group', 'group-1'),
                    'hello', {}
                )
                self.assertEqual(''.join(result), 'first\n')

        def get_group_members(group_id):
            if group_id == 'group-1':
                return [
                    FakeUser('plaintext', 'first'),
                    FakeUser('group', 'nested'),
                    FakeUser('plaintext', 'second'),
                ]
            if group_id == 'nested':
                return [
                    FakeUser('unknown', 'skip'),
                    FakeUser('plaintext', 'nested-member'),
                ]
            return []

        self.users.get_group_members.side_effect = get_group_members
        result = []
        self.module._do_action_iter(
            result, self.bot, FakeUser('group', 'group-1'), 'hello', {}
        )
        self.assertEqual(''.join(result), 'first\nnested-member\nsecond\n')

    def test_direct_unknown_interface_returns_not_found(self):
        response = self.client.post(
            '/api/v1/bots/bot/action',
            params=self.action_params(user='unknown:user-1'),
            expect_errors=True,
        )

        self.assertEqual(response.status_int, 404)

    def test_unhandled_action_exception_remains_server_error(self):
        self.bot.handle_action = Mock(side_effect=RuntimeError('action failure'))

        response = self.client.post(
            '/api/v1/bots/bot/action',
            params=self.action_params(),
            expect_errors=True,
        )

        self.assertEqual(response.status_int, 500)

    def test_group_interval_setting_and_zero_are_preserved(self):
        self.users.get_group_members.return_value = [
            FakeUser('plaintext', 'first'),
            FakeUser('plaintext', 'second'),
        ]

        self.settings.OPTIONS = {'group_interval': 250}
        with patch.object(self.module.time, 'sleep') as sleep:
            self.module._do_action_iter(
                [], self.bot, FakeUser('group', 'group-1'), 'hello', {})
        self.assertEqual(sleep.call_args_list, [call(0.25), call(0.25)])

        self.settings.OPTIONS = {'group_interval': 0}
        with patch.object(self.module.time, 'sleep') as sleep:
            self.module._do_action_iter(
                [], self.bot, FakeUser('group', 'group-1'), 'hello', {})
        sleep.assert_not_called()

    def test_group_batch_requires_header_token_before_bot_lookup(self):
        response = self.client.post(
            '/api/v1/bots/bot/process_group_batch',
            params={
                'message_task_id': 'task-1',
                'batch_index': '0',
                'token': 'valid-token',
            },
            expect_errors=True,
        )

        self.assertEqual(response.status_int, 401)
        self.main.get_bot.assert_not_called()

    def test_group_batch_processes_with_valid_header(self):
        manager_module = types.ModuleType('group_message_task_manager')
        manager = Mock()
        manager.handle_batch_process_request.return_value = (
            {'message': '処理完了', 'status': 'completed'}, 200)
        manager_class = Mock(return_value=manager)
        manager_module.GroupMessageTaskManager = manager_class

        with patch.dict(sys.modules, {
                'group_message_task_manager': manager_module}):
            response = self.client.post(
                '/api/v1/bots/bot/process_group_batch',
                params={'message_task_id': 'task-1', 'batch_index': '2'},
                headers={'X-API-Token': 'valid-token'},
            )

        self.assertEqual(response.status_int, 200)
        manager_class.assert_called_once_with('bot', bot_instance=self.bot)
        manager.handle_batch_process_request.assert_called_once_with('task-1', 2)
        self.bot.check_reload.assert_called_once_with()

    def test_group_batch_options_is_unauthenticated(self):
        response = self.client.options(
            '/api/v1/bots/bot/process_group_batch')

        self.assertEqual(response.status_int, 200)
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], '*')
        self.auth.check_token.assert_not_called()
        self.main.get_bot.assert_not_called()

    def test_warmup_is_absent_and_start_stop_are_unchanged(self):
        warmup = self.client.get('/_ah/warmup', expect_errors=True)
        start = self.client.get('/_ah/start')
        stop = self.client.get('/_ah/stop')

        self.assertEqual(warmup.status_int, 404)
        self.assertEqual(start.text, 'Start successful')
        self.assertEqual(stop.text, 'Stop successful')
        self.assertEqual(start.status_int, 200)
        self.assertEqual(stop.status_int, 200)


class AppBuilderTest(unittest.TestCase):
    def setUp(self):
        self.main = types.ModuleType('main')
        self.auth = types.ModuleType('auth')
        self.settings = types.ModuleType('settings')
        self.bot = Mock()

        self.main.get_bot = Mock(return_value=self.bot)
        self.main.get_options = Mock(return_value={'scenario_version': 3})
        self.auth.check_token = Mock(side_effect=lambda token: token == 'valid-token')
        self.settings.GCP_SETTINGS = {
            'services': {
                'app': {'base_url': 'https://app.example.invalid'},
            },
        }
        self.settings.SERVICE_SETTINGS = self.settings.GCP_SETTINGS['services']
        self.settings.DEPLOY_ENV = 'test'
        self.bot.build_scenario.return_value = True, None

        self.module = load_module(
            '_test_app_builder',
            'app_builder.py',
            {
                'main': self.main,
                'auth': self.auth,
                'settings': self.settings,
            },
        )
        self.client = TestApp(self.module.app)

    def test_post_requires_header_token_before_bot_lookup(self):
        response = self.client.post(
            '/api/build/bot',
            params={'token': 'valid-token'},
            expect_errors=True,
        )

        self.assertEqual(response.status_int, 401)
        self.main.get_bot.assert_not_called()

    def test_options_is_unauthenticated_and_preserves_cors(self):
        response = self.client.options('/api/build/bot')

        self.assertEqual(response.status_int, 200)
        self.assertEqual(response.headers['Content-Type'],
                         'text/plain; charset=utf-8')
        self.assertEqual(response.headers['Access-Control-Allow-Origin'],
                         'https://app.example.invalid')
        self.assertEqual(response.headers['Access-Control-Allow-Headers'],
                         'Authorization,Content-Type,X-API-Token')
        self.assertEqual(response.headers['Access-Control-Allow-Methods'],
                         'POST, GET, OPTIONS')
        self.auth.check_token.assert_not_called()
        self.main.get_bot.assert_not_called()

    def test_success_preserves_text_response_and_build_arguments(self):
        response = self.client.post(
            '/api/build/bot',
            params={
                'task_id': 'task-1',
                'skip_image': 'true',
                'force': 'false',
            },
            headers={'X-API-Token': 'valid-token'},
        )

        self.assertEqual(response.status_int, 200)
        self.assertEqual(response.headers['Content-Type'],
                         'text/plain; charset=utf-8')
        self.assertEqual(json.loads(response.text)['result'], 'Success')
        self.bot.build_scenario.assert_called_once_with(
            task_id='task-1',
            options={'skip_image': True, 'force': False},
            version=3,
        )

    def test_logical_build_failure_remains_http_200(self):
        self.bot.build_scenario.return_value = False, 'build error'

        response = self.client.post(
            '/api/build/bot',
            headers={'X-API-Token': 'valid-token'},
        )

        self.assertEqual(response.status_int, 200)
        self.assertEqual(response.headers['Content-Type'],
                         'text/plain; charset=utf-8')
        self.assertEqual(json.loads(response.text)['result'], 'Failure')

    def test_unknown_bot_is_checked_after_valid_token(self):
        self.main.get_bot.return_value = None

        response = self.client.post(
            '/api/build/missing',
            headers={'X-API-Token': 'valid-token'},
            expect_errors=True,
        )

        self.assertEqual(response.status_int, 404)
        self.auth.check_token.assert_called_once_with('valid-token')
        self.main.get_bot.assert_called_once_with('missing')

    def test_builder_route_is_post_only(self):
        response = self.client.get('/api/build/bot', expect_errors=True)

        self.assertEqual(response.status_int, 405)
        self.auth.check_token.assert_not_called()
        self.main.get_bot.assert_not_called()

    def test_healthz_warmup_start_and_stop_contracts(self):
        healthz = self.client.get('/healthz')
        warmup = self.client.get('/_ah/warmup', expect_errors=True)
        start = self.client.get('/_ah/start')
        stop = self.client.get('/_ah/stop')

        self.assertEqual(healthz.status_int, 200)
        self.assertEqual(healthz.headers['Content-Type'],
                         'application/json; charset=utf-8')
        self.assertEqual(healthz.json, {'status': 'ok'})
        self.assertEqual(warmup.status_int, 404)
        self.assertEqual(start.text, 'Start successful')
        self.assertEqual(stop.text, 'Stop successful')


class AppEntrypointTest(unittest.TestCase):
    def load_app(self):
        main = types.ModuleType('main')
        settings = types.ModuleType('settings')
        root_webapi = types.ModuleType('webapi')
        dashboard = types.ModuleType('dashboard')

        main.get_plugins = Mock(return_value={})
        settings.DEPLOY_ENV = 'test'
        root_webapi.app = Bottle()
        dashboard.app = Bottle()

        @root_webapi.app.get('/_ah/start')
        def start_handler():
            return 'Start successful'

        @root_webapi.app.get('/_ah/stop')
        def stop_handler():
            return 'Stop successful'

        return load_module(
            '_test_app',
            'app.py',
            {
                'main': main,
                'settings': settings,
                'webapi': root_webapi,
                'dashboard': dashboard,
            },
        )

    def test_healthz_is_json_and_warmup_is_not_merged(self):
        client = TestApp(self.load_app().app)

        healthz = client.get('/healthz')
        warmup = client.get('/_ah/warmup', expect_errors=True)
        start = client.get('/_ah/start')
        stop = client.get('/_ah/stop')

        self.assertEqual(healthz.status_int, 200)
        self.assertEqual(healthz.headers['Content-Type'],
                         'application/json; charset=utf-8')
        self.assertEqual(healthz.json, {'status': 'ok'})
        self.assertEqual(warmup.status_int, 404)
        self.assertEqual(start.text, 'Start successful')
        self.assertEqual(stop.text, 'Stop successful')

    def test_main_initialization_failure_prevents_healthz_startup(self):
        original_import = builtins.__import__

        def import_with_failure(name, *args, **kwargs):
            if name == 'main':
                raise RuntimeError('必須資格情報の初期化に失敗')
            return original_import(name, *args, **kwargs)

        module_name = '_test_app_initialization_failure'
        spec = importlib.util.spec_from_file_location(
            module_name, PROJECT_ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)

        with patch.object(builtins, '__import__', side_effect=import_with_failure):
            with self.assertRaisesRegex(RuntimeError, '必須資格情報'):
                spec.loader.exec_module(module)


if __name__ == '__main__':
    unittest.main()
