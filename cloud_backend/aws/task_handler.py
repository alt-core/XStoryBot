"""SQSから共通のaction・グループ配信処理を呼ぶLambda入口。"""

import json
import logging
import re
import uuid

from async_task_processor import process_action, process_group_batch


_BOT_NAME_PATTERN = re.compile(r'^[-_a-zA-Z0-9]+$')
_QUEUE_KIND_MAP = {
    'action-queue': 'action',
    'group-message-queue': 'group_batch',
}


def _validate_task_id(value):
    if not isinstance(value, str):
        raise ValueError('task_idが不正です')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValueError('task_idが不正です') from error
    if str(parsed) != value:
        raise ValueError('task_idが不正です')


def _load_envelope(record, backend_settings):
    if record.get('eventSource') != 'aws:sqs':
        raise ValueError('SQS以外のeventは処理できません')

    try:
        envelope = json.loads(record['body'])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError('SQS本文が不正です') from error
    if not isinstance(envelope, dict):
        raise ValueError('SQS本文が不正です')
    if type(envelope.get('version')) is not int or envelope['version'] != 1:
        raise ValueError('SQS本文のversionが不正です')

    task_id = envelope.get('task_id')
    _validate_task_id(task_id)
    queue_name = envelope.get('queue_name')
    kind = envelope.get('kind')
    if _QUEUE_KIND_MAP.get(queue_name) != kind:
        raise ValueError('SQS本文のkindが不正です')

    queue_settings = (
        backend_settings.get('task_queue', {})
        .get('queues', {})
        .get(queue_name, {})
    )
    expected_arn = queue_settings.get('arn')
    if not expected_arn or record.get('eventSourceARN') != expected_arn:
        raise ValueError('SQS送信元が設定と一致しません')

    bot_name = envelope.get('bot_name')
    if not isinstance(bot_name, str) or not _BOT_NAME_PATTERN.fullmatch(
            bot_name):
        raise ValueError('Bot名が不正です')
    params = envelope.get('params')
    if not isinstance(params, dict) or params.get('task_id') != task_id:
        raise ValueError('SQS本文のparameterが不正です')
    return envelope


def _load_dependencies():
    import main
    import settings
    import users
    from group_message_task_manager import GroupMessageTaskManager

    return {
        'backend_settings': settings.BACKEND_SETTINGS,
        'get_bot': main.get_bot,
        'user_class': users.User,
        'get_group_members': users.get_group_members,
        'options': settings.OPTIONS,
        'manager_class': GroupMessageTaskManager,
    }


def _process_record(record, dependencies):
    envelope = _load_envelope(record, dependencies['backend_settings'])
    bot = dependencies['get_bot'](envelope['bot_name'])
    if bot is None:
        raise ValueError('Botが見つかりません')

    params = envelope['params']
    if envelope['kind'] == 'action':
        serialized_user = params.get('user', '')
        encoded_action = params.get('action', '')
        if not isinstance(serialized_user, str) or not isinstance(
                encoded_action, str):
            raise ValueError('action parameterが不正です')
        process_action(
            bot,
            serialized_user,
            encoded_action,
            dependencies['user_class'],
            dependencies['get_group_members'],
            dependencies['options'],
            log_values=False,
        )
        return

    message_task_id = params.get('message_task_id', '')
    batch_index = params.get('batch_index', 0)
    if not isinstance(message_task_id, str):
        raise ValueError('group batch parameterが不正です')
    if isinstance(batch_index, bool) or not isinstance(batch_index, (str, int)):
        raise ValueError('group batch parameterが不正です')
    try:
        batch_index = int(batch_index)
    except (TypeError, ValueError) as error:
        raise ValueError('group batch parameterが不正です') from error
    if batch_index < 0 or str(batch_index) != str(params.get('batch_index', 0)):
        raise ValueError('group batch parameterが不正です')
    process_group_batch(
        envelope['bot_name'],
        bot,
        message_task_id.strip(),
        batch_index,
        dependencies['manager_class'],
    )


def lambda_handler(event, context):
    """SQS batchの各recordを処理し、失敗recordだけを再試行させる。"""
    del context
    dependencies = _load_dependencies()
    failures = []
    records = event.get('Records', []) if isinstance(event, dict) else []
    if not isinstance(records, list):
        raise ValueError('SQS eventが不正です')

    for record in records:
        message_id = record.get('messageId', '') if isinstance(
            record, dict) else ''
        if not message_id:
            raise ValueError('SQS messageIdがありません')
        try:
            _process_record(record, dependencies)
        except Exception as error:
            logging.error(
                'SQS task failed: message_id=%s, error_type=%s',
                message_id, type(error).__name__,
            )
            failures.append({'itemIdentifier': message_id})

    return {'batchItemFailures': failures}
