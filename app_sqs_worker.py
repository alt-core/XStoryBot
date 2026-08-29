"""Lambda Web AdapterからSQS eventを受け取る専用アプリ。"""

import json

from bottle import Bottle, abort, request, response

from cloud_backend.aws.task_handler import lambda_handler


app = Bottle()


@app.get('/healthz')
def health_check():
    response.set_header('Content-Type', 'application/json; charset=utf-8')
    return '{"status":"ok"}'


@app.post('/events')
def handle_event():
    try:
        event = json.loads(request.body.read())
    except (TypeError, ValueError):
        abort(500, 'SQS eventが不正です')
    if not isinstance(event, dict):
        abort(500, 'SQS eventが不正です')

    result = lambda_handler(event, None)
    response.set_header('Content-Type', 'application/json; charset=utf-8')
    return json.dumps(result, ensure_ascii=False)
