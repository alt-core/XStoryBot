import importlib.util
import io
import logging
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

from bottle import Bottle


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_main(deploy_env, provider='gcp'):
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
    settings.CLOUD_SETTINGS = {'provider': provider}
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

    module_name = f'main_for_initialization_test_{deploy_env}_{provider}'
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / 'main.py')
    module = importlib.util.module_from_spec(spec)
    original_excepthook = sys.excepthook
    original_level = logging.getLogger().level
    try:
        with (
            patch.dict(sys.modules, replacements),
            patch.dict(os.environ, {'XSBOT_DEPLOY_ENV': deploy_env}, clear=False),
            patch('logging.basicConfig') as basic_config,
        ):
            spec.loader.exec_module(module)
    finally:
        sys.excepthook = original_excepthook
        root_level = logging.getLogger().level
        logging.getLogger().setLevel(original_level)
    cloud_logging.basic_config = basic_config
    cloud_logging.root_level = root_level
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

    def test_AWS選択時はCloud_Loggingを初期化しない(self):
        cloud_logging, logging_client = load_main('prod', provider='aws')

        cloud_logging.Client.assert_not_called()
        logging_client.setup_logging.assert_not_called()
        # AWS では標準エラーへ INFO 以上を出す（CloudWatch Logs が拾う）
        cloud_logging.basic_config.assert_called_once()
        self.assertEqual(logging.INFO, cloud_logging.root_level)

    def test_AWSはtest環境でも標準ログをINFOにする(self):
        # deploy 環境名 test は AWS の test stack でも使われる実環境名
        for deploy_env in ('test', 'local'):
            with self.subTest(deploy_env=deploy_env):
                cloud_logging, _client = load_main(deploy_env, provider='aws')
                cloud_logging.basic_config.assert_called_once()
                self.assertEqual(logging.INFO, cloud_logging.root_level)


class LogConfigTest(unittest.TestCase):
    """log_config.configure が INFO record を実際に handler へ届けることを確認する。"""

    def setUp(self):
        import log_config
        self.log_config = log_config
        self.root = logging.getLogger()
        self.original_level = self.root.level
        self.original_handlers = list(self.root.handlers)

    def tearDown(self):
        self.root.handlers[:] = self.original_handlers
        self.root.setLevel(self.original_level)

    def test_handlerが無ければ標準エラーへINFOを出す(self):
        self.root.handlers[:] = []
        self.root.setLevel(logging.WARNING)
        stderr = io.StringIO()
        with patch('sys.stderr', stderr):
            self.log_config.configure('aws', 'test')
            logging.getLogger('probe').info('届くはず')
        self.assertIn('INFO probe: 届くはず', stderr.getvalue())

    def test_既存handlerは壊さずlevelだけINFOにする(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        self.root.handlers[:] = [handler]
        self.root.setLevel(logging.WARNING)

        self.log_config.configure('aws', 'prod')
        logging.getLogger('probe').info('届くはず')

        self.assertEqual([handler], self.root.handlers)
        self.assertEqual(['届くはず'], [record.getMessage() for record in records])


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

    def test_Webchatのrouteは通常APIに混在させない(self):
        line_webapi = types.SimpleNamespace(app=Bottle())

        @line_webapi.app.post('/line/callback/<bot_name>')
        def line_callback(bot_name):
            return 'OK'

        importer = Mock(return_value=line_webapi)
        module = self.load_app(importer, {'webchat': object(), 'line': object()})

        importer.assert_called_once_with('plugin.line.webapi')
        self.assertIn('/line/callback/<bot_name>', [route.rule for route in module.app.routes])
        self.assertFalse(any('/api/webchat/' in route.rule for route in module.app.routes))


if __name__ == '__main__':
    unittest.main()
