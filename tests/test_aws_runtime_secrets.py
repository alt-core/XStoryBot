import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import boto3
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from cloud_backend import factory as cloud_backend_factory
from cloud_backend.aws import runtime_secrets
from cloud_backend.aws.runtime_secrets import (
    ALLOWED_ENVIRONMENT_NAMES,
    RuntimeSecretsError,
    load_runtime_secrets,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARAMETER_NAME = '/xstorybot/test/runtime-secrets'


def make_ssm_client():
    return boto3.client(
        'ssm',
        region_name='ap-northeast-1',
        aws_access_key_id='test-access-key',
        aws_secret_access_key='test-secret-key',
        aws_session_token='test-session-token',
    )


class AwsRuntimeSecretsTest(unittest.TestCase):
    def setUp(self):
        runtime_secrets._reset_for_test()

    def tearDown(self):
        runtime_secrets._reset_for_test()

    def test_Parameter未指定時は既存環境変数を変更しない(self):
        environ = {'XSBOT_API_TOKEN': 'existing-token'}
        client_factory = Mock()

        load_runtime_secrets(
            environ=environ, client_factory=client_factory)

        self.assertEqual('existing-token', environ['XSBOT_API_TOKEN'])
        client_factory.assert_not_called()

    def test_SecureStringを復号して既存環境変数より優先しcacheする(self):
        client = make_ssm_client()
        stubber = Stubber(client)
        value = json.dumps({
            'XSBOT_API_TOKEN': 'parameter-token',
            'LINE_CHANNEL_SECRET': 'parameter-channel-secret',
        })
        stubber.add_response('get_parameter', {
            'Parameter': {
                'Name': PARAMETER_NAME,
                'Type': 'SecureString',
                'Value': value,
                'Version': 1,
            },
        }, {
            'Name': PARAMETER_NAME,
            'WithDecryption': True,
        })
        environ = {
            'AWS_REGION': 'ap-northeast-1',
            'XSBOT_AWS_RUNTIME_SECRETS_PARAMETER': PARAMETER_NAME,
            'XSBOT_API_TOKEN': 'existing-token',
        }

        with stubber:
            load_runtime_secrets(environ=environ, client=client)
            cached_environ = {
                'AWS_REGION': 'ap-northeast-1',
                'XSBOT_AWS_RUNTIME_SECRETS_PARAMETER': PARAMETER_NAME,
            }
            load_runtime_secrets(environ=cached_environ)

        self.assertEqual('parameter-token', environ['XSBOT_API_TOKEN'])
        self.assertEqual(
            'parameter-channel-secret', environ['LINE_CHANNEL_SECRET'])
        self.assertEqual(
            'parameter-token', cached_environ['XSBOT_API_TOKEN'])

    def test_SDK_clientはParameter取得時まで生成せずAWS_REGIONを使う(self):
        client = Mock()
        client.get_parameter.return_value = {
            'Parameter': {
                'Type': 'SecureString',
                'Value': '{"XSBOT_API_TOKEN":"token"}',
            },
        }
        client_factory = Mock(return_value=client)
        environ = {
            'AWS_REGION': 'ap-northeast-1',
            'XSBOT_AWS_RUNTIME_SECRETS_PARAMETER': PARAMETER_NAME,
        }

        client_factory.assert_not_called()
        load_runtime_secrets(
            environ=environ, client_factory=client_factory)

        client_factory.assert_called_once_with(
            'ssm', region_name='ap-northeast-1')

    def test_許可対象は設定templateの実行時資格情報に限定する(self):
        self.assertEqual({
            'XSBOT_API_TOKEN',
            'LINE_ACCESS_TOKEN',
            'LINE_CHANNEL_SECRET',
            'OPENAI_API_KEY',
            'TWILIO_SID',
            'TWILIO_AUTH_TOKEN',
            'TWILIO_PHONE_NUMBER',
            'PUSHER_APP_ID',
            'PUSHER_APP_KEY',
            'PUSHER_APP_SECRET',
            'PUSHER_APP_CLUSTER',
        }, set(ALLOWED_ENVIRONMENT_NAMES))
        self.assertNotIn('XSBOT_ADMIN_AUTH_JSON', ALLOWED_ENVIRONMENT_NAMES)
        self.assertNotIn('SHEETS_SERVICE_ACCOUNT', ALLOWED_ENVIRONMENT_NAMES)

    def test_不正な応答を秘密値なしで拒否する(self):
        invalid_responses = (
            None,
            {},
            {'Parameter': []},
            {'Parameter': {
                'Type': 'String',
                'Value': '{"XSBOT_API_TOKEN":"secret-value"}',
            }},
            {'Parameter': {
                'Type': 'SecureString',
                'Value': '{"XSBOT_API_TOKEN":"secret-value"',
            }},
            {'Parameter': {
                'Type': 'SecureString',
                'Value': '["secret-value"]',
            }},
            {'Parameter': {
                'Type': 'SecureString',
                'Value': '{"UNEXPECTED_SECRET":"secret-value"}',
            }},
            {'Parameter': {
                'Type': 'SecureString',
                'Value': '{"XSBOT_API_TOKEN":123}',
            }},
        )

        for response in invalid_responses:
            with self.subTest(response_type=type(response).__name__):
                runtime_secrets._reset_for_test()
                client = Mock()
                client.get_parameter.return_value = response
                environ = {
                    'AWS_REGION': 'ap-northeast-1',
                    'XSBOT_AWS_RUNTIME_SECRETS_PARAMETER': PARAMETER_NAME,
                }

                with self.assertRaises(RuntimeSecretsError) as raised:
                    load_runtime_secrets(environ=environ, client=client)

                self.assertNotIn('secret-value', str(raised.exception))
                self.assertNotIn('UNEXPECTED_SECRET', str(raised.exception))

    def test_SDK例外のParameter名とservice_messageを公開しない(self):
        client = Mock()
        client.get_parameter.side_effect = ClientError(
            {
                'Error': {
                    'Code': 'ParameterNotFound',
                    'Message': 'private service message',
                },
            },
            'GetParameter',
        )
        environ = {
            'AWS_REGION': 'ap-northeast-1',
            'XSBOT_AWS_RUNTIME_SECRETS_PARAMETER': PARAMETER_NAME,
        }

        with self.assertRaises(RuntimeSecretsError) as raised:
            load_runtime_secrets(environ=environ, client=client)

        self.assertNotIn(PARAMETER_NAME, str(raised.exception))
        self.assertNotIn('private service message', str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)

    def test_Parameter指定時はAWS_REGIONを必須にする(self):
        client_factory = Mock()

        with self.assertRaisesRegex(RuntimeSecretsError, 'AWS_REGION'):
            load_runtime_secrets(
                environ={
                    'XSBOT_AWS_RUNTIME_SECRETS_PARAMETER': PARAMETER_NAME,
                },
                client_factory=client_factory,
            )

        client_factory.assert_not_called()


class SettingsRuntimeSecretsIntegrationTest(unittest.TestCase):
    def setUp(self):
        runtime_secrets._reset_for_test()
        cloud_backend_factory._reset_for_test()

    def tearDown(self):
        runtime_secrets._reset_for_test()
        cloud_backend_factory._reset_for_test()

    def _load_settings_module(self, settings_text, environment, module_name,
                              modules=None):
        module_path = PROJECT_ROOT / 'settings.py'
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, 'settings.yaml').write_text(
                settings_text, encoding='utf-8')
            previous_dir = os.getcwd()
            try:
                os.chdir(temp_dir)
                spec = importlib.util.spec_from_file_location(
                    module_name, module_path)
                module = importlib.util.module_from_spec(spec)
                patched_modules = {module_name: module}
                if modules:
                    patched_modules.update(modules)
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.dict(sys.modules, patched_modules),
                ):
                    spec.loader.exec_module(module)
            finally:
                os.chdir(previous_dir)
                sys.modules.pop(module_name, None)
        return module

    def test_AWSではYAMLのenv解決前に秘密値を展開する(self):
        client = Mock()
        client.get_parameter.return_value = {
            'Parameter': {
                'Type': 'SecureString',
                'Value': '{"XSBOT_API_TOKEN":"parameter-token"}',
            },
        }
        settings_text = '''
"*":
  cloud:
    provider: gcp
  auth:
    api_token: !env XSBOT_API_TOKEN
  aws:
    region: !env AWS_REGION
  bots: {}
'''

        with patch(
            'cloud_backend.aws.runtime_secrets.boto3.client',
            return_value=client,
        ) as client_factory:
            module = self._load_settings_module(
                settings_text,
                {
                    'XSBOT_CLOUD_PROVIDER': 'aws',
                    'AWS_REGION': 'ap-northeast-1',
                    'XSBOT_AWS_RUNTIME_SECRETS_PARAMETER': PARAMETER_NAME,
                    'XSBOT_API_TOKEN': 'existing-token',
                },
                'settings_aws_runtime_secrets_under_test',
            )

        self.assertEqual('parameter-token', module.AUTH_SETTINGS['api_token'])
        client_factory.assert_called_once_with(
            'ssm', region_name='ap-northeast-1')
        client.get_parameter.assert_called_once_with(
            Name=PARAMETER_NAME, WithDecryption=True)

    def test_YAMLだけでAWS選択した場合も秘密値を展開する(self):
        client = Mock()
        client.get_parameter.return_value = {
            'Parameter': {
                'Type': 'SecureString',
                'Value': '{"XSBOT_API_TOKEN":"parameter-token"}',
            },
        }
        settings_text = '''
"*":
  cloud:
    provider: aws
  auth:
    api_token: !env XSBOT_API_TOKEN
  aws:
    region: !env AWS_REGION
  bots: {}
'''

        with patch(
            'cloud_backend.aws.runtime_secrets.boto3.client',
            return_value=client,
        ) as client_factory:
            module = self._load_settings_module(
                settings_text,
                {
                    'AWS_REGION': 'ap-northeast-1',
                    'XSBOT_AWS_RUNTIME_SECRETS_PARAMETER': PARAMETER_NAME,
                },
                'settings_yaml_aws_runtime_secrets_under_test',
            )

        self.assertEqual('aws', module.CLOUD_SETTINGS['provider'])
        self.assertEqual('parameter-token', module.AUTH_SETTINGS['api_token'])
        client_factory.assert_called_once_with(
            'ssm', region_name='ap-northeast-1')
        client.get_parameter.assert_called_once_with(
            Name=PARAMETER_NAME, WithDecryption=True)

    def test_GCPではAWS秘密値loaderを呼ばない(self):
        loader = Mock(side_effect=AssertionError('AWS loader must not run'))
        fake_runtime_secrets = types.ModuleType(
            'cloud_backend.aws.runtime_secrets')
        fake_runtime_secrets.load_runtime_secrets = loader
        settings_text = '''
"*":
  cloud:
    provider: gcp
  auth: {}
  gcp: {}
  bots: {}
'''

        module = self._load_settings_module(
            settings_text,
            {'XSBOT_CLOUD_PROVIDER': 'gcp'},
            'settings_gcp_without_aws_secrets_under_test',
            modules={
                'cloud_backend.aws.runtime_secrets': fake_runtime_secrets,
            },
        )

        self.assertEqual('gcp', module.CLOUD_SETTINGS['provider'])
        loader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
