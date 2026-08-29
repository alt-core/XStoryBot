"""GCP環境の資格情報参照を提供する。"""

import json
import os
from collections.abc import Mapping

from cloud_backend.contracts import (
    CredentialData,
    CredentialSource,
    CredentialSourceError,
)


class GcpCredentialSource(CredentialSource):
    """環境変数とGoogle資格情報参照を扱う。"""

    def __init__(self, auth_settings=None, gcp_settings=None, environ=None):
        self._auth_settings = auth_settings
        self._gcp_settings = gcp_settings
        self._environ = os.environ if environ is None else environ

    def get_admin_auth_json(self):
        auth_settings = self._auth_settings
        if auth_settings is None:
            import settings
            auth_settings = settings.AUTH_SETTINGS
        environment_name = auth_settings.get(
            'admin_auth_json_env', 'XSBOT_ADMIN_AUTH_JSON')
        if not isinstance(environment_name, str) or not environment_name:
            raise CredentialSourceError(
                '管理者認証JSONの環境変数名を設定してください')
        value = self._environ.get(environment_name, '')
        if not value:
            raise CredentialSourceError('管理者認証JSONが設定されていません')
        return value

    def get_google_service_account(self, reference=None, allow_default=False):
        if isinstance(reference, CredentialData):
            return reference
        if isinstance(reference, Mapping):
            return CredentialData(
                inline_json=json.dumps(reference, ensure_ascii=False))
        if isinstance(reference, str):
            if allow_default and not reference:
                return CredentialData(use_default=True)
            if reference.lstrip().startswith('{'):
                try:
                    decoded = json.loads(reference)
                except ValueError as error:
                    raise CredentialSourceError(
                        'Googleサービスアカウント資格情報のJSONが不正です') from error
                if not isinstance(decoded, Mapping):
                    raise CredentialSourceError(
                        'Googleサービスアカウント資格情報はJSON objectで指定してください')
                return CredentialData(inline_json=reference)
            if reference.lstrip().startswith('['):
                raise CredentialSourceError(
                    'Googleサービスアカウント資格情報はJSON objectで指定してください')
            return CredentialData(file_path=reference)
        if reference is None:
            if allow_default:
                return CredentialData(use_default=True)
            return CredentialData(file_path=None)
        raise CredentialSourceError(
            '資格情報の参照はファイルパス、JSON、またはCredentialDataで指定してください')
