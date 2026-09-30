# coding: utf-8

import os

from cloud_backend import configure as configure_cloud_backend
from utility import deep_merge, load_settings_yaml, normalize_constants


DEPLOY_ENV = os.getenv('XSBOT_DEPLOY_ENV', '')


def load_settings(env=None):
    settings = load_settings_yaml(os.getenv('XSBOT_SETTINGS_FILE', 'settings.yaml'), env=env)
    default_settings = settings.get('*', {})
    env_settings = settings.get(DEPLOY_ENV, {})
    return deep_merge(default_settings, env_settings)


def _deployment_settings(config):
    """追加設定が基盤・署名・公開条件を変更していないことを確認する対象。"""
    protected = {key: config.get(key) for key in ('cloud', 'gcp', 'aws', 'local', 'services')}
    fields = (
        'enabled', 'deployment', 'signing_key', 'scenario_uri', 'scenario_compatibility_epoch',
        'start_action', 'self_origin', 'allowed_origins', 'external_http_origins', 'media_origins',
        'allowed_commands', 'turn_deadline_seconds', 'liff_apps',
    )
    common = {**config.get('options', {}), **config.get('plugins', {}).get('webchat', {})}
    protected['webchat'] = {'common': {key: common.get(key) for key in fields}, 'bots': {}}
    for name, bot in config.get('bots', {}).items():
        interfaces = []
        for interface in bot.get('interfaces', []):
            if interface.get('type') == 'webchat':
                params = {**common, **(interface.get('params') or {})}
                interfaces.append({key: params.get(key) for key in fields})
        protected['webchat']['bots'][name] = interfaces
    return protected


# 設定の読み込み
settings = load_settings()
_provider_from_environment = os.getenv('XSBOT_CLOUD_PROVIDER')
_configured_provider = (
    _provider_from_environment
    or settings.get('cloud', {}).get('provider')
)
if _configured_provider not in ('gcp', 'aws', 'local'):
    raise ValueError(
        'クラウドプロバイダーをXSBOT_CLOUD_PROVIDERまたは'
        'cloud.providerへgcp／aws／localで明示してください')

# AWSの追加値は!env解決にだけ使い、基盤とWebchatの公開設定は起動時の値に固定する。
if (
        _configured_provider == 'aws'
        and os.getenv('XSBOT_AWS_RUNTIME_SECRETS_PARAMETER')):
    from cloud_backend.aws.runtime_secrets import load_runtime_secrets, RuntimeSecretsError
    runtime_values = load_runtime_secrets()
    try:
        updated_settings = load_settings(env=runtime_values)
    except Exception:
        # !formatの例外には追加値が含まれることがあるため、起動ログへ連鎖表示しない。
        raise RuntimeSecretsError('AWS追加設定を!env・!formatで解決できませんでした') from None
    if _deployment_settings(updated_settings) != _deployment_settings(settings):
        raise RuntimeSecretsError('AWS追加設定で基盤またはWebchatの公開設定は変更できません')
    settings = updated_settings

CLOUD_SETTINGS = dict(settings.get('cloud', {}))
if _provider_from_environment:
    CLOUD_SETTINGS['provider'] = _provider_from_environment
_cloud_provider = CLOUD_SETTINGS.get('provider')

# GCP選択時はgcp設定を必須とし、他provider選択時だけ省略を許す。
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
CONSTANTS = normalize_constants(settings.get('constants', {}))


# 設定を読む時点で、このプロセスが使うクラウドを一度だけ確定する。
configure_cloud_backend(CLOUD_SETTINGS)
