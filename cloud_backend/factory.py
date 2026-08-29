"""起動時に一度だけクラウド実装を選ぶ小さなfactory。"""

import importlib


_provider = None


def configure(cloud_settings=None):
    """プロセスで使用するプロバイダーを確定する。"""
    global _provider

    cloud_settings = cloud_settings or {}
    provider = cloud_settings.get('provider', 'gcp')
    if provider not in ('gcp', 'aws'):
        raise ValueError(f'未対応のクラウドプロバイダーです: {provider}')
    if _provider is not None and _provider != provider:
        raise RuntimeError('起動後にクラウドプロバイダーは変更できません')
    _provider = provider
    return _provider


def get_provider():
    """確定済みプロバイダーを返し、未設定ならGCPを選ぶ。"""
    if _provider is None:
        return configure()
    return _provider


def _provider_module():
    provider = get_provider()
    try:
        return importlib.import_module(f'cloud_backend.{provider}')
    except ModuleNotFoundError as error:
        if error.name == f'cloud_backend.{provider}':
            raise ValueError(
                f'クラウドプロバイダーはまだ実装されていません: {provider}') from error
        raise


def create_state_store():
    return _provider_module().create_state_store()


def create_object_store():
    return _provider_module().create_object_store()


def create_task_queue():
    return _provider_module().create_task_queue()


def create_credential_source():
    return _provider_module().create_credential_source()


def _reset_for_test():
    global _provider
    _provider = None
