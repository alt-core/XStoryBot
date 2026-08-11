import hashlib
import importlib.util
import pickle
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch


_MISSING = object()


class ScenarioStorageTest(unittest.TestCase):
    module_name = 'tests._scenario_storage_under_test'

    def setUp(self):
        self.storage_client = Mock()

        google = types.ModuleType('google')
        google.__path__ = []
        cloud = types.ModuleType('google.cloud')
        cloud.__path__ = []
        storage = types.ModuleType('google.cloud.storage')
        storage.Client = Mock(return_value=self.storage_client)
        oauth2 = types.ModuleType('google.oauth2')
        oauth2.__path__ = []
        service_account = types.ModuleType('google.oauth2.service_account')
        service_account.Credentials = SimpleNamespace(
            from_service_account_file=Mock(return_value=object())
        )
        google.cloud = cloud
        google.oauth2 = oauth2
        cloud.storage = storage
        oauth2.service_account = service_account

        settings = types.ModuleType('settings')
        settings.GCP_SETTINGS = {
            'credentials_path': '/unused/test-credentials.json',
            'project_id': 'test-project',
            'storage_bucket': 'trusted-bucket',
        }

        models = types.ModuleType('models')
        models.ImageFileStatDB = type('ImageFileStatDB', (), {})
        models.MediaFileStatDB = type('MediaFileStatDB', (), {})

        common_commands = types.ModuleType('common_commands')
        for name in (
            'OR_CMDS', 'IF_CMDS', 'ELSE_CMDS', 'END_CMDS', 'SEQ_CMDS',
            'LOOP_CMDS', 'RANDOM_CMDS', 'IMAGE_CMDS', 'CALL_CMDS',
            'RETURN_CMDS', 'DEFER_CMDS',
        ):
            setattr(common_commands, name, ())

        self.hub = types.ModuleType('hub')
        self.hub.filter_all_runtime_methods = Mock(return_value='filtered-action')
        commands = types.ModuleType('commands')
        convert_image = types.ModuleType('convert_image')
        utility = types.ModuleType('utility')
        requests = types.ModuleType('requests')

        condition_expr = types.ModuleType('condition_expr')
        condition_expr.ConditionExpression = type('ConditionExpression', (), {})
        expression = types.ModuleType('expression')
        expression.Expression = type('Expression', (), {})
        expression.set_version = Mock()

        fake_modules = {
            'google': google,
            'google.cloud': cloud,
            'google.cloud.storage': storage,
            'google.oauth2': oauth2,
            'google.oauth2.service_account': service_account,
            'settings': settings,
            'models': models,
            'common_commands': common_commands,
            'hub': self.hub,
            'commands': commands,
            'convert_image': convert_image,
            'utility': utility,
            'requests': requests,
            'condition_expr': condition_expr,
            'expression': expression,
        }
        self.saved_modules = {
            name: sys.modules.get(name, _MISSING)
            for name in (*fake_modules, self.module_name)
        }
        sys.modules.update(fake_modules)

        scenario_path = Path(__file__).resolve().parents[1] / 'scenario.py'
        spec = importlib.util.spec_from_file_location(self.module_name, scenario_path)
        self.scenario_module = importlib.util.module_from_spec(spec)
        sys.modules[self.module_name] = self.scenario_module
        spec.loader.exec_module(self.scenario_module)

    def tearDown(self):
        for name, original in self.saved_modules.items():
            if original is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    def test_異なるbucketはdownload前に拒否する(self):
        with self.assertRaises(self.scenario_module.ScenarioSyntaxError):
            self.scenario_module.Scenario.load_from_uri(
                'https://storage.googleapis.com/foreign-bucket/scenario/object'
            )

        self.storage_client.bucket.assert_not_called()

    def test_同じbucketのpickleを読み込む(self):
        saved = self.scenario_module.Scenario(version=3)
        saved.constants = {'key': 'value'}
        blob = Mock()
        blob.download_as_bytes.return_value = pickle.dumps(saved)
        bucket = Mock()
        bucket.blob.return_value = blob
        self.storage_client.bucket.return_value = bucket

        loaded = self.scenario_module.Scenario.load_from_uri(
            'https://storage.googleapis.com/trusted-bucket/scenario/object'
        )

        self.assertIsInstance(loaded, self.scenario_module.Scenario)
        self.assertEqual(loaded.version, 3)
        self.assertEqual(loaded.constants, {'key': 'value'})
        self.storage_client.bucket.assert_called_once_with('trusted-bucket')
        bucket.blob.assert_called_once_with('scenario/object')
        blob.download_as_bytes.assert_called_once_with()

    def test_保存はMD5名とmake_publicを維持する(self):
        blob = Mock()
        bucket = Mock()
        bucket.name = 'trusted-bucket'
        bucket.blob.return_value = blob
        self.storage_client.bucket.return_value = bucket

        uri = self.scenario_module.Scenario(version=3).save_to_storage()

        scenario_data = blob.upload_from_string.call_args.args[0]
        digest = hashlib.md5(scenario_data).hexdigest()
        self.storage_client.bucket.assert_called_once_with('trusted-bucket')
        bucket.blob.assert_called_once_with(f'scenario/{digest}')
        blob.upload_from_string.assert_called_once_with(
            scenario_data,
            content_type='application/octet-stream',
        )
        blob.make_public.assert_called_once_with()
        self.assertEqual(
            uri,
            f'https://storage.googleapis.com/trusted-bucket/scenario/{digest}',
        )

    def _assert_fallback_env(self, version, flag_label_error, env_name, fallback_action):
        status = SimpleNamespace(scene='main/', action_token='action-token')
        context = SimpleNamespace(
            status=status,
            action='raw-action',
            current_action=None,
            attrs={},
            env=Mock(),
            add_env=Mock(),
            reactions=[],
        )
        scenario = SimpleNamespace(
            version=version,
            startup_scene_title='startup/',
            scenes={},
        )
        director = self.scenario_module.Director(scenario, context)
        base_scene = object()
        fallback_scene = object()
        fallback_region = object()
        director._get_scene = Mock(return_value=object())
        director._get_scene_or_default = Mock(return_value=base_scene)
        director.search_block = Mock(side_effect=[
            (None, None, None, None),
            (fallback_scene, fallback_region, 0, ('match',)),
        ])
        director._plan_reaction_sub = Mock(return_value=None)
        director.flag_label_error = flag_label_error
        self.hub.filter_all_runtime_methods.reset_mock()
        self.hub.filter_all_runtime_methods.return_value = 'filtered-action'

        with patch.object(self.scenario_module.logging, 'warning'):
            director.plan_reactions()

        context.add_env.assert_called_once_with({env_name: 'filtered-action'})
        self.assertEqual(
            director.search_block.call_args_list,
            [
                call(base_scene, 'filtered-action'),
                call(base_scene, fallback_action),
            ],
        )

    def test_invalid_labelは全versionでfilter後actionを設定する(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                self._assert_fallback_env(
                    version,
                    True,
                    '$$invalid_label',
                    '##error_invalid_label',
                )

    def test_unhandled_actionは全versionでfilter後actionを設定する(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                self._assert_fallback_env(
                    version,
                    False,
                    '$$unhandled_action',
                    '##error_unhandled_action',
                )


if __name__ == '__main__':
    unittest.main()
