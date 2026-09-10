"""明示した環境変数と資格情報ファイルだけを扱う。"""

import os

from cloud_backend.contracts import (
    CredentialData, CredentialSource, CredentialSourceError,
)


class LocalCredentialSource(CredentialSource):
    def __init__(self, auth_settings=None, environ=None):
        self._auth_settings = auth_settings or {}
        self._environ = os.environ if environ is None else environ

    def get_admin_auth_json(self):
        name = self._auth_settings.get('admin_auth_json_env', 'XSBOT_ADMIN_AUTH_JSON')
        if not isinstance(name, str) or not name:
            raise CredentialSourceError('管理者認証JSONの環境変数名を設定してください')
        value = self._environ.get(name)
        if not value:
            raise CredentialSourceError('管理者認証JSONが設定されていません')
        return value

    def get_google_service_account(self, reference=None, allow_default=False):
        if isinstance(reference, CredentialData):
            if reference.inline_json is not None or reference.use_default:
                raise CredentialSourceError('localでは資格情報ファイルを明示してください')
            reference = reference.file_path
        if (not isinstance(reference, str) or not reference.strip()
                or reference.lstrip().startswith(('{', '['))):
            raise CredentialSourceError('localでは資格情報ファイルを明示してください')
        return CredentialData(file_path=reference)
