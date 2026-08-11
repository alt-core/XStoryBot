import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_runtime_module():
    build_cache = types.ModuleType('build_cache')
    settings = types.ModuleType('settings')
    settings.CONSTANTS = {}

    models = types.ModuleType('models')
    models.GlobalBotVariablesDB = Mock()

    scenario = types.ModuleType('scenario')
    scenario.Scenario = object
    scenario.ScenarioBuilder = Mock()
    scenario.ScenarioSyntaxError = type('ScenarioSyntaxError', (Exception,), {})

    class Director:
        def __init__(self, _scenario, context):
            self.context = context

        def plan_reactions(self):
            self.context.reactions = []

    scenario.Director = Director

    commands = types.ModuleType('commands')
    commands.get_runtime_object_dictionary = lambda *_args: {}

    module_name = 'runtime_for_state_namespace_test'
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / 'runtime.py'
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        'build_cache': build_cache,
        'settings': settings,
        'models': models,
        'scenario': scenario,
        'commands': commands,
    }):
        spec.loader.exec_module(module)
    return module


def load_context_module():
    models = types.ModuleType('models')
    models.PlayerStatusDB = Mock(return_value=object())

    utility = types.ModuleType('utility')
    utility.safe_list_get = lambda values, index, default=None: (
        values[index] if 0 <= index < len(values) else default
    )

    module_name = 'context_for_state_namespace_test'
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / 'context.py'
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        'models': models,
        'utility': utility,
    }):
        spec.loader.exec_module(module)
    return module, models


def load_main_module():
    auth = types.ModuleType('auth')
    auth.setup = Mock()
    common_commands = types.ModuleType('common_commands')
    common_commands.setup = Mock()
    plugin = types.ModuleType('plugin')
    plugin.load_plugins = Mock()
    plugin.get_plugins = Mock(return_value={})
    settings = types.ModuleType('settings')
    settings.AUTH_SETTINGS = {'api_token': 'value'}
    settings.GCP_SETTINGS = {'project_id': 'test-project'}
    settings.BACKEND_SETTINGS = settings.GCP_SETTINGS
    settings.OPTIONS = {}
    settings.PLUGINS = {}
    settings.BOTS = {
        'bot_a': {
            'state_namespace': 'shared',
            'interfaces': [{'type': 'mock_line', 'params': {}}],
            'scenario': {'type': 'mock_loader', 'params': {}},
        },
        'bot_b': {
            'interfaces': [{'type': 'mock_line', 'params': {}}],
            'scenario': {'type': 'mock_loader', 'params': {}},
        },
    }

    interface = Mock()
    interface.get_service_list.return_value = {'mock_line': interface}
    hub = types.ModuleType('hub')
    hub.clear = Mock()
    hub.create_interface = Mock(return_value=interface)
    hub.create_scenario_loader = Mock(return_value=Mock())

    commands = types.ModuleType('commands')
    commands.clear = Mock()
    task_client = types.ModuleType('task_client')
    task_client.initialize = Mock()

    group_db = types.ModuleType('group_message_task_db')
    group_db.GroupMessageTaskDB = Mock()

    runtime = types.ModuleType('runtime')
    runtime.created = []

    class FakeRuntime:
        def __init__(self, name, interfaces, scenario_loader,
                     state_namespace=None):
            runtime.created.append((name, state_namespace))
            self.scenario = object()

    runtime.BotRuntime = FakeRuntime

    scenario = types.ModuleType('scenario')
    scenario.ScenarioBuilder = Mock()

    replacements = {
        'auth': auth,
        'common_commands': common_commands,
        'plugin': plugin,
        'settings': settings,
        'hub': hub,
        'commands': commands,
        'task_client': task_client,
        'group_message_task_db': group_db,
        'runtime': runtime,
        'scenario': scenario,
    }
    module_name = 'main_for_state_namespace_test'
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / 'main.py'
    )
    module = importlib.util.module_from_spec(spec)
    with (
        patch.dict(sys.modules, replacements),
        patch.dict('os.environ', {'XSBOT_DEPLOY_ENV': 'test'}, clear=False),
    ):
        spec.loader.exec_module(module)
    return module, runtime


class FakeContext:
    def __init__(self, bot_name):
        self.bot_name = bot_name
        self.state_namespace = bot_name
        self.service_name = 'mock_line'
        self.action = '開始'
        self.user = Mock()
        self.user.serialize.return_value = 'mock_line:user-1'
        self.loaded_namespace = None
        self.env = []

    def add_env(self, value):
        self.env.append(value)

    def load_status(self):
        self.loaded_namespace = self.state_namespace

    def save_status(self):
        return None


class StateNamespaceTest(unittest.TestCase):
    def setUp(self):
        self.module = load_runtime_module()
        self.interface = Mock()
        self.interface.get_retry_count.return_value = 0
        self.interface.respond_reaction.return_value = 'OK'

    def create_runtime(self, name, state_namespace=None, version=3):
        runtime = self.module.BotRuntime(
            name,
            {'mock_line': self.interface},
            scenario_loader=Mock(),
            state_namespace=state_namespace,
        )
        runtime.scenario = types.SimpleNamespace(version=version, constants={})
        return runtime

    def test_omitted_namespace_uses_bot_name(self):
        context = FakeContext('bot-a')

        self.create_runtime('bot-a').handle_action(context)

        self.assertEqual(context.loaded_namespace, 'bot-a')

    def test_same_namespace_is_shared_between_bots(self):
        first = FakeContext('bot-a')
        second = FakeContext('bot-b')

        self.create_runtime('bot-a', 'shared').handle_action(first)
        self.create_runtime('bot-b', 'shared').handle_action(second)

        self.assertEqual(first.loaded_namespace, 'shared')
        self.assertEqual(second.loaded_namespace, 'shared')

    def test_default_namespaces_remain_separate(self):
        first = FakeContext('bot-a')
        second = FakeContext('bot-b')

        self.create_runtime('bot-a').handle_action(first)
        self.create_runtime('bot-b').handle_action(second)

        self.assertNotEqual(first.loaded_namespace, second.loaded_namespace)

    def test_main_passes_configured_and_default_namespaces(self):
        _module, runtime = load_main_module()

        self.assertEqual(
            runtime.created,
            [('bot_a', 'shared'), ('bot_b', 'bot_b')],
        )

    def test_ActionContextはnamespaceをPlayerStatusへ渡す(self):
        module, models = load_context_module()
        user = Mock()
        user.serialize.return_value = 'line:user-1'
        context = module.ActionContext(
            'bot-a', 'line', None, user, '開始', {})
        context.state_namespace = 'shared'

        context.load_status()

        models.PlayerStatusDB.assert_called_once_with('shared', 'line:user-1')

    def test_action環境変数はv3だけでfilter前actionを保持する(self):
        v2_context = FakeContext('bot-v2')
        v3_context = FakeContext('bot-v3')

        self.create_runtime('bot-v2', version=2).handle_action(v2_context)
        self.create_runtime('bot-v3', version=3).handle_action(v3_context)

        v2_env = {
            key: value
            for env in v2_context.env
            for key, value in env.items()
        }
        v3_env = {
            key: value
            for env in v3_context.env
            for key, value in env.items()
        }
        self.assertNotIn('$$action', v2_env)
        self.assertEqual(v3_env['$$action'], '開始')


if __name__ == '__main__':
    unittest.main()
