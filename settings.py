# coding: utf-8

import os

from cloud_backend import configure as configure_cloud_backend
from utility import deep_merge, load_settings_yaml


DEPLOY_ENV = os.getenv('XSBOT_DEPLOY_ENV', '')


def load_settings():
    settings = load_settings_yaml('settings.yaml')
    default_settings = settings.get('*', {})
    env_settings = settings.get(DEPLOY_ENV, {})
    return deep_merge(default_settings, env_settings)


# 設定の読み込み
settings = load_settings()
_provider_from_environment = os.getenv('XSBOT_CLOUD_PROVIDER')
_configured_provider = (
    _provider_from_environment
    or settings.get('cloud', {}).get('provider', 'gcp')
)

# AWSではSecureString展開後に!envを解決し直す。GCPは従来どおり一度だけ読む。
if _configured_provider == 'aws':
    from cloud_backend.aws.runtime_secrets import load_runtime_secrets
    load_runtime_secrets()
    settings = load_settings()

CLOUD_SETTINGS = dict(settings.get('cloud', {'provider': 'gcp'}))
if _provider_from_environment:
    CLOUD_SETTINGS['provider'] = _provider_from_environment
_cloud_provider = CLOUD_SETTINGS.get('provider', 'gcp')

# GCP選択時は従来どおりgcp設定を必須とし、他provider選択時だけ省略を許す。
if _cloud_provider == 'gcp':
    GCP_SETTINGS = settings['gcp']
else:
    GCP_SETTINGS = settings.get('gcp', {})
BACKEND_SETTINGS = settings.get(_cloud_provider, {})
SERVICE_SETTINGS = settings.get(
    'services', BACKEND_SETTINGS.get('services', {}))
AUTH_SETTINGS = settings['auth']
OPTIONS = settings.get('options', {})
PLUGINS = settings.get('plugins', {})
BOTS = settings['bots']
CONSTANTS = settings.get('constants', {})


# 設定を読む時点で、このプロセスが使うクラウドを一度だけ確定する。
configure_cloud_backend(CLOUD_SETTINGS)
