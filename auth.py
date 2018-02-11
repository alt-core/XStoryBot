# coding: utf-8


api_token = None


def check_token(token):
    return token == api_token


def setup(params):
    global api_token
    api_token = params['api_token']


# TODO: 認可をもっと細かい粒度で行えるようにする
