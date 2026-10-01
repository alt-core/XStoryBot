"""Lambda非同期呼出しからaction・グループ配信処理を呼ぶ入口。"""

import logging
import os
import re
import uuid

from async_task_processor import process_action, process_group_batch
from cloud_backend.aws.state_store import (
    TASK_EXECUTION_BUSY,
    TASK_EXECUTION_CLAIMED,
    TASK_EXECUTION_COMPLETED,
)


_BOT_NAME_PATTERN = re.compile(r'^[-_a-zA-Z0-9]+$')
_QUEUE_KIND_MAP = {
    'action-queue': 'action',
    'group-message-queue': 'group_batch',
}
_ACTION_LEASE_SECONDS = 360  # ActionWorkerFunctionのTimeout（300秒）より長くする
_GROUP_LEASE_SECONDS = 960


class _TaskExecutionBusy(Exception):
    """同じ論理taskを別のworkerが実行中であることを表す。"""


class ActionNotDelivered(RuntimeError):
    """handle_actionが再試行を尽くして応答を返せなかったことを表す。"""


def _validate_task_id(value):
    if not isinstance(value, str):
        raise ValueError('task_idが不正です')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValueError('task_idが不正です') from error
    if str(parsed) != value:
        raise ValueError('task_idが不正です')


def validate_worker_kind(value):
    """起動時と封筒検査時にworker種別の設定不備を検出する。"""
    if value not in ('action', 'group_batch'):
        raise ValueError('AWS workerのkindが設定されていません')


def _load_envelope(envelope, worker_kind):
    if not isinstance(envelope, dict):
        raise ValueError('AWS taskの封筒が不正です')
    if type(envelope.get('version')) is not int or envelope['version'] != 1:
        raise ValueError('AWS taskのversionが不正です')
    task_id = envelope.get('task_id')
    _validate_task_id(task_id)
    queue_name = envelope.get('queue_name')
    kind = envelope.get('kind')
    if (
            not isinstance(queue_name, str)
            or not isinstance(kind, str)
            or _QUEUE_KIND_MAP.get(queue_name) != kind):
        raise ValueError('AWS taskのkindが不正です')
    validate_worker_kind(worker_kind)
    if worker_kind != kind:
        raise ValueError('AWS taskとworkerのkindが一致しません')
    bot_name = envelope.get('bot_name')
    if not isinstance(bot_name, str) or not _BOT_NAME_PATTERN.fullmatch(bot_name):
        raise ValueError('Bot名が不正です')
    params = envelope.get('params')
    if not isinstance(params, dict) or params.get('task_id') != task_id:
        raise ValueError('AWS taskのparameterが不正です')
    return envelope


def _load_dependencies():
    import main
    import settings
    import users
    from cloud_backend.aws import create_state_store
    from group_message_task_manager import GroupMessageTaskManager

    return {
        'get_bot': main.get_bot,
        'user_class': users.User,
        'get_group_members': users.get_group_members,
        'options': settings.OPTIONS,
        'manager_class': GroupMessageTaskManager,
        'execution_store': create_state_store(),
        'worker_kind': os.environ.get('XSBOT_AWS_WORKER_KIND'),
    }


def _claim_or_skip(execution_store, execution_key, owner, lease_seconds):
    result = execution_store.try_claim_task_execution(
        execution_key, owner, lease_seconds)
    if result == TASK_EXECUTION_COMPLETED:
        return False
    if result == TASK_EXECUTION_BUSY:
        raise _TaskExecutionBusy()
    if result != TASK_EXECUTION_CLAIMED:
        raise ValueError('実行記録の取得結果が不正です')
    return True


def _group_parameters(params):
    message_task_id = params.get('message_task_id', '')
    batch_index = params.get('batch_index', 0)
    if not isinstance(message_task_id, str):
        raise ValueError('group batch parameterが不正です')
    if isinstance(batch_index, bool) or not isinstance(batch_index, (str, int)):
        raise ValueError('group batch parameterが不正です')
    try:
        batch_index = int(batch_index)
    except (TypeError, ValueError):
        raise ValueError('group batch parameterが不正です') from None
    if batch_index < 0 or str(batch_index) != str(params.get('batch_index', 0)):
        raise ValueError('group batch parameterが不正です')
    message_task_id = message_task_id.strip()
    if not message_task_id:
        raise ValueError('group batch parameterが不正です')
    return message_task_id, batch_index


def _process_task(event, dependencies, owner):
    envelope = _load_envelope(event, dependencies['worker_kind'])
    params = envelope['params']
    if envelope['kind'] == 'action':
        serialized_user = params.get('user', '')
        encoded_action = params.get('action', '')
        if not isinstance(serialized_user, str) or not isinstance(encoded_action, str):
            raise ValueError('action parameterが不正です')
        execution_key = f'action:{envelope["bot_name"]}:{envelope["task_id"]}'
        lease_seconds = _ACTION_LEASE_SECONDS
        logging.info(
            'AWS action task: task_id=%s, bot_name=%s, user=%s, '
            'action=%s, owner=%s',
            envelope['task_id'], envelope['bot_name'], serialized_user,
            encoded_action, owner,
        )
    else:
        message_task_id, batch_index = _group_parameters(params)
        execution_key = (
            f'group:{envelope["bot_name"]}:{message_task_id}:{batch_index}')
        lease_seconds = _GROUP_LEASE_SECONDS

    execution_store = dependencies['execution_store']
    try:
        if not _claim_or_skip(execution_store, execution_key, owner, lease_seconds):
            logging.info(
                'AWS task already completed: task_id=%s, owner=%s',
                envelope['task_id'], owner)
            return
        bot = dependencies['get_bot'](envelope['bot_name'])
        if bot is None:
            raise ValueError('Botが見つかりません')
        if envelope['kind'] == 'action':
            failures = []
            process_action(
                bot, serialized_user, encoded_action,
                dependencies['user_class'], dependencies['get_group_members'],
                dependencies['options'], log_values=True, failures=failures,
                interface_name=params.get('interface'),
            )
            if failures:
                raise ActionNotDelivered(', '.join(failures))
        else:
            process_group_batch(
                envelope['bot_name'], bot, message_task_id, batch_index,
                dependencies['manager_class'],
            )
        execution_store.complete_task_execution(execution_key, owner)
    except _TaskExecutionBusy:
        raise
    except Exception:
        try:
            execution_store.release_task_execution(execution_key, owner)
        except Exception as error:
            # 解放失敗で元の取得・業務処理・完了保存の例外を上書きしない。
            logging.error(
                'AWS task lease release failed: owner=%s, error_type=%s',
                owner, type(error).__name__)
        raise
    if envelope['kind'] == 'action':
        logging.info(
            'AWS action task completed: task_id=%s, owner=%s',
            envelope['task_id'], owner)


def lambda_handler(event, context):
    """一つの封筒を処理し、失敗をLambdaの非同期再試行へ伝播する。"""
    dependencies = _load_dependencies()
    owner = str(uuid.uuid4())
    try:
        _process_task(event, dependencies, owner)
    except _TaskExecutionBusy:
        logging.warning('AWS task is being processed elsewhere: owner=%s', owner)
        raise
    except Exception as error:
        logging.exception(
            'AWS task failed: owner=%s, error_type=%s, error=%s',
            owner, type(error).__name__, error,
        )
        raise
    return {'status': 'ok'}
