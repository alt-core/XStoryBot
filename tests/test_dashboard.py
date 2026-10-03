import datetime
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from functools import wraps
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode
from requests.exceptions import RequestException
from wsgiref.util import setup_testing_defaults


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeUser:
    def __init__(self, value):
        self.value = value

    @classmethod
    def deserialize(cls, value):
        if not value or ':' not in value:
            return None
        return cls(value)

    def serialize(self):
        return self.value


def make_ok_json(message, data=None):
    return json.dumps({
        'code': 200,
        'result': 'Success',
        'message': message,
        'data': data,
    }, ensure_ascii=False)


def make_error_json(code, message):
    return json.dumps({
        'code': code,
        'result': 'Error',
        'message': message,
    }, ensure_ascii=False)


def load_dashboard(group_db=None):
    """外部サービスを初期化せずにdashboard moduleを読み込む。"""
    settings = types.ModuleType('settings')
    settings.DEPLOY_ENV = 'prod'
    settings.CONSTANTS = {}
    settings.CLOUD_SETTINGS = {'provider': 'gcp'}
    settings.BOTS = {
        'zeta': {
            'name': 'あいうBot',
            'description': '<strong>開発環境</strong>',
        },
        'alpha': {
            'name': 'Beta Bot',
            'description': '<em>本番環境</em>',
        },
    }
    settings.GCP_SETTINGS = {
        'project_id': 'public-test-project',
        'services': {
            'builder': {'base_url': 'http://builder.example.test'},
        },
    }
    settings.AUTH_SETTINGS = {'admin_auth_json_env': 'TEST_ADMIN_AUTH'}
    settings.SERVICE_SETTINGS = settings.GCP_SETTINGS['services']

    auth_middleware = types.ModuleType('auth_middleware')

    def auth_required(state_changing=False):
        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                from bottle import request
                request.dashboard_user = {
                    'username': 'admin',
                    'csrf_token': 'csrf-token',
                }
                return func(*args, **kwargs)
            wrapper.dashboard_auth_required = True
            wrapper.dashboard_state_changing = state_changing
            return wrapper
        return decorator

    auth_middleware.auth_required = auth_required
    auth_middleware.require_same_origin = Mock()
    auth_middleware.verify_credentials = Mock(return_value=True)
    auth_middleware.create_session = Mock(
        return_value=('signed-session', 'csrf-token'))
    auth_middleware.set_session_cookie = Mock()
    auth_middleware.clear_session_cookie = Mock()

    main = types.ModuleType('main')
    main.get_bot = Mock(return_value=types.SimpleNamespace(name='Bot Runtime'))

    task_client = types.ModuleType('task_client')
    task_client.create_task = Mock(return_value='task-123')
    task_client.allows_delayed_scenarios = Mock(return_value=True)

    utility = types.ModuleType('utility')
    utility.timezone = lambda name: datetime.timezone(datetime.timedelta(hours=9))
    utility.make_ok_json = make_ok_json
    utility.make_error_json = make_error_json

    build_cache = types.ModuleType('build_cache')
    build_cache.get_cache = Mock(return_value=None)

    users = types.ModuleType('users')
    users.User = FakeUser
    users.get_all_groups = Mock(return_value=[
        {'id': 'z-group'},
        {'id': 'A-group'},
    ])
    users.get_group_members = Mock(return_value=[])
    users.remove_group_member = Mock()
    users.append_group_member = Mock()

    class FakeGroupMessageTaskDB:
        STATUS_PENDING = 'pending'
        STATUS_RUNNING = 'running'
        STATUS_COMPLETED = 'completed'
        STATUS_FAILED = 'failed'
        STATUS_ABORTED = 'aborted'

    FakeGroupMessageTaskDB.create_task = Mock(return_value='group-task-1')
    FakeGroupMessageTaskDB.get_recent_tasks = Mock(return_value=[])
    FakeGroupMessageTaskDB.get_task = Mock(return_value=None)
    FakeGroupMessageTaskDB.abort_task = Mock(return_value=True)
    FakeGroupMessageTaskDB.retry_failed_members = Mock(return_value=None)
    FakeGroupMessageTaskDB.update_task_status = Mock(return_value=True)

    if group_db is not None:
        FakeGroupMessageTaskDB = group_db

    group_message_task_db = types.ModuleType('group_message_task_db')
    group_message_task_db.GroupMessageTaskDB = FakeGroupMessageTaskDB

    auth = types.ModuleType('auth')
    auth.get_api_token = Mock(return_value='shared-api-token')

    requests = types.ModuleType('requests')
    requests.RequestException = RequestException
    requests.post = Mock(return_value=types.SimpleNamespace(text='builder body'))

    replacements = {
        'settings': settings,
        'auth_middleware': auth_middleware,
        'main': main,
        'task_client': task_client,
        'utility': utility,
        'build_cache': build_cache,
        'users': users,
        'group_message_task_db': group_message_task_db,
        'auth': auth,
        'requests': requests,
    }
    previous = {name: sys.modules.get(name) for name in replacements}
    module_name = '_test_dashboard'
    previous_module = sys.modules.get(module_name)
    try:
        sys.modules.update(replacements)
        spec = importlib.util.spec_from_file_location(
            module_name, PROJECT_ROOT / 'dashboard.py')
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

    dependencies = types.SimpleNamespace(
        settings=settings,
        auth_middleware=auth_middleware,
        main=main,
        task_client=task_client,
        build_cache=build_cache,
        users=users,
        group_db=FakeGroupMessageTaskDB,
        auth=auth,
        requests=requests,
    )
    return module, dependencies


def call_wsgi(app, method, path, params=None, json_body=None):
    """Bottle appをネットワーク接続なしで呼び出す。"""
    environ = {}
    setup_testing_defaults(environ)
    environ['REQUEST_METHOD'] = method
    environ['PATH_INFO'] = path

    if method == 'GET':
        environ['QUERY_STRING'] = urlencode(params or {})
        body = b''
    elif json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode('utf-8')
        environ['CONTENT_TYPE'] = 'application/json'
    else:
        body = urlencode(params or {}).encode('utf-8')
        environ['CONTENT_TYPE'] = 'application/x-www-form-urlencoded'

    environ['CONTENT_LENGTH'] = str(len(body))
    environ['wsgi.input'] = io.BytesIO(body)
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured['status'] = int(status.split(' ', 1)[0])
        captured['headers'] = dict(headers)

    result = app(environ, start_response)
    try:
        response_body = b''.join(
            part.encode('utf-8') if isinstance(part, str) else part
            for part in result)
    finally:
        if hasattr(result, 'close'):
            result.close()
    return captured['status'], captured['headers'], response_body.decode('utf-8')


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.module, self.dependencies = load_dashboard()
        # 予約日時の過去判定を実時刻に依存させない。
        self.now = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
        patcher = patch.object(self.module, '_now_utc', side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_認証秘密値を読まずにdashboardをimportする(self):
        self.dependencies.auth_middleware.verify_credentials.assert_not_called()

    def test_public_shell_does_not_embed_bot_group_or_private_values(self):
        self.dependencies.settings.BOTS = {
            'hidden-bot-marker': {
                'name': 'hidden-name-marker',
                'description': 'hidden-description-marker',
            },
        }
        self.dependencies.users.get_all_groups.return_value = [
            {'id': 'hidden-group-marker'},
        ]
        self.dependencies.settings.GCP_SETTINGS['api_token'] = (
            'hidden-config-marker')

        status, _, body = call_wsgi(
            self.module.app, 'GET', '/dashboard/hidden-bot-marker')

        self.assertEqual(status, 200)
        self.assertNotIn('hidden-name-marker', body)
        self.assertNotIn('hidden-description-marker', body)
        self.assertNotIn('hidden-group-marker', body)
        self.assertNotIn('hidden-config-marker', body)
        self.dependencies.users.get_all_groups.assert_not_called()

    def test_config_sorts_by_display_name_and_returns_raw_description(self):
        with patch.object(self.module.logging, 'info') as info:
            status, headers, body = call_wsgi(
                self.module.app, 'GET', '/dashboard/api/config')

        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
        payload = json.loads(body)
        self.assertEqual(
            [bot['id'] for bot in payload['data']['bots']],
            ['alpha', 'zeta'],
        )
        self.assertEqual(
            payload['data']['bots'][0]['description'],
            '<em>本番環境</em>',
        )
        self.assertEqual(payload['data']['user_email'], 'admin')
        self.assertEqual(payload['data']['csrf_token'], 'csrf-token')
        info.assert_called_once_with('Dashboard accessed by: admin')

    def test_every_management_api_is_authenticated_and_no_duplicate_routes(self):
        expected_rules = {
            '/dashboard/api/config',
            '/dashboard/api/bots/<bot_name>/richmenus',
            '/dashboard/api/bots/<bot_name>/richmenus/menus/<name>',
            '/dashboard/api/bots/<bot_name>/richmenus/default',
            '/dashboard/build_async/<bot_name>',
            '/dashboard/last_build_result/<bot_name>',
            '/dashboard/api/groups',
            '/dashboard/api/group_members/<group_id>',
            '/dashboard/api/remove_member',
            '/dashboard/api/add_members',
            '/dashboard/api/create_group_message_task',
            '/dashboard/api/bots/<bot_name>/group_tasks',
            '/dashboard/api/group_tasks/<task_id>',
            '/dashboard/api/group_tasks/<task_id>/abort',
            '/dashboard/api/group_tasks/<task_id>/retry_failed',
        }
        matching_routes = [
            route for route in self.module.app.routes
            if route.rule in expected_rules
        ]

        self.assertEqual(
            {route.rule for route in matching_routes}, expected_rules)
        self.assertTrue(all(
            getattr(route.callback, 'dashboard_auth_required', False)
            for route in matching_routes
        ))
        rules = {route.rule for route in self.module.app.routes}
        self.assertFalse(any('/cancel_task/' in rule for rule in rules))
        self.assertFalse(any(rule.endswith('/execute') for rule in rules))

    def test_状態変更APIだけCSRF検証付き認証にする(self):
        expected_state_changing = {
            '/dashboard/api/bots/<bot_name>/richmenus/menus/<name>',
            '/dashboard/api/bots/<bot_name>/richmenus/default',
            '/dashboard/logout',
            '/dashboard/build_async/<bot_name>',
            '/dashboard/api/remove_member',
            '/dashboard/api/add_members',
            '/dashboard/api/create_group_message_task',
            '/dashboard/api/group_tasks/<task_id>/abort',
            '/dashboard/api/group_tasks/<task_id>/retry_failed',
        }

        actual = {
            route.rule for route in self.module.app.routes
            if getattr(route.callback, 'dashboard_state_changing', False)
        }

        self.assertEqual(expected_state_changing, actual)

    def test_リッチメニューは対象ごとに処理し失敗をJSONで返す(self):
        service = Mock()
        service.menus = types.SimpleNamespace(menus={'main': object()}, default='main')
        service.plan.return_value = {'name': 'main', 'status': 'create'}
        service.apply.return_value = {'name': 'main', 'status': 'create', 'new_id': 'id'}
        with patch('richmenu_service.RichmenuService', return_value=service):
            status, _headers, body = call_wsgi(self.module.app, 'GET', '/dashboard/api/bots/alpha/richmenus')
            self.assertEqual(200, status)
            self.assertEqual(['main'], json.loads(body)['data']['menus'])
            call_wsgi(self.module.app, 'GET', '/dashboard/api/bots/alpha/richmenus/menus/main')
            service.apply.assert_not_called()
            call_wsgi(self.module.app, 'POST', '/dashboard/api/bots/alpha/richmenus/menus/main')
            service.apply.assert_called_once_with('main')
            service.plan.side_effect = ValueError('画像が不正です')
            status, _headers, body = call_wsgi(self.module.app, 'GET', '/dashboard/api/bots/alpha/richmenus/menus/main')
            self.assertEqual(400, status)
            self.assertIn('画像が不正', json.loads(body)['message'])

    def test_loginはOrigin確認後に署名Cookieを発行する(self):
        status, headers, body = call_wsgi(
            self.module.app,
            'POST',
            '/dashboard/login',
            params={'username': 'admin', 'password': 'secret'},
        )

        self.assertEqual(200, status)
        self.assertEqual('no-store', headers['Cache-Control'])
        self.assertEqual('Success', json.loads(body)['result'])
        self.dependencies.auth_middleware.require_same_origin.assert_called_once_with()
        self.dependencies.auth_middleware.verify_credentials.assert_called_once_with(
            'admin', 'secret')
        self.dependencies.auth_middleware.create_session.assert_called_once_with(
            'admin')
        self.dependencies.auth_middleware.set_session_cookie.assert_called_once_with(
            'signed-session')

    def test_login失敗は同じmessageでCookieを発行しない(self):
        self.dependencies.auth_middleware.verify_credentials.return_value = False

        status, headers, body = call_wsgi(
            self.module.app,
            'POST',
            '/dashboard/login',
            params={'username': 'unknown', 'password': 'wrong'},
        )

        self.assertEqual(401, status)
        self.assertEqual('no-store', headers['Cache-Control'])
        self.assertIn('ユーザー名またはパスワードが正しくありません', body)
        self.dependencies.auth_middleware.set_session_cookie.assert_not_called()

    def test_group_list_keeps_case_insensitive_id_order(self):
        status, _, body = call_wsgi(
            self.module.app, 'GET', '/dashboard/api/groups')

        self.assertEqual(status, 200)
        self.assertEqual(
            [group['id'] for group in json.loads(body)['groups']],
            ['A-group', 'z-group'],
        )

    def test_build_get_and_post_keep_queued_top_level_task_id_contract(self):
        get_status, get_headers, get_body = call_wsgi(
            self.module.app,
            'GET',
            '/dashboard/build_async/zeta',
            params={'skip_image': 'skip-value', 'force': 'force-value'},
        )
        post_status, post_headers, post_body = call_wsgi(
            self.module.app,
            'POST',
            '/dashboard/build_async/zeta',
            params={'skip_image': 'post-skip', 'force': 'post-force'},
        )

        self.assertEqual(get_status, 200)
        self.assertEqual(post_status, 200)
        for headers in (get_headers, post_headers):
            self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
        for body in (get_body, post_body):
            payload = json.loads(body)
            self.assertEqual(payload['message'], 'Queued')
            self.assertEqual(payload['task_id'], 'task-123')
            self.assertNotIn('data', payload)
        self.assertEqual(
            self.dependencies.task_client.create_task.call_args_list[0].kwargs,
            {
                'queue_name': 'build-queue',
                'url': '/api/build/zeta',
                'params': {
                    'skip_image': 'skip-value',
                    'force': 'force-value',
                },
            },
        )
        self.assertEqual(
            self.dependencies.task_client.create_task.call_args_list[1].kwargs[
                'params'],
            {'skip_image': 'post-skip', 'force': 'post-force'},
        )

    def test_local_builder_passes_body_and_options_without_http_rewrite(self):
        self.dependencies.settings.DEPLOY_ENV = 'local'
        self.dependencies.requests.post.return_value.text = (
            '{"status":"Failure","error":"builder result"}')

        status, headers, body = call_wsgi(
            self.module.app,
            'POST',
            '/dashboard/build_async/zeta',
            params={'skip_image': 'non-empty', 'force': 'also-non-empty'},
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
        self.assertEqual(
            body, '{"status":"Failure","error":"builder result"}')
        self.dependencies.requests.post.assert_called_once_with(
            'http://builder.example.test/api/build/zeta',
            params={
                'skip_image': 'non-empty',
                'force': 'also-non-empty',
            },
            headers={'X-API-Token': 'shared-api-token'},
        )

    def test_AWSではFargateタスクを開始し既存のQueued応答を返す(self):
        self.dependencies.settings.CLOUD_SETTINGS = {'provider': 'aws'}
        task_id = '12345678-1234-4abc-8def-1234567890ab'
        launcher = Mock()
        launcher.launch.return_value = task_id

        with (
            patch.object(self.module.uuid, 'uuid4', return_value=task_id),
            patch.object(
                self.module,
                '_create_aws_build_task_launcher',
                return_value=launcher,
            ),
        ):
            status, _, body = call_wsgi(
                self.module.app,
                'POST',
                '/dashboard/build_async/zeta',
                params={'skip_image': 'true', 'force': 'false'},
            )

        self.assertEqual(200, status)
        self.assertEqual({
            'code': 200,
            'result': 'Success',
            'message': 'Queued',
            'task_id': task_id,
        }, json.loads(body))
        launcher.launch.assert_called_once_with(
            task_id=task_id,
            bot_name='zeta',
            skip_image=True,
            force=False,
        )
        self.dependencies.task_client.create_task.assert_not_called()
        self.dependencies.requests.post.assert_not_called()

    def test_missing_last_build_result_is_200_failure_sentinel(self):
        status, headers, body = call_wsgi(
            self.module.app,
            'GET',
            '/dashboard/last_build_result/zeta',
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            headers['Content-Type'], 'application/json; charset=utf-8')
        self.assertEqual(
            json.loads(body), {'status': 'Failure', 'error': 'Not Found'})

    def test_予約日時のoffset付き入力は保存やenqueue前に400にする(self):
        for scheduled_at in (
                '2026-09-06T20:00:00+00:00',
                '2026-09-06T20:00:00+09:00',
                '2026-09-06T20:00:00Z'):
            with self.subTest(scheduled_at=scheduled_at):
                status, _, body = call_wsgi(
                    self.module.app,
                    'POST',
                    '/dashboard/api/create_group_message_task',
                    json_body={
                        'bot_name': 'zeta',
                        'group_id': 'group-1',
                        'action': 'hello',
                        'scheduled_at': scheduled_at,
                    },
                )

                self.assertEqual(400, status)
                self.assertIn('タイムゾーンを付けずに入力してください', body)

        self.dependencies.group_db.create_task.assert_not_called()
        self.dependencies.task_client.create_task.assert_not_called()

    def test_過去や現在の予約日時は保存やenqueue前に400にする(self):
        # 2026-08-01T00:00:00Zは日本時間の09:00:00。
        for scheduled_at in ('2026-08-01T09:00:00', '2026-08-01T08:59:59', '2025-08-01T10:00:00'):
            with self.subTest(scheduled_at=scheduled_at):
                status, _, body = call_wsgi(
                    self.module.app,
                    'POST',
                    '/dashboard/api/create_group_message_task',
                    json_body={
                        'bot_name': 'zeta',
                        'group_id': 'group-1',
                        'action': 'hello',
                        'scheduled_at': scheduled_at,
                    },
                )

                self.assertEqual(400, status)
                self.assertIn('予約日時が現在より前です', body)

        self.dependencies.group_db.create_task.assert_not_called()
        self.dependencies.task_client.create_task.assert_not_called()

    def test_文字列でない予約日時は形式エラーにする(self):
        for allows_delayed in (True, False):
            self.dependencies.task_client.allows_delayed_scenarios.return_value = allows_delayed
            for scheduled_at in (0, 0.0, False, True, 20260901, [], [1], {}, {'date': '2026-10-04'}):
                with self.subTest(allows_delayed=allows_delayed, scheduled_at=scheduled_at):
                    status, _, body = call_wsgi(
                        self.module.app,
                        'POST',
                        '/dashboard/api/create_group_message_task',
                        json_body={
                            'bot_name': 'zeta',
                            'group_id': 'group-1',
                            'action': 'hello',
                            'scheduled_at': scheduled_at,
                        },
                    )

                    self.assertEqual(400, status)
                    self.assertIn('Invalid scheduled_at format', body)
                    self.dependencies.group_db.create_task.assert_not_called()
                    self.dependencies.task_client.create_task.assert_not_called()

    def test_日時なしと空文字は即時送信として維持する(self):
        data = {'bot_name': 'zeta', 'group_id': 'group-1', 'action': 'hello'}
        for allows_delayed in (True, False):
            self.dependencies.task_client.allows_delayed_scenarios.return_value = allows_delayed
            for date_input in ({}, {'scheduled_at': None}, {'scheduled_at': ''}):
                with self.subTest(allows_delayed=allows_delayed, date_input=date_input):
                    self.dependencies.group_db.create_task.reset_mock()
                    self.dependencies.task_client.create_task.reset_mock()
                    status, _, _body = call_wsgi(
                        self.module.app, 'POST', '/dashboard/api/create_group_message_task',
                        json_body={**data, **date_input})
                    self.assertEqual(200, status)
                    self.assertIsNone(self.dependencies.group_db.create_task.call_args.kwargs['scheduled_at'])
                    self.dependencies.task_client.create_task.assert_called_once()

    def test_予約日時のoffsetなし入力をJSTで保存してenqueueする(self):
        status, _, body = call_wsgi(
            self.module.app,
            'POST',
            '/dashboard/api/create_group_message_task',
            json_body={
                'bot_name': 'zeta',
                'group_id': 'group-1',
                'action': 'hello',
                'scheduled_at': '2026-09-06T20:00:00',
            },
        )

        self.assertEqual(200, status)
        scheduled_at = self.dependencies.group_db.create_task.call_args.kwargs[
            'scheduled_at']
        self.assertEqual(
            '2026-09-06T20:00:00+09:00', scheduled_at.isoformat())
        self.assertEqual(
            '2026-09-06T11:00:00+00:00',
            scheduled_at.astimezone(datetime.timezone.utc).isoformat())
        self.assertIn('実行予定です', json.loads(body)['message'])
        self.dependencies.task_client.create_task.assert_called_once_with(
            queue_name='group-message-queue',
            url='/api/v1/bots/zeta/process_group_batch',
            params={'message_task_id': 'group-task-1', 'batch_index': 0},
        )

    def test_予約未対応では作成前に拒否し即時配信はできる(self):
        self.dependencies.task_client.allows_delayed_scenarios.return_value = False
        data = {'bot_name': 'zeta', 'group_id': 'group-1', 'action': 'hello'}
        status, _, body = call_wsgi(
            self.module.app, 'POST', '/dashboard/api/create_group_message_task',
            json_body={**data, 'scheduled_at': '2026-10-01T20:00:00'})
        self.assertEqual(400, status)
        self.assertIn('予約配信を使えません', body)
        self.dependencies.group_db.create_task.assert_not_called()
        self.dependencies.task_client.create_task.assert_not_called()
        status, _, _body = call_wsgi(
            self.module.app, 'POST', '/dashboard/api/create_group_message_task',
            json_body={**data, 'scheduled_at': None})
        self.assertEqual(200, status)
        self.dependencies.group_db.create_task.assert_called_once()
        self.dependencies.task_client.create_task.assert_called_once()

    def test_予約未対応の管理画面は即時送信だけを表示する(self):
        self.dependencies.task_client.allows_delayed_scenarios.return_value = False
        status, _, body = call_wsgi(self.module.app, 'GET', '/dashboard/')
        self.assertEqual(200, status)
        self.assertFalse('id="schedule-date"' in body)
        self.assertIn('id="immediate-send" checked', body)
        self.assertIn('即時送信のみ', body)

    def test_scheduled_enqueue_failure_message_is_not_overwritten(self):
        self.dependencies.task_client.create_task.side_effect = RuntimeError(
            'queue unavailable')

        with patch.object(self.module.logging, 'error'):
            status, _, body = call_wsgi(
                self.module.app,
                'POST',
                '/dashboard/api/create_group_message_task',
                json_body={
                    'bot_name': 'zeta',
                    'group_id': 'group-1',
                    'action': 'hello',
                    'scheduled_at': '2026-08-11T20:00:00',
                },
            )

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn('即時実行の開始に失敗しました', payload['message'])
        self.assertNotIn('実行予定です', payload['message'])
        self.dependencies.group_db.create_task.assert_called_once()

    def test_remove_member_input_error_stays_400(self):
        status, _, body = call_wsgi(
            self.module.app,
            'POST',
            '/dashboard/api/remove_member',
            json_body={},
        )

        self.assertEqual(status, 400)
        self.assertIn('Group ID is required', body)


class DashboardGroupTaskFlowTest(unittest.TestCase):
    def setUp(self):
        import cloud_backend
        from cloud_backend.local.object_store import LocalObjectStore
        from cloud_backend.local.state_store import LocalStateStore

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_store = LocalStateStore(directory.name)
        self.object_store = LocalObjectStore(
            directory.name, 'http://127.0.0.1:8080/local-media')
        models = types.ModuleType('models')
        models.GroupMembersDB = object
        models.get_state_store = lambda: self.state_store
        spec = importlib.util.spec_from_file_location(
            '_dashboard_group_task_db', PROJECT_ROOT / 'group_message_task_db.py')
        module = importlib.util.module_from_spec(spec)
        with (
            patch.dict(sys.modules, {'models': models}),
            patch.object(cloud_backend, 'create_object_store', return_value=self.object_store),
        ):
            spec.loader.exec_module(module)
            self.group_db = module.GroupMessageTaskDB
            self.group_db.initialize({}, {})
        self.module, self.dependencies = load_dashboard(self.group_db)
        self.task_id = 'task-with-stored-id'
        self.state_store.create_group_message_task(self.task_id, {
            'bot_name': 'zeta', 'group_id': 'group-1', 'action': 'notice',
            'attrs': {}, 'created_by': 'dashboard', 'status': 'failed',
            'total_members': 2, 'processed_members': 2, 'successful_members': 1,
            'failed_members': 1, 'current_batch': 0, 'total_batches': 1,
            'interval_ms': 1, 'error_messages': ['送信失敗'],
        })
        self.object_store.store_private(
            f'group_tasks/{self.task_id}/failed_members.json',
            json.dumps(['line:user,failed-user']))

    def response(self, path, method='GET', status=200):
        result_status, _, body = call_wsgi(self.module.app, method, path)
        self.assertEqual(status, result_status, body)
        return json.loads(body) if status == 200 else body

    def test_失敗タスクは中止後に失敗者だけを再送する(self):
        base = f'/dashboard/api/group_tasks/{self.task_id}'
        listed = self.response('/dashboard/api/bots/zeta/group_tasks')['data']['tasks']
        task = self.response(base)['data']['task']
        self.assertEqual(listed[0]['id'], self.task_id)
        self.assertEqual(task['id'], self.task_id)
        self.assertTrue(task['can_abort'])
        self.assertFalse(task['can_retry'])
        self.assertNotIn('id', self.state_store.get_group_message_task(self.task_id))

        self.response(base + '/retry_failed', method='POST', status=400)
        self.dependencies.task_client.create_task.assert_not_called()
        self.assertEqual(len(self.state_store.get_recent_group_message_tasks('zeta', 10)), 1)
        self.response(base + '/abort', method='POST')
        stopped = self.response(base)['data']['task']
        self.assertEqual(stopped['status'], 'aborted')
        self.assertFalse(stopped['can_abort'])
        self.assertTrue(stopped['can_retry'])

        from tests.test_group_message_batching import load_group_message_task_manager
        manager_module = load_group_message_task_manager()
        manager_module.GroupMessageTaskDB = self.group_db
        bot = Mock()
        result, status = manager_module.GroupMessageTaskManager('zeta', bot).handle_batch_process_request(self.task_id)
        self.assertEqual((status, result['status']), (200, 'aborted'))
        bot.handle_action.assert_not_called()
        manager_module.users.get_group_members.assert_not_called()
        manager_module.task_client.create_task.assert_not_called()
        self.assertEqual(self.state_store.get_group_message_task(self.task_id)['status'], 'aborted')

        retry = self.response(base + '/retry_failed', method='POST')['data']
        new_task_id = retry['new_task_id']
        self.assertEqual(self.group_db.get_members_from_storage(new_task_id), ['line:user,failed-user'])
        self.assertEqual(self.state_store.get_group_message_task(new_task_id)['status'], 'pending')
        self.dependencies.task_client.create_task.assert_called_once_with(
            queue_name='group-message-queue',
            url='/api/v1/bots/zeta/process_group_batch',
            params={'message_task_id': new_task_id, 'batch_index': 0})

    def test_完了後の中止はAPIからも拒否して完了状態を保つ(self):
        self.state_store.update_group_message_task(
            self.task_id, lambda task: {'status': 'completed'})
        self.response(f'/dashboard/api/group_tasks/{self.task_id}/abort', method='POST', status=400)
        self.assertEqual(self.state_store.get_group_message_task(self.task_id)['status'], 'completed')

    def test_再送一覧が壊れている場合はAPIも失敗して元データを保つ(self):
        self.state_store.update_group_message_task(
            self.task_id, lambda task: {'status': 'aborted'})
        key = f'group_tasks/{self.task_id}/failed_members.json'
        self.object_store.store_private(key, '壊れたJSON')
        with patch.object(self.module.logging, 'error'):
            self.response(f'/dashboard/api/group_tasks/{self.task_id}/retry_failed', method='POST', status=500)
        self.assertEqual(self.object_store.load_private(key), '壊れたJSON'.encode('utf-8'))
        self.assertEqual(len(self.state_store.get_recent_group_message_tasks('zeta', 10)), 1)
        self.dependencies.task_client.create_task.assert_not_called()

    @unittest.skipUnless(shutil.which('node'), 'Node.jsが利用できないため省略')
    def test_保存済みタスクのAPI応答を使い画面のID操作と自動更新を確認する(self):
        base = f'/dashboard/api/group_tasks/{self.task_id}'
        responses = {
            'list': self.response('/dashboard/api/bots/zeta/group_tasks'),
            'failed': self.response(base),
        }
        responses['abort'] = self.response(base + '/abort', method='POST')
        responses['aborted'] = self.response(base)
        responses['retry'] = self.response(base + '/retry_failed', method='POST')
        self.state_store.update_group_message_task(
            self.task_id, lambda task: {'status': 'completed', 'failed_members': 0})
        responses['completed'] = self.response(base)
        template = (PROJECT_ROOT / 'template' / 'dashboard.tpl').read_text(encoding='utf-8')
        escape_start = template.index('    function escapeHtml(value) {')
        escape_end = template.index('    function renderBotNavigation', escape_start)
        start = template.index('    function updateTaskList() {')
        end = template.index('    let richmenuPlan = null;', start)
        script = r"""
const assert = require('node:assert/strict');
const responses = JSON.parse(process.argv[1]), taskId = 'task-with-stored-id';
let botName = 'zeta', csrfToken = 'artificial', state = 'failed', poll;
const fields = new Map(), requests = [];
const $ = (key) => {
  if (!fields.has(key)) fields.set(key, {props: {}, data: {}});
  const field = fields.get(key);
  return {
    text(value) { if (value === undefined) return field.text; field.text = value; return this; },
    html(value) { field.html = value; return this; },
    prop(name, value) { field.props[name] = value; return this; },
    data(name, value) { if (value === undefined) return field.data[name]; field.data[name] = value; return this; },
    css() { return this; }, addClass() { return this; }, removeClass() { return this; },
    click(handler) { field.click = handler; return this; },
    modal(command) { field.visible = command === 'show'; return this; },
    is() { return field.visible || false; },
  };
};
$.ajax = (options) => {
  requests.push(options);
  return {done(callback) {
    const payload = options.type === 'POST'
      ? responses[options.url.endsWith('/abort') ? 'abort' : 'retry']
      : options.url.endsWith('/group_tasks') ? responses.list : responses[state];
    callback(payload);
    return {fail() {}};
  }};
};
const alert = () => {}, formatDateTime = (value) => value || '-';
const setInterval = (callback) => { poll = callback; return 1; }, clearInterval = () => {};
""" + template[escape_start:escape_end] + template[start:end] + r"""
updateTaskList();
const html = fields.get('#task-list-body').html;
assert.match(html, /data-task-id="task-with-stored-id"/);
assert.match(html, /task-abort/);
assert.doesNotMatch(html, /task-retry/);
assert.doesNotMatch(html, /undefined/);
showTaskDetail(taskId);
assert.equal(fields.get('#detail-task-id').text, taskId);
assert.equal(fields.get('#detail-abort-button').props.disabled, false);
assert.equal(fields.get('#detail-retry-button').props.disabled, true);
assert.equal(fields.get('#detail-abort-button').data['task-id'], taskId);
startTaskPolling();
poll();
assert.equal(requests.at(-1).url, `/dashboard/api/group_tasks/${taskId}`);
state = 'aborted'; poll();
assert.equal(fields.get('#task-status-text').text, 'ステータス: 中止');
assert.equal(fields.get('#detail-abort-button').props.disabled, true);
assert.equal(fields.get('#detail-retry-button').props.disabled, false);
state = 'completed'; poll();
assert.equal(fields.get('#task-status-text').text, 'ステータス: 完了');
assert.equal(fields.get('#detail-retry-button').props.disabled, true);
state = 'failed'; showTaskDetail(taskId);
abortTask(fields.get('#detail-abort-button').data['task-id']);
assert.equal(requests.find((item) => item.type === 'POST').url, `/dashboard/api/group_tasks/${taskId}/abort`);
assert.equal(requests.find((item) => item.type === 'POST').headers['X-CSRF-Token'], csrfToken);
state = 'aborted'; showTaskDetail(taskId);
retryFailedMembers(fields.get('#detail-retry-button').data['task-id']);
assert.equal(requests.filter((item) => item.type === 'POST').at(-1).url, `/dashboard/api/group_tasks/${taskId}/retry_failed`);
stopTaskPolling();
"""
        completed = subprocess.run(
            ['node', '-e', script, json.dumps(responses, ensure_ascii=False)],
            capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)


class DashboardTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            PROJECT_ROOT / 'template' / 'dashboard.tpl'
        ).read_text(encoding='utf-8')

    @unittest.skipUnless(shutil.which('node'), 'Node.jsが利用できないため省略')
    def test_メニュー反映は一件ずつ行い失敗後に既定を変更しない(self):
        start = self.template.index('    let richmenuPlan = null;')
        end = self.template.index('    $(function(){', start)
        script = r"""
const assert = require('node:assert/strict');
let botName = 'alpha', csrfToken = 'artificial', fail = true;
const calls = [], completed = new Set(), created = [];
const fields = new Map();
const $ = (key) => ({prop() {return this;}, text(value) {
  if (value === undefined) return fields.get(key) || '';
  fields.set(key, value); return this;
}});
const confirm = () => true;
const fetch = async (url, options) => {
  calls.push([url, options.method]);
  const name = url.split('/menus/')[1];
  let data;
  if (name) {
    const status = completed.has(name) ? 'unchanged' : 'create';
    if (options.method === 'POST') {
      assert.equal(options.headers['X-CSRF-Token'], csrfToken);
      if (name === 'extra' && fail) return {ok: false, status: 502, json: async () => ({result: 'Error', message: '人工失敗'})};
      if (status === 'create') {completed.add(name); created.push(name);}
    }
    data = {name, status, current_id: completed.has(name) ? name : null};
  } else if (url.endsWith('/default')) data = {configured: 'main', action: 'set_default'};
  else data = {menus: ['main', 'extra'], default: 'main'};
  return {ok: true, json: async () => ({result: 'Success', data})};
};
""" + self.template[start:end] + r"""
(async () => {
  await runRichmenus(false);
  assert.equal(calls.filter((call) => call[1] === 'POST').length, 0);
  await runRichmenus(true);
  assert.deepEqual(created, ['main']);
  assert.equal(calls.some(([url, method]) => url.endsWith('/default') && method === 'POST'), false);
  fail = false;
  await runRichmenus(false);
  await runRichmenus(true);
  assert.deepEqual(created, ['main', 'extra']);
  assert.deepEqual(calls.at(-1), ['/dashboard/api/bots/alpha/richmenus/default', 'POST']);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.jsが利用できないため省略')
    def test_escape_html_function_escapes_five_html_characters(self):
        start = self.template.index('    function escapeHtml(value) {')
        end = self.template.index(
            '    function renderBotNavigation', start)
        function_source = self.template[start:end]
        script = (
            function_source
            + "process.stdout.write(escapeHtml(`&<>\\\"'`));"
        )

        result = subprocess.run(
            ['node', '-e', script],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            result.stdout, '&amp;&lt;&gt;&quot;&#39;')

    def test_only_approved_dynamic_html_sinks_use_escape_html(self):
        self.assertIn('const safeId = escapeHtml(id);', self.template)
        self.assertIn('const safeGroup = escapeHtml(group);', self.template)
        self.assertEqual(
            self.template.count(
                'errorHtml += `<li class="text-danger">${escapeHtml(error)}</li>`;'),
            2,
        )
        self.assertIn('const safeMember = escapeHtml(member);', self.template)
        self.assertIn('const safeGroupId = escapeHtml(groupId);', self.template)
        self.assertIn(
            "$('#bot-description').html(selectedBot.description || '');",
            self.template,
        )
        self.assertNotIn('escapeHtml(selectedBot.description', self.template)

        group_block = self.template[
            self.template.index('function renderGroups(groups) {'):
            self.template.index('async function loadGroups()')
        ]
        self.assertNotIn('${groupId}', group_block)
        self.assertIn('${safeGroupId}', group_block)

        task_block = self.template[
            self.template.index('const safeId = escapeHtml(id);'):
            self.template.index("$('#task-list-body').html(html);")
        ]
        self.assertNotIn('${id}', task_block)
        self.assertNotIn('${group}', task_block)
        self.assertIn('${safeId}', task_block)
        self.assertIn('${safeGroup}', task_block)

        member_block = self.template[
            self.template.index('const safeMember = escapeHtml(member);'):
            self.template.index('memberList = \'<li class="list-group-item">',
                                self.template.index(
                                    'const safeMember = escapeHtml(member);'))
        ]
        self.assertNotIn('${member}', member_block)
        self.assertNotIn('${groupId}', member_block)
        self.assertIn('${safeMember}', member_block)
        self.assertIn('${safeGroupId}', member_block)

    def test_build_polling_keeps_task_matching_and_interruption_detection(self):
        self.assertIn("if (resp.message === 'Queued')", self.template)
        self.assertIn('var task_id = resp.task_id;', self.template)
        self.assertIn('if (result.task_id === task_id)', self.template)
        self.assertIn(
            'else if (lastBuildTaskId != result.task_id)', self.template)
        self.assertIn(
            "url: '/dashboard/last_build_result/' + botName,",
            self.template,
        )
        polling_start = self.template.index(
            "url: '/dashboard/last_build_result/' + botName,")
        polling_end = self.template.index(
            '}).done(function(result)', polling_start)
        self.assertNotIn('X-CSRF-Token',
                         self.template[polling_start:polling_end])

    def test_template_uses_cookie_auth_and_csrf_for_mutations(self):
        self.assertNotIn('firebase', self.template.lower())
        self.assertNotIn('Authorization', self.template)
        self.assertNotIn('idToken', self.template)
        self.assertIn("url: '/dashboard/login'", self.template)
        self.assertIn("url: '/dashboard/logout'", self.template)
        self.assertIn("'X-CSRF-Token': csrfToken", self.template)

    def test_template_contains_no_server_rendered_bot_or_group_data(self):
        self.assertNotIn('bot_list', self.template)
        self.assertNotIn('bot_settings', self.template)
        self.assertNotIn('% for group in groups:', self.template)
        self.assertNotIn('{{bot_name}}', self.template)


if __name__ == '__main__':
    unittest.main()
