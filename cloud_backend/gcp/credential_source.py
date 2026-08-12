"""GCP環境で既存の資格情報参照を提供する。"""

import json
from collections.abc import Mapping

from cloud_backend.contracts import (
    CredentialData,
    CredentialSource,
    CredentialSourceError,
)


class GcpCredentialSource(CredentialSource):
    """ファイル参照とADCの既存契約をそのまま表現する。"""

    def __init__(self, auth_settings=None, gcp_settings=None):
        self._auth_settings = auth_settings
        self._gcp_settings = gcp_settings

    def get_admin_auth_credential(self):
        auth_settings = self._auth_settings
        if auth_settings is None:
            import settings
            auth_settings = settings.AUTH_SETTINGS
        return self.get_google_service_account(
            auth_settings['firebase_credentials_path'])

    def get_admin_auth_client_config(self):
        gcp_settings = self._gcp_settings
        if gcp_settings is None:
            import settings
            gcp_settings = settings.GCP_SETTINGS
        firebase_settings = gcp_settings['firebase']
        return {
            'apiKey': firebase_settings['api_key'],
            'authDomain': firebase_settings['auth_domain'],
            'projectId': gcp_settings['project_id'],
            'storageBucket': firebase_settings['storage_bucket'],
            'messagingSenderId': firebase_settings['messaging_sender_id'],
            'appId': firebase_settings['app_id'],
        }

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
            return CredentialData(file_path=reference)
        if reference is None:
            if allow_default:
                return CredentialData(use_default=True)
            return CredentialData(file_path=None)
        raise CredentialSourceError(
            '資格情報の参照はファイルパス、JSON、またはCredentialDataで指定してください')
