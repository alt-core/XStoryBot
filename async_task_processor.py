"""HTTPとキューワーカーで共有する非同期タスクの業務処理。"""

import logging
import time

import utility


class TaskProcessingError(Exception):
    """入力不備など、既知の処理失敗をHTTP状態とともに表す。"""

    def __init__(self, status_code, public_message):
        super().__init__(public_message)
        self.status_code = status_code
        self.public_message = public_message


def get_task_interface(bot, service):
    """指定Botから、非同期タスクで実行できるinterfaceだけを取得する。"""
    interface = bot.get_interface(service) if bot is not None else None
    if interface is not None and getattr(interface, 'supports_task_execution', True):
        return interface
    return None


def _do_action_iter(result, bot, user, action, attrs, get_group_members,
                    options, sleep_func=time.sleep, level=0,
                    log_values=True, failures=None, interface_name=None):
    if level > 20:
        if log_values:
            logging.warning(f'group infinite loop: {user} {action}')
        else:
            logging.warning('group infinite loop is detected')
        raise TaskProcessingError(400, 'infinite loop is detected')

    if user.service_name == 'group':
        for member in get_group_members(user.user_id):
            _do_action_iter(
                result, bot, member, action, attrs,
                get_group_members, options, sleep_func, level + 1,
                log_values, failures, interface_name,
            )
            interval = options.get('group_interval', 100)
            if interval > 0:
                sleep_func(interval / 1000)
        return

    service = user.service_name if interface_name is None else interface_name
    interface = get_task_interface(bot, service)
    if interface is not None:
        context = interface.create_context(user, action, attrs)
        outcome = bot.handle_action(context)
        if outcome is None and failures is not None and level == 0:
            # handle_action は再試行を尽くすと None を返す。単一userの失敗だけを呼出し側へ伝える
            # （group展開の各memberは記録済みのログに任せ、fan-out全体の再試行にはしない）
            failures.append(str(user))
        result.append(str(outcome))
    elif level == 0:
        raise TaskProcessingError(404, 'not found')
    else:
        if log_values:
            logging.warning(f'interface not found: {user} {action}')
        else:
            logging.warning('interface not found for a group member')


def process_decoded_action(bot, serialized_user, action, attrs, user_class,
                           get_group_members, options,
                           sleep_func=time.sleep, log_values=True,
                           failures=None, interface_name=None):
    """decode済みのactionを処理し、文字列化した結果を返す。

    failures に list を渡すと、単一userの handle_action 失敗をそこへ追記する。
    """
    user = user_class.deserialize(serialized_user) if serialized_user else None
    if user is None or action is None:
        raise TaskProcessingError(400, 'invalid parameter')
    if interface_name is not None and (
            not isinstance(interface_name, str) or not interface_name):
        raise TaskProcessingError(400, 'invalid interface')

    bot.check_reload()
    result = []
    _do_action_iter(
        result, bot, user, action, attrs,
        get_group_members, options, sleep_func, log_values=log_values,
        failures=failures, interface_name=interface_name,
    )
    return ''.join(result)


def process_action(bot, serialized_user, encoded_action, user_class,
                   get_group_members, options, sleep_func=time.sleep,
                   log_values=True, failures=None, interface_name=None):
    """一件のactionをdecodeし、共通のaction処理へ渡す。"""
    action, attrs = utility.decode_action_string(encoded_action)
    return process_decoded_action(
        bot, serialized_user, action, attrs,
        user_class, get_group_members, options, sleep_func, log_values,
        failures=failures, interface_name=interface_name,
    )


def process_group_batch(bot_name, bot, task_id, batch_index, manager_class):
    """一件のグループ配信バッチを処理する。"""
    if not task_id:
        raise TaskProcessingError(400, 'missing task_id parameter')

    bot.check_reload()
    processor = manager_class(bot_name, bot_instance=bot)
    result, status_code = processor.handle_batch_process_request(
        task_id, batch_index)
    if status_code != 200:
        raise TaskProcessingError(
            status_code, result.get('error', 'Unknown error'))
    return result
