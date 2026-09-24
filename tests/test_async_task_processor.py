import json
import sys
import types
import unittest
from unittest.mock import Mock, call, patch

import async_task_processor


class FakeUser:
    def __init__(self, service_name, user_id):
        self.service_name = service_name
        self.user_id = user_id

    @classmethod
    def deserialize(cls, value):
        if not isinstance(value, str) or ':' not in value:
            return None
        service_name, user_id = value.split(':', 1)
        if not service_name or not user_id:
            return None
        return cls(service_name, user_id)

    def __str__(self):
        return f'{self.service_name}:{self.user_id}'


class FakeInterface:
    def create_context(self, user, action, attrs):
        return (user, action, attrs)


class FakeBot:
    def __init__(self):
        self.check_reload = Mock()
        self.handle_action = Mock(
            side_effect=lambda context: f'{context[0].user_id}\n')

    def get_interface(self, service_name):
        if service_name == 'plaintext':
            return FakeInterface()
        return None


class AsyncActionProcessorTest(unittest.TestCase):
    def setUp(self):
        self.bot = FakeBot()
        self.get_group_members = Mock(return_value=[])

    def test_actionをdecodeして従来の文字列結果を返す(self):
        result = async_task_processor.process_action(
            self.bot,
            'plaintext:user-1',
            'hello@@action-token',
            FakeUser,
            self.get_group_members,
            {},
        )

        self.assertEqual(result, 'user-1\n')
        self.bot.check_reload.assert_called_once_with()
        context = self.bot.handle_action.call_args.args[0]
        self.assertEqual(context[0].user_id, 'user-1')
        self.assertEqual(context[1], 'hello')
        self.assertEqual(context[2], {'action_token': 'action-token'})

    def test_group展開と間隔はHTTPとSQSで共有する(self):
        members = [
            FakeUser('plaintext', 'first'),
            FakeUser('unknown', 'skip'),
            FakeUser('plaintext', 'second'),
        ]
        self.get_group_members.return_value = members
        sleep = Mock()

        result = async_task_processor.process_action(
            self.bot,
            'group:group-1',
            'hello',
            FakeUser,
            self.get_group_members,
            {'group_interval': 250},
            sleep,
        )

        self.assertEqual(result, 'first\nsecond\n')
        self.assertEqual(sleep.call_args_list, [call(0.25)] * 3)

    def test_groupのinterface指定は各メンバーへ適用し識別子を維持する(self):
        members = [FakeUser('line', 'first'), FakeUser('line', 'second')]
        self.get_group_members.return_value = members
        result = async_task_processor.process_action(
            self.bot, 'group:group-1', 'hello', FakeUser,
            self.get_group_members, {'group_interval': 0}, interface_name='plaintext')
        self.assertEqual('first\nsecond\n', result)
        self.assertEqual(members, [item.args[0][0] for item in self.bot.handle_action.call_args_list])

    def test_不正userと未対応interfaceを状態付きerrorにする(self):
        with self.assertRaises(
                async_task_processor.TaskProcessingError) as invalid:
            async_task_processor.process_action(
                self.bot, 'invalid', 'hello', FakeUser,
                self.get_group_members, {},
            )
        self.assertEqual(invalid.exception.status_code, 400)
        self.bot.check_reload.assert_not_called()

        with self.assertRaises(
                async_task_processor.TaskProcessingError) as missing:
            async_task_processor.process_action(
                self.bot, 'unknown:user-1', 'hello', FakeUser,
                self.get_group_members, {},
            )
        self.assertEqual(missing.exception.status_code, 404)

    def test_group警告へuserやactionを出さない(self):
        secret_user = 'secret-user-value'
        secret_action = 'secret-action-value'
        self.get_group_members.return_value = [
            FakeUser('unknown', secret_user),
        ]

        with patch.object(async_task_processor.logging, 'warning') as warning:
            async_task_processor.process_action(
                self.bot,
                'group:group-1',
                secret_action,
                FakeUser,
                self.get_group_members,
                {'group_interval': 0},
                log_values=False,
            )

        messages = ' '.join(
            str(value)
            for log_call in warning.call_args_list
            for value in log_call.args
        )
        self.assertNotIn(secret_user, messages)
        self.assertNotIn(secret_action, messages)

    def test_HTTP互換ではgroup警告の詳細を維持する(self):
        self.get_group_members.return_value = [
            FakeUser('unknown', 'user-1'),
        ]

        with patch.object(async_task_processor.logging, 'warning') as warning:
            async_task_processor.process_action(
                self.bot,
                'group:group-1',
                'hello',
                FakeUser,
                self.get_group_members,
                {'group_interval': 0},
            )

        warning.assert_called_once_with(
            'interface not found: unknown:user-1 hello')


class ForwardInterfaceTest(unittest.TestCase):
    """実Scenarioからtaskを作り、Userを保ったままLINE／LIFFへ渡す。"""

    @classmethod
    def setUpClass(cls):
        from tests.plugin import test_webchat_runtime_e2e
        cls.fixture = test_webchat_runtime_e2e.WebchatRuntimeE2ETest
        cls.fixture.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.tearDownClass()

    def setUp(self):
        from plugin.liff.interface import LiffPlugin_Interface
        from plugin.line.interface import LinePlugin_Interface
        from plugin.webchat.state import TokenPlayerStatus
        from users import User
        self.user = User('line', 'user,artificial-user')
        self.line = LinePlugin_Interface('story', {
            'line_access_token': 'artificial-token', 'line_channel_secret': 'artificial-secret',
        })
        self.line.api = Mock()
        self.liff = LiffPlugin_Interface('story', {'allow_origin': '*'})
        self.bot = self.fixture.runtime.BotRuntime('story', {
            'line': self.line, 'liff': self.liff,
        }, None, state_namespace='shared-story')
        self.bot.scenario = self.fixture.scenario_module.ScenarioBuilder.build_from_table([
            ['##test.forward', '@forward', 'story', '#record'],
            ['##test.liff', '@forward', 'story', '#record', 'liff'],
            ['##test.delay', '@delay', '5', 'story', '#record'],
            ['##test.delay-liff', '@delay', '5', 'story', '#record', 'liff'],
            ['##test.missing', '@forward', 'story', '#record', 'missing'],
            ['##test.chain', '@forward', 'story', '#again', 'liff'],
            ['#again', '@forward', 'story', '#record'],
            ['#record', '@set', '$saved', '1'],
            ['', '@set', '$interface', '$$service_name'], ['', '@set', '$identity', '$$user_id'],
            ['', '保存しました'],
        ], version=3)
        self.bot.check_reload = Mock()
        self.saved = {}
        saved = self.saved

        class Status(TokenPlayerStatus):
            def __init__(self, namespace, user_id):
                self.key = (namespace, user_id)
                super().__init__(namespace, user_id, saved.get(self.key))

            def save(self):
                saved[self.key] = self.export()
                self.mark_saved()

        main = types.ModuleType('main')
        main.get_bot = lambda name: self.bot if name == 'story' else None
        for patcher in (
                patch.dict(sys.modules, {'main': main}),
                patch('context._get_player_status_class', return_value=Status),
                patch('requests.sessions.Session.request', side_effect=AssertionError('外部通信は禁止'))):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch('common_commands.task_client.create_task', return_value='artificial-task')
        self.enqueue = patcher.start()
        self.addCleanup(patcher.stop)

    def execute_task(self, params):
        from users import User
        return async_task_processor.process_action(
            self.bot, params['user'], params['action'], User, Mock(), {},
            interface_name=params.get('interface'))

    def test_LIFFのメニュー変更は同じBotのLINE紐付けだけを行う(self):
        from plugin.liff.richmenu import register_runtime
        register_runtime({})
        self.bot.scenario = self.fixture.scenario_module.ScenarioBuilder.build_from_table([
            ['##liff.menu', '@richmenu', 'main'], ['', '@set', '$opened', '1'],
        ], version=3)
        self.bot.scenario.richmenu_ids = {'main': 'richmenu-artificial'}
        context = self.liff.create_context(self.user, '##liff.menu', {})
        self.assertEqual('[]', self.bot.handle_action(context))
        self.line.api.link_rich_menu.assert_called_once_with('artificial-user', 'richmenu-artificial')
        self.line.api.push.assert_not_called()
        self.line.api.reply.assert_not_called()
        self.assertEqual(1, self.saved[('shared-story', self.user.serialize())]['flags']['$opened'])

    def test_LIFFからの転送と遅延は選択したinterfaceで実行しセーブを共有する(self):
        for action, selected, delay in (
                ('forward', None, None), ('liff', 'liff', None),
                ('delay', None, 5), ('delay-liff', 'liff', 5)):
            with self.subTest(action=action):
                self.enqueue.reset_mock()
                self.line.api.reset_mock()
                context = self.liff.create_context(self.user, '##test.' + action, {})
                self.assertEqual('[]', self.bot.handle_action(context))
                task = self.enqueue.call_args.kwargs
                self.assertEqual(delay, task['delay_seconds'])
                self.assertEqual(selected, task['params'].get('interface'))
                self.assertEqual(self.user.serialize(), task['params']['user'])
                result = self.execute_task(task['params'])
                if selected == 'liff':
                    self.assertEqual(['保存しました'], json.loads(result))
                    self.line.api.push.assert_not_called()
                else:
                    self.line.api.push.assert_called_once()
                self.assertEqual({('shared-story', self.user.serialize())}, set(self.saved))
                flags = self.saved[('shared-story', self.user.serialize())]['flags']
                self.assertEqual(1, flags['$saved'])
                self.assertEqual(selected or 'line', flags['$interface'])
                self.assertEqual(self.user.serialize(), flags['$identity'])

    def test_既定転送はLINEだけを検査し未登録の明示指定はqueueへ送らない(self):
        self.bot.interfaces.pop('liff')
        context = types.SimpleNamespace(user=self.user, service_name='liff', add_reaction=Mock())
        import common_commands
        command = common_commands.CommonCommands_Runtime({'reset_keyword': '!reset'})
        command.run_command(context, None, '@forward', ['story', '#record'])
        self.enqueue.assert_called_once()
        context.add_reaction.assert_not_called()
        self.enqueue.reset_mock()
        command.run_command(context, None, '@forward', ['story', '#record', 'missing'])
        self.enqueue.assert_not_called()
        context.add_reaction.assert_called_once()

    def test_非同期非対応のinterfaceは生成前と受信後の両方で拒否する(self):
        context = types.SimpleNamespace(user=self.user, service_name='liff', add_reaction=Mock())
        import common_commands
        command = common_commands.CommonCommands_Runtime({'reset_keyword': '!reset'})
        for service in ('webchat', 'browser_alias'):
            self.bot.interfaces[service] = self.fixture.interface
            for name, options in (
                    ('@forward', ['story', '#record', service]),
                    ('@delay', ['5', 'story', '#record', service])):
                command.run_command(context, None, name, options)
            with self.assertRaises(async_task_processor.TaskProcessingError) as rejected:
                self.execute_task({'user': self.user.serialize(), 'action': '#record', 'interface': service})
            self.assertEqual(404, rejected.exception.status_code)
        self.enqueue.assert_not_called()
        self.assertEqual(4, context.add_reaction.call_count)
        self.line.api.push.assert_not_called()

    def test_interface指定は後続の転送へ自動継承しない(self):
        self.bot.handle_action(self.liff.create_context(self.user, '##test.chain', {}))
        self.execute_task(self.enqueue.call_args.kwargs['params'])
        following = self.enqueue.call_args.kwargs['params']
        self.assertNotIn('interface', following)
        self.execute_task(following)
        self.line.api.push.assert_called_once()


class AsyncGroupProcessorTest(unittest.TestCase):
    def test_成功時はreload後にmanagerの結果を返す(self):
        order = []
        bot = Mock()
        bot.check_reload.side_effect = lambda: order.append('reload')
        manager = Mock()
        manager.handle_batch_process_request.side_effect = (
            lambda task_id, batch_index:
            (order.append((task_id, batch_index)) or
             ({'message': '処理完了'}, 200)))
        manager_class = Mock(return_value=manager)

        result = async_task_processor.process_group_batch(
            'bot', bot, 'task-1', 2, manager_class)

        self.assertEqual(result, {'message': '処理完了'})
        self.assertEqual(order, ['reload', ('task-1', 2)])
        manager_class.assert_called_once_with('bot', bot_instance=bot)

    def test_既存の非200を状態付きerrorにする(self):
        bot = Mock()
        manager = Mock()
        manager.handle_batch_process_request.return_value = (
            {'error': 'task not found'}, 404)

        with self.assertRaises(
                async_task_processor.TaskProcessingError) as raised:
            async_task_processor.process_group_batch(
                'bot', bot, 'task-1', 0,
                Mock(return_value=manager),
            )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.public_message, 'task not found')
