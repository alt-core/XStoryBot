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
