"""LINEの実入力・返信処理を、外部送信せずに検証する。"""

import copy
import json
from pathlib import Path

from plugin.line.api import LineApiClient
from plugin.line.interface import LinePlugin_Interface
from plugin.webchat.context import DEFAULT_ALLOWED_COMMANDS
from tools.local_support import LocalInputError, read_json, write_json
from users import User


USER_ID = 'local-player'
ALLOWED_COMMANDS = DEFAULT_ALLOWED_COMMANDS.difference({
    '@webhook', '@WebHook', '@postjson', '@PostJSON', '@getjson', '@GetJSON',
}).union({'@error', '@Error', '@raise', '@例外'})


class RecordedLineApi(LineApiClient):
    def __init__(self):
        super().__init__('local-unused')
        self.records = []

    def _post(self, path, body=None, headers=None):
        self.records.append({'path': path, 'body': copy.deepcopy(body)})


class LocalLineInterface(LinePlugin_Interface):
    def __init__(self, bot_name, params):
        super().__init__(bot_name, {
            **params,
            'line_access_token': 'local-unused',
            'line_channel_secret': 'local-unused',
            'line_api_retry_count': 1,
            'retry_count': 0,
            'allow_special_action_text_for_debug': False,
        })
        self.api = RecordedLineApi()

    def should_raise_exceptions(self):
        return True

    @staticmethod
    def check_command_policy(command):
        if command not in ALLOWED_COMMANDS:
            raise ValueError(f'ローカル検証では実行できないcommandです: {command}')

    def context_for_input(self, input_data, choices):
        kind = input_data['type']
        event = {
            'source': {'type': 'user', 'userId': USER_ID},
            'replyToken': 'local-reply-token',
        }
        if kind == 'start':
            action = input_data.get('action', '##line.follow')
            if action == '##line.follow':
                event['type'] = 'follow'
            else:
                context = self.create_context(User('line', f'user,{USER_ID}'), action, {})
                context.check_command_policy = self.check_command_policy
                return context
        elif kind == 'text':
            event.update(type='message', message={'type': 'text', 'text': input_data['text']})
        else:
            index = input_data['index']
            if index >= len(choices):
                raise ValueError(f'choice indexが範囲外です: {index} / {len(choices)}')
            choice = choices[index]
            if choice['type'] == 'message':
                event.update(type='message', message={'type': 'text', 'text': choice['text']})
            elif choice['type'] == 'postback':
                event.update(type='postback', postback={'data': choice['data']})
            else:
                raise ValueError('URIの選択はローカル検証の対象外です')
        context = self.create_context_from_line_event(event)
        if context is None:
            raise ValueError('入力からLINE contextを作れませんでした')
        context.check_command_policy = self.check_command_policy
        return context


def messages_from_records(records):
    return [
        message for record in records
        for message in (record.get('body') or {}).get('messages', [])
    ]


def choices_from_messages(messages):
    choices = []
    for message in messages:
        if message.get('type') == 'template':
            template = message.get('template', {})
            if template.get('type') in ('buttons', 'confirm'):
                choices.extend(template.get('actions', []))
        if message.get('type') == 'imagemap':
            choices.extend(message.get('actions', []))
        choices.extend(
            item['action'] for item in message.get('quickReply', {}).get('items', [])
            if 'action' in item)
    return choices


def player_snapshot(store, status_id):
    stored = store.load_player_status(status_id)
    if stored is None:
        return None, {'flags': {}}
    data = dict(stored.data)
    data['flags'] = json.loads(data.pop('value', None) or '{}')
    return stored.version.value, data


def compare_expected(expected, messages, choices, player):
    actual = {
        'texts': [message['text'] for message in messages if message.get('type') == 'text'],
        'choices': [{'type': choice['type'], 'label': choice.get('label')} for choice in choices],
    }
    differences = []
    for key in ('texts', 'choices'):
        if key in expected and expected[key] != actual[key]:
            differences.append({'field': key, 'expected': expected[key], 'actual': actual[key]})
    flags = player['flags']
    for key, value in expected.get('flags', {}).items():
        if key not in flags or flags[key] != value:
            differences.append({
                'field': f'flags.{key}', 'expected': value,
                'present': key in flags, 'actual': flags.get(key),
            })
    for key in expected.get('absent_flags', []):
        if key in flags:
            differences.append({'field': f'flags.{key}', 'expected': 'absent', 'actual': flags[key]})
    return actual, differences


def run_case(bot, interface, store, case, storage_root, input_hash):
    path = Path(storage_root) / 'metadata' / 'line-session.json'
    binding = {'schema_version': 1, 'bot': bot.name, 'namespace': bot.state_namespace, 'user': USER_ID}
    session = read_json(path) if path.exists() else dict(binding)
    if any(session.get(key) != value for key, value in binding.items()):
        raise LocalInputError('sessionのBot／namespace／userが一致しません')
    status_id = f'{bot.state_namespace}:line:user,{USER_ID}'
    result = {'name': case['name'], 'passed': True, 'steps': [], 'seed': case.get('seed', 0)}
    for index, step in enumerate(case['steps'], 1):
        row = {'step': index, 'input': step['input'], 'phase': 'input'}
        try:
            version, _ = player_snapshot(store, status_id)
            if step['input']['type'] == 'choice' and (
                    'payloads' not in session or session.get('version') != version):
                raise ValueError('再開できる直前の選択肢がありません（中断または状態変更）')
            choices = choices_from_messages(messages_from_records(session.get('payloads', [])))
            context = interface.context_for_input(step['input'], choices)
            row['previous_input_hash'] = session.get('input_hash')
            write_json(path, binding)
            session = dict(binding)
            interface.api.records.clear()
            row['phase'] = 'runtime'
            outcome = bot.handle_action(context)
            if outcome is None:
                raise ValueError('Runtimeが成功結果を返しませんでした')
            version, player = player_snapshot(store, status_id)
            records = copy.deepcopy(interface.api.records)
            session = {**binding, 'version': version, 'payloads': records, 'input_hash': input_hash}
            write_json(path, session)
            messages = messages_from_records(records)
            choices = choices_from_messages(messages)
            actual, differences = compare_expected(step.get('expect', {}), messages, choices, player)
            row.update(
                phase='assert', payloads=records, player=player,
                scene=player.get('scene'), actual=actual, differences=differences,
                passed=not differences,
            )
            if differences:
                result['passed'] = False
        except Exception as error:
            _, player = player_snapshot(store, status_id)
            row.update(passed=False, error_type=type(error).__name__, error=str(error),
                       player=player, scene=player.get('scene'))
            result['passed'] = False
        result['steps'].append(row)
        if not result['passed']:
            break
    return result
