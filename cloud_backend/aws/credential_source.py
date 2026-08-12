"""AWS Systems Manager Parameter Storeから資格情報を取得する。"""

import json
from collections.abc import Mapping

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from cloud_backend.contracts import (
    CredentialData,
    CredentialSource,
    CredentialSourceError,
)


class AwsCredentialSource(CredentialSource):
    """Google Sheets用SecureStringとローカル用参照を扱う。"""

    def __init__(self, aws_settings=None, client=None, client_factory=None):
        if aws_settings is None:
            import settings
            aws_settings = settings.BACKEND_SETTINGS

        credential_settings = aws_settings.get('credential_source', {})
        if not isinstance(credential_settings, Mapping):
            raise ValueError('AWS CredentialSourceの設定は辞書で指定してください')

        parameter_name = credential_settings.get(
            'google_service_account_parameter', '')
        if parameter_name is None:
            parameter_name = ''
        if not isinstance(parameter_name, str):
            raise ValueError(
                'AWS Google Sheets資格情報のParameter名は文字列で指定してください')

        self._region = aws_settings.get('region') or None
        self._google_service_account_parameter = parameter_name
        self._client_instance = client
        self._client_factory = client_factory or boto3.client
        self._parameter_cache = {}

    @staticmethod
    def _raise_source_error(error):
        if isinstance(error, (BotoCoreError, ClientError)):
            raise CredentialSourceError(
                'AWS Parameter Storeから資格情報を取得できませんでした') from error
        if type(error).__module__.startswith(('boto3.', 'botocore.')):
            raise CredentialSourceError(
                'AWS Parameter Storeから資格情報を取得できませんでした') from error
        raise error

    def _client(self):
        if self._client_instance is None:
            try:
                options = {}
                if self._region is not None:
                    options['region_name'] = self._region
                self._client_instance = self._client_factory('ssm', **options)
            except Exception as error:
                self._raise_source_error(error)
        return self._client_instance

    @staticmethod
    def _validate_inline_json(value):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as error:
            raise CredentialSourceError(
                'Googleサービスアカウント資格情報のJSONが不正です') from error
        if not isinstance(decoded, Mapping):
            raise CredentialSourceError(
                'Googleサービスアカウント資格情報はJSON objectで指定してください')
        return value

    def _load_google_service_account_parameter(self, parameter_name):
        cached = self._parameter_cache.get(parameter_name)
        if cached is not None:
            return cached

        try:
            result = self._client().get_parameter(
                Name=parameter_name,
                WithDecryption=True,
            )
        except Exception as error:
            self._raise_source_error(error)

        if not isinstance(result, Mapping):
            raise CredentialSourceError(
                'AWS Parameter Storeの資格情報応答が不正です')
        parameter = result.get('Parameter')
        if not isinstance(parameter, Mapping):
            raise CredentialSourceError(
                'AWS Parameter Storeの資格情報応答が不正です')
        if parameter.get('Type') != 'SecureString':
            raise CredentialSourceError(
                'Googleサービスアカウント資格情報はSecureStringで指定してください')

        value = parameter.get('Value')
        self._validate_inline_json(value)
        credential = CredentialData(inline_json=value)
        self._parameter_cache[parameter_name] = credential
        return credential

    @classmethod
    def _normalize_reference(cls, reference, allow_default):
        if isinstance(reference, CredentialData):
            return reference
        if isinstance(reference, Mapping):
            return CredentialData(
                inline_json=json.dumps(reference, ensure_ascii=False))
        if isinstance(reference, str):
            if allow_default and not reference:
                return CredentialData(use_default=True)
            if reference.lstrip().startswith('{'):
                cls._validate_inline_json(reference)
                return CredentialData(inline_json=reference)
            return CredentialData(file_path=reference)
        if reference is None:
            if allow_default:
                return CredentialData(use_default=True)
            return CredentialData(file_path=None)
        raise CredentialSourceError(
            '資格情報の参照はファイルパス、JSON、またはCredentialDataで指定してください')

    def get_admin_auth_credential(self):
        raise CredentialSourceError('AWSの共通フォーム認証はまだ実装されていません')

    def get_admin_auth_client_config(self):
        raise CredentialSourceError('AWSの共通フォーム認証はまだ実装されていません')

    def get_google_service_account(self, reference=None, allow_default=False):
        if self._google_service_account_parameter:
            return self._load_google_service_account_parameter(
                self._google_service_account_parameter)
        return self._normalize_reference(reference, allow_default)
