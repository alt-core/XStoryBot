import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from utility import deep_merge, load_settings_yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SettingsTemplateTest(unittest.TestCase):
    def setUp(self):
        self.environment = {
            'ADMIN_EMAIL': 'admin@example.invalid',
            'XSBOT_API_TOKEN': 'value',
            'GOOGLE_CLOUD_PROJECT': 'test-project',
            'GOOGLE_CLOUD_PROJECT_NUMBER': '1234567890',
            'GOOGLE_CLOUD_LOCATION': 'asia-northeast1',
            'GOOGLE_APPLICATION_CREDENTIALS': '/secrets/service-account.json',
            'GOOGLE_FIREBASE_APP_ID': 'value',
            'GOOGLE_FIREBASE_API_KEY': 'value',
            'XSBOT_STORAGE_BUCKET': 'test-storage-bucket',
            'XSBOT_APP_BASE_URL': 'https://app.example.invalid',
            'XSBOT_BUILDER_BASE_URL': 'https://builder.example.invalid',
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
        self.assertEqual(
            '/secrets/service-account.json',
            default['auth']['firebase_credentials_path'],
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


class IgnoreConfigurationTest(unittest.TestCase):
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
        self.assertFalse(
            any(line.startswith('google-cloud-memcache') for line in requirements)
        )


if __name__ == '__main__':
    unittest.main()
