import io
import importlib
import builtins
import json
import os
import sys
from types import ModuleType
import unittest
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults

with patch.dict(os.environ, {'XSBOT_AWS_WORKER_KIND': 'action'}), patch.dict(
        sys.modules, {'main': ModuleType('main')}):
    import app_aws_worker
sys.modules['app_aws_worker'] = app_aws_worker


class TestResponse:
    def __init__(self, status_int, headers, body):
        self.status_int = status_int
        self.headers = dict(headers)
        self.body = body

    @property
    def json(self):
        return json.loads(self.body.decode('utf-8'))


def request_app(method, path, body=b'', content_type=None):
    environ = {}
    setup_testing_defaults(environ)
    environ['REQUEST_METHOD'] = method
    environ['PATH_INFO'] = path
    environ['wsgi.input'] = io.BytesIO(body)
    environ['CONTENT_LENGTH'] = str(len(body))
    if content_type:
        environ['CONTENT_TYPE'] = content_type

    captured = {}

    def start_response(status, headers, exc_info=None):
        del exc_info
        captured['status'] = status
        captured['headers'] = headers

    result = app_aws_worker.app(environ, start_response)
    try:
        response_body = b''.join(result)
    finally:
        close = getattr(result, 'close', None)
        if close:
            close()

    return TestResponse(
        int(captured['status'].split(' ', 1)[0]),
        captured['headers'],
        response_body,
    )


class AwsWorkerAppTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(sys.modules, {'main': ModuleType('main')})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_設定初期化に失敗したworkerは起動しない(self):
        original_import = builtins.__import__

        def import_module(name, *args, **kwargs):
            if name == 'main':
                raise ValueError('送信先が未設定です')
            return original_import(name, *args, **kwargs)

        with patch.dict(os.environ, {'XSBOT_AWS_WORKER_KIND': 'action'}):
            with patch.object(builtins, '__import__', side_effect=import_module):
                with self.assertRaisesRegex(ValueError, '送信先が未設定です'):
                    importlib.reload(app_aws_worker)
            importlib.reload(app_aws_worker)

    def test_worker種別の不備は起動時に検出する(self):
        try:
            for worker_kind in (None, '', 'unknown'):
                with self.subTest(worker_kind=worker_kind), patch.dict(os.environ):
                    if worker_kind is None:
                        os.environ.pop('XSBOT_AWS_WORKER_KIND', None)
                    else:
                        os.environ['XSBOT_AWS_WORKER_KIND'] = worker_kind
                    with self.assertRaises(ValueError), patch(
                            'cloud_backend.aws.task_handler._load_dependencies'
                    ) as dependencies:
                        importlib.reload(app_aws_worker)
                    dependencies.assert_not_called()
        finally:
            with patch.dict(os.environ, {'XSBOT_AWS_WORKER_KIND': 'action'}):
                importlib.reload(app_aws_worker)

    def test_両worker種別で初期化後にhealthを返す(self):
        for worker_kind in ('action', 'group_batch'):
            with self.subTest(worker_kind=worker_kind), patch.dict(
                    os.environ, {'XSBOT_AWS_WORKER_KIND': worker_kind}):
                importlib.reload(app_aws_worker)
                self.assertEqual(request_app('GET', '/healthz').status_int, 200)

    def test_workerにはhealthzとeventsだけを公開する(self):
        routes = {
            (route.method, route.rule)
            for route in app_aws_worker.app.routes
        }

        self.assertEqual(routes, {
            ('GET', '/healthz'),
            ('POST', '/events'),
        })
        self.assertEqual(request_app('GET', '/healthz').status_int, 200)
        self.assertEqual(request_app('GET', '/events').status_int, 405)
        self.assertEqual(request_app('GET', '/').status_int, 404)

    def test_eventsは封筒をlambda_handlerへ渡して成功を返す(self):
        event = {'version': 1, 'task_id': 'task-1', 'kind': 'action'}
        result = {'status': 'ok'}

        with patch.object(
                app_aws_worker, 'lambda_handler', return_value=result
        ) as handler:
            response = request_app(
                'POST',
                '/events',
                json.dumps(event).encode('utf-8'),
                'application/json',
            )

        self.assertEqual(response.status_int, 200)
        self.assertEqual(
            response.headers['Content-Type'],
            'application/json; charset=utf-8',
        )
        self.assertEqual(response.json, result)
        handler.assert_called_once_with(event, None)

    def test_JSON_object以外はhandlerを呼ばず500を返す(self):
        with patch.object(app_aws_worker, 'lambda_handler') as handler:
            malformed = request_app(
                'POST', '/events', b'{', 'application/json')
            array = request_app(
                'POST', '/events', b'[]', 'application/json')

        self.assertEqual(malformed.status_int, 500)
        self.assertEqual(array.status_int, 500)
        handler.assert_not_called()

    def test_handlerの例外はHTTP_500にする(self):
        with patch.object(
                app_aws_worker,
                'lambda_handler',
                side_effect=RuntimeError('処理失敗'),
        ):
            response = request_app(
                'POST', '/events', b'{"version": 1}', 'application/json')

        self.assertEqual(response.status_int, 500)


if __name__ == '__main__':
    unittest.main()
