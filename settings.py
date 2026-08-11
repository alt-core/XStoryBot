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

# 各種設定を直接参照
GCP_SETTINGS = settings['gcp']
CLOUD_SETTINGS = settings.get('cloud', {'provider': 'gcp'})
BACKEND_SETTINGS = settings.get(CLOUD_SETTINGS.get('provider', 'gcp'), {})
SERVICE_SETTINGS = settings.get(
    'services', BACKEND_SETTINGS.get('services', {}))
AUTH_SETTINGS = settings['auth']
OPTIONS = settings.get('options', {})
PLUGINS = settings.get('plugins', {})
BOTS = settings['bots']
CONSTANTS = settings.get('constants', {})


# 設定を読む時点で、このプロセスが使うクラウドを一度だけ確定する。
configure_cloud_backend(CLOUD_SETTINGS)
