"""AWS実行時秘密値を設定読込前に環境変数へ展開する。"""

import json
import os
from collections.abc import Mapping

import boto3


RUNTIME_SECRETS_PARAMETER_ENV = 'XSBOT_AWS_RUNTIME_SECRETS_PARAMETER'

# settings.yaml.templateがAPI・各プラグインへ渡す実行時資格情報だけを許可する。
ALLOWED_ENVIRONMENT_NAMES = frozenset({
    'XSBOT_API_TOKEN',
    'LINE_ACCESS_TOKEN',
    'LINE_CHANNEL_SECRET',
    'OPENAI_API_KEY',
    'TWILIO_SID',
    'TWILIO_AUTH_TOKEN',
    'TWILIO_PHONE_NUMBER',
    'PUSHER_APP_ID',
    'PUSHER_APP_KEY',
    'PUSHER_APP_SECRET',
    'PUSHER_APP_CLUSTER',
})


class RuntimeSecretsError(RuntimeError):
    """AWS実行時秘密値を安全に読み込めなかったことを表す。"""


_client_instance = None
_cached_secrets = None


def _raise_fetch_error():
    # SDKのservice messageやParameter名を起動ログへ連鎖表示しない。
    raise RuntimeSecretsError(
        'AWS Parameter Storeから実行時秘密値を取得できませんでした'
    ) from None


def _get_client(region, client_factory):
    global _client_instance

    if _client_instance is None:
        try:
            factory = client_factory or boto3.client
            _client_instance = factory('ssm', region_name=region)
        except Exception:
            _raise_fetch_error()
    return _client_instance


def _decode_secrets(response):
    if not isinstance(response, Mapping):
        raise RuntimeSecretsError(
            'AWS Parameter Storeの実行時秘密値応答が不正です')
    parameter = response.get('Parameter')
    if not isinstance(parameter, Mapping):
        raise RuntimeSecretsError(
            'AWS Parameter Storeの実行時秘密値応答が不正です')
    if parameter.get('Type') != 'SecureString':
        raise RuntimeSecretsError(
            'AWS実行時秘密値はSecureStringで指定してください')

    try:
        decoded = json.loads(parameter.get('Value'))
    except (TypeError, ValueError):
        raise RuntimeSecretsError(
            'AWS実行時秘密値JSONが不正です') from None
    if not isinstance(decoded, Mapping):
        raise RuntimeSecretsError(
            'AWS実行時秘密値はJSON objectで指定してください')
    if any(name not in ALLOWED_ENVIRONMENT_NAMES for name in decoded):
        raise RuntimeSecretsError(
            'AWS実行時秘密値JSONに許可されていない項目があります')
    if any(not isinstance(value, str) for value in decoded.values()):
        raise RuntimeSecretsError(
            'AWS実行時秘密値JSONの値は文字列で指定してください')
    return dict(decoded)


def load_runtime_secrets(environ=None, client=None, client_factory=None):
    """SecureString 1件を読み、許可済みの環境変数へ展開する。"""
    global _client_instance, _cached_secrets

    target_environ = os.environ if environ is None else environ
    parameter_name = target_environ.get(RUNTIME_SECRETS_PARAMETER_ENV, '')
    if not parameter_name:
        return
    if not isinstance(parameter_name, str):
        raise RuntimeSecretsError(
            'AWS実行時秘密値のParameter名は文字列で指定してください')

    if _cached_secrets is not None:
        target_environ.update(_cached_secrets)
        return

    region = target_environ.get('AWS_REGION', '')
    if not isinstance(region, str) or not region:
        raise RuntimeSecretsError('AWS_REGIONを設定してください')

    if client is not None:
        _client_instance = client
    ssm_client = _get_client(region, client_factory)
    try:
        response = ssm_client.get_parameter(
            Name=parameter_name,
            WithDecryption=True,
        )
    except Exception:
        _raise_fetch_error()

    secrets = _decode_secrets(response)
    target_environ.update(secrets)
    _cached_secrets = secrets


def _reset_for_test():
    global _client_instance, _cached_secrets
    _client_instance = None
    _cached_secrets = None
