# coding: utf-8

import os

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
AUTH_SETTINGS = settings['auth']
OPTIONS = settings.get('options', {})
PLUGINS = settings.get('plugins', {})
BOTS = settings['bots']
CONSTANTS = settings.get('constants', {})
