"""Lambda Web Adapterから非同期タスクの封筒を受け取る専用アプリ。"""

import json
import os

from bottle import Bottle, abort, request, response

from cloud_backend.aws.task_handler import lambda_handler, validate_worker_kind


validate_worker_kind(os.environ.get('XSBOT_AWS_WORKER_KIND'))

# workerも設定と送信先を初期化し、不正な構成でreadinessを成功させない。
import main

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
        abort(500, 'AWS taskの封筒が不正です')
    if not isinstance(event, dict):
        abort(500, 'AWS taskの封筒が不正です')

    result = lambda_handler(event, None)
    response.set_header('Content-Type', 'application/json; charset=utf-8')
    return json.dumps(result, ensure_ascii=False)
