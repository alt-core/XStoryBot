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
        self.object_store = Mock()
        self.object_store.store_public.side_effect = (
            lambda key, _data, content_type=None:
            f'https://storage.googleapis.com/trusted-bucket/{key}'
        )
        self.object_store.public_url.side_effect = (
            lambda key: f'https://storage.googleapis.com/trusted-bucket/{key}'
        )

        cloud_backend = types.ModuleType('cloud_backend')
        cloud_backend.__path__ = []
        cloud_backend.create_object_store = Mock(return_value=self.object_store)
        contracts = types.ModuleType('cloud_backend.contracts')

        class InvalidObjectReferenceError(Exception):
            pass

        contracts.InvalidObjectReferenceError = InvalidObjectReferenceError
        self.InvalidObjectReferenceError = InvalidObjectReferenceError

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
        self.convert_image = types.ModuleType('convert_image')
        self.convert_image.get_ext_from_format = Mock(
            side_effect=lambda image_format: {
                'PNG': 'png',
                'JPEG': 'jpg',
            }[image_format]
        )
        self.convert_image.get_content_type_from_format = Mock(
            side_effect=lambda image_format: {
                'PNG': 'image/png',
                'JPEG': 'image/jpeg',
            }[image_format]
        )
        self.convert_image.resize_image = Mock(
            side_effect=lambda _data, resize_to, **_kwargs: (
                f'resized-{resize_to}'.encode('ascii'),
                'PNG',
                (resize_to, resize_to // 2),
            )
        )
        utility = types.ModuleType('utility')
        requests = types.ModuleType('requests')

        condition_expr = types.ModuleType('condition_expr')
        condition_expr.ConditionExpression = type('ConditionExpression', (), {})
        expression = types.ModuleType('expression')
        expression.Expression = type('Expression', (), {})
        expression.set_version = Mock()

        fake_modules = {
            'cloud_backend': cloud_backend,
            'cloud_backend.contracts': contracts,
            'models': models,
            'common_commands': common_commands,
            'hub': self.hub,
            'commands': commands,
            'convert_image': self.convert_image,
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
        self.object_store.load_scenario.side_effect = (
            self.InvalidObjectReferenceError(
                '設定済みのCloud Storage bucketではありません')
        )

        with self.assertRaises(self.scenario_module.ScenarioSyntaxError):
            self.scenario_module.Scenario.load_from_uri(
                'https://storage.googleapis.com/foreign-bucket/scenario/object'
            )

        self.object_store.load_scenario.assert_called_once_with(
            'https://storage.googleapis.com/foreign-bucket/scenario/object')

    def test_同じbucketのpickleを読み込む(self):
        saved = self.scenario_module.Scenario(version=3)
        saved.constants = {'key': 'value'}
        self.object_store.load_scenario.return_value = pickle.dumps(saved)

        loaded = self.scenario_module.Scenario.load_from_uri(
            'https://storage.googleapis.com/trusted-bucket/scenario/object'
        )

        self.assertIsInstance(loaded, self.scenario_module.Scenario)
        self.assertEqual(loaded.version, 3)
        self.assertEqual(loaded.constants, {'key': 'value'})
        self.object_store.load_scenario.assert_called_once_with(
            'https://storage.googleapis.com/trusted-bucket/scenario/object')

    def test_保存はMD5名とGCS参照を維持する(self):
        expected_uri = (
            'https://storage.googleapis.com/trusted-bucket/scenario/result')
        self.object_store.store_scenario.return_value = expected_uri

        uri = self.scenario_module.Scenario(version=3).save_to_storage()

        key, scenario_data = self.object_store.store_scenario.call_args.args
        digest = hashlib.md5(scenario_data).hexdigest()
        self.assertEqual(key, f'scenario/{digest}')
        self.assertEqual(uri, expected_uri)
        self.assertNotIn('object_store', pickle.loads(scenario_data).__dict__)

    def test_公開mediaはprovider非依存keyと従来URLを使う(self):
        builder = self.scenario_module.ScenarioBuilder({}, version=3)

        self.assertEqual(
            builder._make_image_filepath('PNG_digest', 1024),
            'image/digest_1024.png',
        )
        self.assertEqual(
            builder._make_imagemap_filepath('PNG_digest'),
            'imagemap/digest.png',
        )
        self.assertEqual(
            builder._make_video_filepath('digest'),
            'video/digest.mp4',
        )
        self.assertEqual(
            builder._make_url_from_filepath('image/digest_240.png'),
            'https://storage.googleapis.com/trusted-bucket/image/digest_240.png',
        )

    def test_通常画像は240と1024を公開保存する(self):
        builder = self.scenario_module.ScenarioBuilder({}, version=3)

        url, size = builder.build_image_for_image_command_with_rawdata(
            b'original', 'PNG_digest')

        self.assertEqual(
            url,
            'https://storage.googleapis.com/trusted-bucket/image/digest_1024.png',
        )
        self.assertEqual(size, (1024, 512))
        self.assertEqual(
            self.object_store.store_public.call_args_list,
            [
                call(
                    'image/digest_240.png', b'resized-240',
                    content_type='image/png'),
                call(
                    'image/digest_1024.png', b'resized-1024',
                    content_type='image/png'),
            ],
        )

    def test_imagemapは5sizeを保存しbase_URLを返す(self):
        builder = self.scenario_module.ScenarioBuilder({}, version=3)

        url, size = builder.build_image_for_imagemap_command_with_rawdata(
            b'original', 'PNG_digest')

        self.assertEqual(
            url,
            'https://storage.googleapis.com/trusted-bucket/imagemap/digest.png',
        )
        self.assertEqual(size, (1040, 520))
        self.assertEqual(
            self.object_store.store_public.call_args_list,
            [
                call(
                    'imagemap/digest.png/240', b'resized-240',
                    content_type='image/png'),
                call(
                    'imagemap/digest.png/300', b'resized-300',
                    content_type='image/png'),
                call(
                    'imagemap/digest.png/460', b'resized-460',
                    content_type='image/png'),
                call(
                    'imagemap/digest.png/700', b'resized-700',
                    content_type='image/png'),
                call(
                    'imagemap/digest.png/1040', b'resized-1040',
                    content_type='image/png'),
            ],
        )
        self.object_store.public_url.assert_called_once_with(
            'imagemap/digest.png')

    def test_動画はMP4として公開保存する(self):
        builder = self.scenario_module.ScenarioBuilder({}, version=3)

        url = builder.build_video_with_rawdata(b'video', 'digest')

        self.assertEqual(
            url,
            'https://storage.googleapis.com/trusted-bucket/video/digest.mp4',
        )
        self.object_store.store_public.assert_called_once_with(
            'video/digest.mp4', b'video', content_type='video/mp4')

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
