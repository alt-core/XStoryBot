import json
import sys
import types
import unittest
from unittest.mock import Mock, patch

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from botocore.stub import Stubber

from cloud_backend import aws as aws_backend
from cloud_backend.aws.credential_source import AwsCredentialSource
from cloud_backend.contracts import CredentialData, CredentialSourceError


PARAMETER_NAME = '/xstorybot/test/google-sheets-service-account'
ADMIN_PARAMETER_NAME = '/xstorybot/test/admin-auth'
SERVICE_ACCOUNT_JSON = json.dumps({
    'type': 'service_account',
    'project_id': 'test-project',
}, separators=(',', ':'))
AWS_SETTINGS = {
    'region': 'ap-northeast-1',
    'credential_source': {
        'google_service_account_parameter': PARAMETER_NAME,
        'admin_auth_parameter': ADMIN_PARAMETER_NAME,
    },
}


def make_ssm_client():
    return boto3.client(
        'ssm',
        region_name='ap-northeast-1',
        aws_access_key_id='test-access-key',
        aws_secret_access_key='test-secret-key',
        aws_session_token='test-session-token',
    )


class AwsCredentialSourceTest(unittest.TestCase):
    def test_SDK_clientは初回取得まで生成しない(self):
        client = Mock()
        client.get_parameter.return_value = {
            'Parameter': {
                'Type': 'SecureString',
                'Value': SERVICE_ACCOUNT_JSON,
            },
        }
        client_factory = Mock(return_value=client)
        source = AwsCredentialSource(
            AWS_SETTINGS, client_factory=client_factory)

        client_factory.assert_not_called()
        source.get_google_service_account('/local/fallback.json')

        client_factory.assert_called_once_with(
            'ssm', region_name='ap-northeast-1')

    def test_SecureStringを復号してinline_JSONとしてcacheする(self):
        client = make_ssm_client()
        stubber = Stubber(client)
        stubber.add_response('get_parameter', {
            'Parameter': {
                'Name': PARAMETER_NAME,
                'Type': 'SecureString',
                'Value': SERVICE_ACCOUNT_JSON,
                'Version': 1,
            },
        }, {
            'Name': PARAMETER_NAME,
            'WithDecryption': True,
        })
        source = AwsCredentialSource(AWS_SETTINGS, client=client)

        with stubber:
            first = source.get_google_service_account('/local/fallback.json')
            second = source.get_google_service_account(
                '{"project_id":"other-fallback"}')

        self.assertIs(first, second)
        self.assertEqual(
            {'type': 'service_account', 'project_id': 'test-project'},
            json.loads(first.inline_json),
        )
        self.assertIsNone(first.file_path)
        self.assertFalse(first.use_default)

    def test_Parameter未設定時は既存参照へfallbackする(self):
        client_factory = Mock()
        source = AwsCredentialSource(
            {'region': 'ap-northeast-1'}, client_factory=client_factory)
        original = CredentialData(inline_json=SERVICE_ACCOUNT_JSON)

        self.assertIs(
            original, source.get_google_service_account(original))
        mapped = source.get_google_service_account({'project_id': 'mapped'})
        inline = source.get_google_service_account(
            '  {"project_id":"inline"}')
        file_credential = source.get_google_service_account('/keys/sheets.json')
        required = source.get_google_service_account('', allow_default=False)
        optional = source.get_google_service_account('', allow_default=True)
        missing = source.get_google_service_account(None, allow_default=False)
        default = source.get_google_service_account(None, allow_default=True)

        self.assertEqual(
            {'project_id': 'mapped'}, json.loads(mapped.inline_json))
        self.assertEqual(
            {'project_id': 'inline'}, json.loads(inline.inline_json))
        self.assertEqual('/keys/sheets.json', file_credential.file_path)
        self.assertEqual('', required.file_path)
        self.assertFalse(required.use_default)
        self.assertTrue(optional.use_default)
        self.assertIsNone(missing.file_path)
        self.assertFalse(missing.use_default)
        self.assertTrue(default.use_default)
        client_factory.assert_not_called()

    def test_不明なfallback参照型を拒否する(self):
        source = AwsCredentialSource({})

        with self.assertRaises(CredentialSourceError):
            source.get_google_service_account(123)

    def test_資格情報設定の型を検証する(self):
        invalid_settings = (
            {'credential_source': []},
            {'credential_source': {
                'google_service_account_parameter': 123,
            }},
            {'credential_source': {
                'admin_auth_parameter': 123,
            }},
        )

        for aws_settings in invalid_settings:
            with self.subTest(aws_settings=aws_settings):
                with self.assertRaises(ValueError):
                    AwsCredentialSource(aws_settings)

    def test_SecureString以外と不正JSONを秘密値なしで拒否する(self):
        invalid_responses = (
            None,
            {},
            {'Parameter': []},
            {'Parameter': {
                'Type': 'String',
                'Value': SERVICE_ACCOUNT_JSON,
            }},
            {'Parameter': {
                'Type': 'SecureString',
                'Value': '{"private_key":"secret-value"',
            }},
            {'Parameter': {
                'Type': 'SecureString',
                'Value': '["secret-value"]',
            }},
        )

        for response in invalid_responses:
            with self.subTest(response_type=type(response).__name__):
                client = Mock()
                client.get_parameter.return_value = response
                source = AwsCredentialSource(AWS_SETTINGS, client=client)

                with self.assertRaises(CredentialSourceError) as raised:
                    source.get_google_service_account()

                self.assertNotIn('secret-value', str(raised.exception))

    def test_AWS例外を秘密値なしの共通例外へ正規化する(self):
        errors = (
            ClientError(
                {
                    'Error': {
                        'Code': 'ParameterNotFound',
                        'Message': 'parameter missing',
                    },
                },
                'GetParameter',
            ),
            NoCredentialsError(),
        )

        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                client = Mock()
                client.get_parameter.side_effect = error
                source = AwsCredentialSource(AWS_SETTINGS, client=client)

                with self.assertRaises(CredentialSourceError) as raised:
                    source.get_google_service_account()

                self.assertNotIn(PARAMETER_NAME, str(raised.exception))

    def test_AWS以外の例外は隠さない(self):
        client = Mock()
        client.get_parameter.side_effect = RuntimeError('application error')
        source = AwsCredentialSource(AWS_SETTINGS, client=client)

        with self.assertRaisesRegex(RuntimeError, 'application error'):
            source.get_google_service_account()

    def test_失敗した取得結果はcacheしない(self):
        client = Mock()
        client.get_parameter.side_effect = (
            ClientError(
                {'Error': {'Code': 'ThrottlingException', 'Message': 'retry'}},
                'GetParameter',
            ),
            {
                'Parameter': {
                    'Type': 'SecureString',
                    'Value': SERVICE_ACCOUNT_JSON,
                },
            },
        )
        source = AwsCredentialSource(AWS_SETTINGS, client=client)

        with self.assertRaises(CredentialSourceError):
            source.get_google_service_account()
        credential = source.get_google_service_account()

        self.assertEqual('test-project', json.loads(
            credential.inline_json)['project_id'])
        self.assertEqual(2, client.get_parameter.call_count)

    def test_管理者認証JSONをSecureStringから取得してcacheする(self):
        client = Mock()
        client.get_parameter.return_value = {
            'Parameter': {
                'Type': 'SecureString',
                'Value': '{"users":{"admin":"hash"}}',
            },
        }
        source = AwsCredentialSource(AWS_SETTINGS, client=client)

        first = source.get_admin_auth_json()
        second = source.get_admin_auth_json()

        self.assertEqual('{"users":{"admin":"hash"}}', first)
        self.assertEqual(first, second)
        client.get_parameter.assert_called_once_with(
            Name=ADMIN_PARAMETER_NAME,
            WithDecryption=True,
        )

    def test_管理者Parameter未指定時だけ環境変数へfallbackする(self):
        source = AwsCredentialSource(
            {'region': 'ap-northeast-1'},
            environ={'TEST_ADMIN_AUTH': '{"users":{}}'},
            auth_settings={'admin_auth_json_env': 'TEST_ADMIN_AUTH'},
        )

        self.assertEqual('{"users":{}}', source.get_admin_auth_json())

    def test_管理者Parameter取得失敗時は環境変数へfallbackしない(self):
        client = Mock()
        client.get_parameter.side_effect = ClientError(
            {'Error': {'Code': 'ParameterNotFound', 'Message': 'missing'}},
            'GetParameter',
        )
        source = AwsCredentialSource(
            AWS_SETTINGS,
            client=client,
            environ={'XSBOT_ADMIN_AUTH_JSON': '{"users":{}}'},
            auth_settings={},
        )

        with self.assertRaises(CredentialSourceError):
            source.get_admin_auth_json()

    def test_管理者認証ParameterはSecureStringだけ受け入れる(self):
        invalid_responses = (
            None,
            {'Parameter': {'Type': 'String', 'Value': 'secret-value'}},
            {'Parameter': {'Type': 'SecureString', 'Value': ''}},
        )
        for result in invalid_responses:
            with self.subTest(result=result):
                client = Mock()
                client.get_parameter.return_value = result
                source = AwsCredentialSource(AWS_SETTINGS, client=client)

                with self.assertRaises(CredentialSourceError) as raised:
                    source.get_admin_auth_json()

                self.assertNotIn('secret-value', str(raised.exception))


    def test_provider内ではCredentialSourceを共有する(self):
        credential_source = Mock()
        original = aws_backend._credential_source
        aws_backend._credential_source = None
        try:
            with (
                patch.dict(
                    sys.modules,
                    {'settings': types.SimpleNamespace(
                        BACKEND_SETTINGS=AWS_SETTINGS,
                        AUTH_SETTINGS={'admin_auth_json_env': 'TEST_ADMIN'},
                    )},
                ),
                patch(
                    'cloud_backend.aws.credential_source.AwsCredentialSource',
                    return_value=credential_source,
                ) as constructor,
            ):
                first = aws_backend.create_credential_source()
                second = aws_backend.create_credential_source()
        finally:
            aws_backend._credential_source = original

        self.assertIs(first, credential_source)
        self.assertIs(second, credential_source)
        constructor.assert_called_once_with(
            AWS_SETTINGS,
            auth_settings={'admin_auth_json_env': 'TEST_ADMIN'},
        )


if __name__ == '__main__':
    unittest.main()
