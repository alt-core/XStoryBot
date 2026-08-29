# coding: utf-8

import hmac


api_token = ''


def check_token(token):
    if not api_token or not token:
        return False
    return hmac.compare_digest(token, api_token)


def get_api_token():
    return api_token


def setup(params):
    global api_token
    api_token = params.get('api_token', '') or ''


# TODO: 認可をもっと細かい粒度で行えるようにする
