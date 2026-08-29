import io
import json
import unittest
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults

import app_sqs_worker


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

    result = app_sqs_worker.app(environ, start_response)
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


class SqsWorkerAppTest(unittest.TestCase):
    def test_workerにはhealthzとeventsだけを公開する(self):
        routes = {
            (route.method, route.rule)
            for route in app_sqs_worker.app.routes
        }

        self.assertEqual(routes, {
            ('GET', '/healthz'),
            ('POST', '/events'),
        })
        self.assertEqual(request_app('GET', '/healthz').status_int, 200)
        self.assertEqual(request_app('GET', '/events').status_int, 405)
        self.assertEqual(request_app('GET', '/').status_int, 404)

    def test_eventsは生eventをlambda_handlerへ渡して結果を返す(self):
        event = {'Records': [{'messageId': 'message-1'}]}
        result = {
            'batchItemFailures': [{'itemIdentifier': 'message-1'}],
        }

        with patch.object(
                app_sqs_worker, 'lambda_handler', return_value=result
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
        with patch.object(app_sqs_worker, 'lambda_handler') as handler:
            malformed = request_app(
                'POST', '/events', b'{', 'application/json')
            array = request_app(
                'POST', '/events', b'[]', 'application/json')

        self.assertEqual(malformed.status_int, 500)
        self.assertEqual(array.status_int, 500)
        handler.assert_not_called()

    def test_handlerの例外はHTTP_500にする(self):
        with patch.object(
                app_sqs_worker,
                'lambda_handler',
                side_effect=RuntimeError('処理失敗'),
        ):
            response = request_app(
                'POST', '/events', b'{"Records": []}', 'application/json')

        self.assertEqual(response.status_int, 500)


if __name__ == '__main__':
    unittest.main()
