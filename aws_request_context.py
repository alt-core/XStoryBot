"""Web Adapterが渡すLambda contextの読取り。"""

import json
import math
import os
import time

from bottle import request


FINISH_RESERVE_SECONDS = 2.0
API_TIMEOUT_SECONDS = 30.0


def lambda_context():
    cached = request.environ.get('xstorybot.lambda_context')
    if isinstance(cached, dict):
        return cached
    raw = request.headers.get('X-Amzn-Lambda-Context')
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    request.environ['xstorybot.lambda_context'] = value
    return value


def get_lambda_deadline():
    """同期APIの終了処理に余裕を残したmonotonic期限を返す。"""
    if not os.getenv('AWS_LAMBDA_FUNCTION_NAME'):
        return None
    deadline = lambda_context().get('deadline')
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise RuntimeError('Lambdaの処理期限を取得できません')
    raw = request.headers.get('X-Amzn-Request-Context')
    if raw:
        try:
            gateway = json.loads(raw)
        except (TypeError, ValueError):
            raise RuntimeError('API Gatewayの要求contextを取得できません') from None
        request_time = gateway.get('timeEpoch') if isinstance(gateway, dict) else None
        if type(request_time) in (int, float) and math.isfinite(request_time):
            # API Gatewayの待ち時間にはLambdaの初期化待ちも含まれる。
            deadline = min(deadline, request_time + API_TIMEOUT_SECONDS * 1000)
    remaining = deadline / 1000.0 - time.time()
    return time.monotonic() + max(0.0, remaining) - FINISH_RESERVE_SECONDS
