import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

from bottle import Bottle


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_main(deploy_env):
    auth = types.ModuleType('auth')
    auth.setup = Mock()

    common_commands = types.ModuleType('common_commands')
    common_commands.setup = Mock()

    plugin = types.ModuleType('plugin')
    plugin.load_plugins = Mock()
    plugin.get_plugins = Mock(return_value={})

    settings = types.ModuleType('settings')
    settings.AUTH_SETTINGS = {}
    settings.GCP_SETTINGS = {}
    settings.BACKEND_SETTINGS = settings.GCP_SETTINGS
    settings.OPTIONS = {}
    settings.PLUGINS = {}
    settings.BOTS = {}

    hub = types.ModuleType('hub')
    hub.clear = Mock()

    commands = types.ModuleType('commands')
    commands.clear = Mock()

    task_client = types.ModuleType('task_client')
    task_client.initialize = Mock()

    group_message_task_db = types.ModuleType('group_message_task_db')
    group_message_task_db.GroupMessageTaskDB = type(
        'GroupMessageTaskDB',
        (),
        {'initialize': Mock()},
    )

    runtime = types.ModuleType('runtime')
    runtime.BotRuntime = object

    scenario = types.ModuleType('scenario')
    scenario.ScenarioBuilder = Mock()

    google = types.ModuleType('google')
    google.__path__ = []
    cloud = types.ModuleType('google.cloud')
    cloud.__path__ = []
    cloud_logging = types.ModuleType('google.cloud.logging')
    logging_client = Mock()
    cloud_logging.Client = Mock(return_value=logging_client)
    google.cloud = cloud
    cloud.logging = cloud_logging

    replacements = {
        'auth': auth,
        'common_commands': common_commands,
        'plugin': plugin,
        'settings': settings,
        'hub': hub,
        'commands': commands,
        'task_client': task_client,
        'group_message_task_db': group_message_task_db,
        'runtime': runtime,
        'scenario': scenario,
        'google': google,
        'google.cloud': cloud,
        'google.cloud.logging': cloud_logging,
    }

    module_name = f'main_for_initialization_test_{deploy_env}'
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / 'main.py')
    module = importlib.util.module_from_spec(spec)
    original_excepthook = sys.excepthook
    try:
        with (
            patch.dict(sys.modules, replacements),
            patch.dict(os.environ, {'XSBOT_DEPLOY_ENV': deploy_env}, clear=False),
        ):
            spec.loader.exec_module(module)
    finally:
        sys.excepthook = original_excepthook
    return cloud_logging, logging_client


class CloudLoggingInitializationTest(unittest.TestCase):
    def test_testとlocalではCloud_Loggingを初期化しない(self):
        for deploy_env in ('test', 'local'):
            with self.subTest(deploy_env=deploy_env):
                cloud_logging, logging_client = load_main(deploy_env)

                cloud_logging.Client.assert_not_called()
                logging_client.setup_logging.assert_not_called()

    def test_その他の環境ではCloud_Loggingを初期化する(self):
        cloud_logging, logging_client = load_main('prod')

        cloud_logging.Client.assert_called_once_with()
        logging_client.setup_logging.assert_called_once_with()


class OptionalPluginWebApiTest(unittest.TestCase):
    def load_app(self, import_side_effect, plugins=None):
        main = types.ModuleType('main')
        main.get_plugins = Mock(return_value=(
            plugins or {'optional_plugin': object()}
        ))

        settings = types.ModuleType('settings')
        settings.DEPLOY_ENV = 'test'

        root_webapi = types.ModuleType('webapi')
        root_webapi.app = Bottle()

        dashboard = types.ModuleType('dashboard')
        dashboard.app = Bottle()

        module_name = 'app_for_optional_plugin_test'
        spec = importlib.util.spec_from_file_location(
            module_name, PROJECT_ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        with (
            patch.dict(sys.modules, {
                'main': main,
                'settings': settings,
                'webapi': root_webapi,
                'dashboard': dashboard,
            }),
            patch('importlib.import_module', side_effect=import_side_effect),
        ):
            spec.loader.exec_module(module)
        return module

    def test_任意pluginのwebapi欠落は読み飛ばす(self):
        present_webapi = types.ModuleType('plugin.present_plugin.webapi')
        present_webapi.app = Bottle()

        @present_webapi.app.get('/present-plugin')
        def present_plugin_handler():
            return 'OK'

        def import_plugin(name):
            if name == 'plugin.optional_plugin.webapi':
                raise ModuleNotFoundError('optional dependency')
            return present_webapi

        importer = Mock(side_effect=import_plugin)

        module = self.load_app(importer, {
            'optional_plugin': object(),
            'present_plugin': object(),
        })

        self.assertIsInstance(module.app, Bottle)
        self.assertEqual(
            [call.args[0] for call in importer.call_args_list],
            [
                'plugin.optional_plugin.webapi',
                'plugin.present_plugin.webapi',
            ],
        )
        self.assertIn('/present-plugin', [route.rule for route in module.app.routes])

    def test_ModuleNotFoundError以外は伝播する(self):
        with self.assertRaisesRegex(RuntimeError, 'plugin import failed'):
            self.load_app(RuntimeError('plugin import failed'))


if __name__ == '__main__':
    unittest.main()
