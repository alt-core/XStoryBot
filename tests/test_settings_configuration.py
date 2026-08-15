import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from utility import deep_merge, load_settings_yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SettingsTemplateTest(unittest.TestCase):
    def setUp(self):
        self.environment = {
            'XSBOT_API_TOKEN': 'value',
            'GOOGLE_CLOUD_PROJECT': 'test-project',
            'GOOGLE_CLOUD_PROJECT_NUMBER': '1234567890',
            'GOOGLE_CLOUD_LOCATION': 'asia-northeast1',
            'GOOGLE_APPLICATION_CREDENTIALS': '/secrets/service-account.json',
            'XSBOT_STORAGE_BUCKET': 'test-storage-bucket',
            'XSBOT_APP_BASE_URL': 'https://app.example.invalid',
            'XSBOT_BUILDER_BASE_URL': 'https://builder.example.invalid',
            'AWS_REGION': 'test-region-1',
            'XSBOT_AWS_PRIVATE_BUCKET': 'test-private-bucket',
            'XSBOT_AWS_MEDIA_BUCKET': 'test-media-bucket',
            'XSBOT_AWS_PUBLIC_MEDIA_BASE_URL': (
                'https://distribution.example.invalid'),
            'XSBOT_AWS_STATE_TABLE': 'test-state-table',
            'XSBOT_AWS_GROUP_TASK_TABLE': 'test-group-task-table',
            'XSBOT_AWS_GROUP_TASK_INDEX': 'test-group-task-index',
            'XSBOT_AWS_CACHE_TABLE': 'test-cache-table',
            'XSBOT_AWS_ACTION_QUEUE_URL': (
                'https://sqs.test-region-1.amazonaws.com/'
                '000000000000/test-action-queue'),
            'XSBOT_AWS_ACTION_QUEUE_ARN': (
                'arn:aws:sqs:test-region-1:000000000000:test-action-queue'),
            'XSBOT_AWS_GROUP_MESSAGE_QUEUE_URL': (
                'https://sqs.test-region-1.amazonaws.com/'
                '000000000000/test-group-queue'),
            'XSBOT_AWS_GROUP_MESSAGE_QUEUE_ARN': (
                'arn:aws:sqs:test-region-1:000000000000:test-group-queue'),
            'XSBOT_AWS_SCHEDULER_ROLE_ARN': (
                'arn:aws:iam::000000000000:role/test-scheduler-role'),
            'XSBOT_AWS_SCHEDULER_GROUP_NAME': 'test-scheduler-group',
            'XSBOT_AWS_SCHEDULER_DLQ_ARN': (
                'arn:aws:sqs:test-region-1:000000000000:test-dlq'),
            'XSBOT_AWS_BUILD_CLUSTER': 'test-cluster',
            'XSBOT_AWS_BUILD_TASK_DEFINITION': 'test-builder:1',
            'XSBOT_AWS_BUILD_CONTAINER_NAME': 'xstorybot',
            'XSBOT_AWS_BUILD_SUBNET_ID_1': 'subnet-0123456789abcdef0',
            'XSBOT_AWS_BUILD_SUBNET_ID_2': 'subnet-0123456789abcdef1',
            'XSBOT_AWS_BUILD_SECURITY_GROUP_ID': (
                'sg-0123456789abcdef0'),
            'XSBOT_AWS_SHEETS_CREDENTIAL_PARAMETER': (
                '/xstorybot/test/google-sheets-service-account'),
            'XSBOT_AWS_ADMIN_AUTH_PARAMETER': (
                '/xstorybot/test/admin-auth'),
            'LINE_CHANNEL_SECRET': 'value',
            'LINE_ACCESS_TOKEN': 'value',
            'SHEETS_ID': 'test-sheet-id',
            'SHEETS_SERVICE_ACCOUNT': '/secrets/sheets-service-account.json',
            'OPENAI_API_KEY': 'value',
            'TWILIO_SID': 'value',
            'TWILIO_AUTH_TOKEN': 'value',
            'TWILIO_PHONE_NUMBER': '+815000000000',
            'PUSHER_APP_ID': 'value',
            'PUSHER_APP_KEY': 'value',
            'PUSHER_APP_SECRET': 'value',
            'PUSHER_APP_CLUSTER': 'ap3',
        }

    def load_template(self):
        with patch.dict(os.environ, self.environment, clear=True):
            return load_settings_yaml(PROJECT_ROOT / 'settings.yaml.template')

    def test_template_has_approved_defaults(self):
        loaded = self.load_template()
        default = loaded['*']

        self.assertEqual(3, default['options']['scenario_version'])
        self.assertEqual(2000, default['options']['group_batch_size'])
        self.assertEqual(150, default['options']['group_max_workers'])
        self.assertEqual(500, default['options']['group_max_rate'])
        self.assertTrue(default['plugins']['chatgpt']['log_conversation'])
        self.assertEqual('bot', default['bots']['bot']['state_namespace'])
        self.assertEqual('gcp', default['cloud']['provider'])

        quick_reply_ignore = re.compile(
            default['plugins']['line.quick_reply']['ignore_pattern'])
        self.assertIsNotNone(quick_reply_ignore.search('##line.liff.action'))
        self.assertIsNone(quick_reply_ignore.search('##lineXliffYaction'))

    def test_template_uses_explicit_service_settings(self):
        default = self.load_template()['*']

        self.assertEqual('test-storage-bucket', default['gcp']['storage_bucket'])
        self.assertEqual(
            'https://app.example.invalid',
            default['gcp']['services']['app']['base_url'],
        )
        self.assertEqual(
            'https://builder.example.invalid',
            default['gcp']['services']['builder']['base_url'],
        )
        self.assertEqual('test-region-1', default['aws']['region'])
        self.assertEqual(
            'test-private-bucket',
            default['aws']['object_store']['private_bucket'],
        )
        self.assertEqual(
            'https://distribution.example.invalid',
            default['aws']['object_store']['public_media_base_url'],
        )
        self.assertEqual(
            'test-state-table',
            default['aws']['state_store']['state_table'],
        )
        self.assertEqual(
            'test-group-task-index',
            default['aws']['state_store']['group_task_index'],
        )
        self.assertEqual(
            350 * 1024,
            default['aws']['state_store']['player_max_bytes'],
        )
        self.assertEqual(
            'https://sqs.test-region-1.amazonaws.com/'
            '000000000000/test-action-queue',
            default['aws']['task_queue']['queues']['action-queue']['url'],
        )
        self.assertEqual(
            'arn:aws:sqs:test-region-1:000000000000:test-group-queue',
            default['aws']['task_queue']['queues'][
                'group-message-queue']['arn'],
        )
        self.assertEqual(
            'test-scheduler-group',
            default['aws']['task_queue']['scheduler']['group_name'],
        )
        self.assertEqual(
            {
                'role_arn': (
                    'arn:aws:iam::000000000000:role/test-scheduler-role'),
                'group_name': 'test-scheduler-group',
                'dead_letter_arn': (
                    'arn:aws:sqs:test-region-1:000000000000:test-dlq'),
                'maximum_event_age_seconds': 3600,
                'maximum_retry_attempts': 3,
            },
            default['aws']['task_queue']['scheduler'],
        )
        self.assertEqual(
            {
                'cluster': 'test-cluster',
                'task_definition': 'test-builder:1',
                'container_name': 'xstorybot',
                'subnet_ids': [
                    'subnet-0123456789abcdef0',
                    'subnet-0123456789abcdef1',
                ],
                'security_group_ids': ['sg-0123456789abcdef0'],
            },
            default['aws']['task_queue']['build'],
        )
        self.assertEqual(
            '/xstorybot/test/google-sheets-service-account',
            default['aws']['credential_source'][
                'google_service_account_parameter'],
        )
        self.assertEqual(
            '/xstorybot/test/admin-auth',
            default['aws']['credential_source']['admin_auth_parameter'],
        )
        self.assertEqual(
            'XSBOT_ADMIN_AUTH_JSON',
            default['auth']['admin_auth_json_env'],
        )
        self.assertEqual(
            '/secrets/sheets-service-account.json',
            default['plugins']['google_sheets']['service_account'],
        )

    def test_template_uses_only_public_font_examples(self):
        default = self.load_template()['*']

        self.assertEqual(
            'plugin/render_text/font/ipaexg_tate.ttf',
            default['plugins']['line.image_text']['frames']['book']['font_path'],
        )
        self.assertEqual(
            'plugin/render_text/font/ipaexg.ttf',
            default['plugins']['render_text']['text2image']['font_path'],
        )

    def test_environment_settings_are_deep_merged(self):
        loaded = self.load_template()
        merged = deep_merge(loaded['*'], loaded['dev'])

        self.assertEqual('My Bot (開発環境)', merged['bots']['bot']['name'])
        self.assertEqual('bot', merged['bots']['bot']['state_namespace'])
        self.assertEqual('test-storage-bucket', merged['gcp']['storage_bucket'])


class SettingsModuleTest(unittest.TestCase):
    def test_module_loads_selected_environment(self):
        template_path = PROJECT_ROOT / 'settings.yaml.template'
        module_path = PROJECT_ROOT / 'settings.py'

        with tempfile.TemporaryDirectory() as temp_dir:
            shutil.copyfile(template_path, Path(temp_dir) / 'settings.yaml')
            previous_dir = os.getcwd()
            try:
                os.chdir(temp_dir)
                with patch.dict(
                    os.environ,
                    {
                        'XSBOT_DEPLOY_ENV': 'dev',
                        'XSBOT_STORAGE_BUCKET': 'test-storage-bucket',
                        'XSBOT_APP_BASE_URL': 'https://app.example.invalid',
                        'XSBOT_BUILDER_BASE_URL': 'https://builder.example.invalid',
                    },
                    clear=True,
                ):
                    spec = importlib.util.spec_from_file_location(
                        'settings_under_test', module_path
                    )
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = module
                    spec.loader.exec_module(module)
            finally:
                os.chdir(previous_dir)
                sys.modules.pop('settings_under_test', None)

        self.assertEqual('dev', module.DEPLOY_ENV)
        self.assertEqual('My Bot (開発環境)', module.BOTS['bot']['name'])
        self.assertEqual(3, module.OPTIONS['scenario_version'])
        self.assertEqual('test-storage-bucket', module.GCP_SETTINGS['storage_bucket'])
        self.assertEqual('gcp', module.CLOUD_SETTINGS['provider'])
        self.assertIs(module.GCP_SETTINGS, module.BACKEND_SETTINGS)
        self.assertEqual(
            'https://app.example.invalid',
            module.SERVICE_SETTINGS['app']['base_url'])

    def test_AWS選択時はGCP設定を必須にしない(self):
        module_path = PROJECT_ROOT / 'settings.py'
        cloud_backend = types.ModuleType('cloud_backend')
        cloud_backend.configure = Mock(return_value='aws')
        runtime_secrets = types.ModuleType(
            'cloud_backend.aws.runtime_secrets')
        runtime_secrets.load_runtime_secrets = Mock()
        config = {
            '*': {
                'cloud': {'provider': 'gcp'},
                'aws': {
                    'services': {
                        'app': {
                            'base_url': 'https://app.example.invalid',
                        },
                    },
                },
                'auth': {},
                'bots': {},
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, 'settings.yaml').write_text(
                json.dumps(config), encoding='utf-8')
            previous_dir = os.getcwd()
            try:
                os.chdir(temp_dir)
                module_name = 'settings_aws_under_test'
                spec = importlib.util.spec_from_file_location(
                    module_name, module_path)
                module = importlib.util.module_from_spec(spec)
                with (
                    patch.dict(os.environ, {'XSBOT_CLOUD_PROVIDER': 'aws'}),
                    patch.dict(
                        sys.modules,
                        {
                            module_name: module,
                            'cloud_backend': cloud_backend,
                            'cloud_backend.aws.runtime_secrets': (
                                runtime_secrets),
                        },
                    ),
                ):
                    spec.loader.exec_module(module)
            finally:
                os.chdir(previous_dir)
                sys.modules.pop('settings_aws_under_test', None)

        self.assertEqual('aws', module.CLOUD_SETTINGS['provider'])
        self.assertEqual({}, module.GCP_SETTINGS)
        self.assertIs(module.BACKEND_SETTINGS, module.settings['aws'])
        self.assertEqual(
            'https://app.example.invalid',
            module.SERVICE_SETTINGS['app']['base_url'])
        cloud_backend.configure.assert_called_once_with(
            {'provider': 'aws'})
        runtime_secrets.load_runtime_secrets.assert_called_once_with()


class IgnoreConfigurationTest(unittest.TestCase):
    def test_env_template_lists_deployment_environment(self):
        lines = (PROJECT_ROOT / '.env.template').read_text(
            encoding='utf-8').splitlines()
        self.assertIn('XSBOT_DEPLOY_ENV=dev', lines)

    def test_管理者認証の例示署名鍵はそのまま使えない(self):
        line = next(
            item for item in (PROJECT_ROOT / '.env.template').read_text(
                encoding='utf-8').splitlines()
            if item.startswith('XSBOT_ADMIN_AUTH_JSON='))

        self.assertIn('"session_secret":"replace-me"', line)

    def test_netrc_is_excluded_from_all_contexts(self):
        for filename in ('.gitignore', '.dockerignore', '.gcloudignore'):
            lines = (PROJECT_ROOT / filename).read_text(encoding='utf-8').splitlines()
            self.assertIn('.netrc', lines, filename)
            self.assertIn('_netrc', lines, filename)

    def test_local_settings_are_excluded_from_deployment_contexts(self):
        for filename in ('.dockerignore', '.gcloudignore'):
            lines = (PROJECT_ROOT / filename).read_text(encoding='utf-8').splitlines()
            self.assertIn('settings.yaml', lines, filename)

    def test_common_secret_file_names_are_excluded(self):
        patterns = {
            '*service-account*.json',
            '*service_account*.json',
            '*firebase-adminsdk*.json',
            'keyfile*.json',
            'credentials*.json',
            'client_secret*.json',
            '*.pem',
            '*.key',
            '*.p12',
        }
        for filename in ('.gitignore', '.dockerignore', '.gcloudignore'):
            lines = set(
                (PROJECT_ROOT / filename).read_text(
                    encoding='utf-8'
                ).splitlines()
            )
            self.assertTrue(patterns.issubset(lines), filename)

    def test_settings_template_remains_in_deployment_contexts(self):
        for filename in ('.dockerignore', '.gcloudignore'):
            lines = (PROJECT_ROOT / filename).read_text(encoding='utf-8').splitlines()
            self.assertNotIn('*.template', lines, filename)


class DependencyConfigurationTest(unittest.TestCase):
    def test_approved_dependency_versions_are_preserved(self):
        requirements = (
            PROJECT_ROOT / 'requirements.txt'
        ).read_text(encoding='utf-8').splitlines()

        self.assertIn('line-bot-sdk==2.4.3', requirements)
        self.assertIn('requests==2.31.0', requirements)
        self.assertIn('Pillow~=12.3.0', requirements)
        self.assertIn('gunicorn~=23.0.0', requirements)
        self.assertIn('boto3~=1.43.53', requirements)
        self.assertIn('argon2-cffi~=25.1.0', requirements)
        self.assertIn('itsdangerous~=2.2.0', requirements)
        self.assertFalse(any(
            line.startswith('firebase-admin') for line in requirements))
        self.assertFalse(
            any(line.startswith('google-cloud-memcache') for line in requirements)
        )


if __name__ == '__main__':
    unittest.main()
