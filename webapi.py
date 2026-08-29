import logging
import time

from bottle import Bottle, HTTPResponse, request, response

import auth
import async_task_processor
import main
import settings
import users
import utility


app = Bottle()


def abort_json(code, msg, data=None):
    raise HTTPResponse(
        body=utility.make_error_json(code, msg, data),
        status=code,
        headers={'Content-Type': 'application/json; charset=utf-8'},
    )


def set_json_response_headers():
    response.set_header('Content-Type', 'text/plain; charset=utf-8')
    response.set_header('Access-Control-Allow-Origin', '*')


def _get_auth_token():
    # ヘッダーから認証トークンを取得（優先）
    token = request.headers.get('X-API-Token')
    if token is None:
        # ヘッダー未指定時はqueryまたはformのtokenを使う。
        token = request.params.getunicode('token', '').strip()
    return token


def _do_action_iter(result, bot, user, action, attrs, level=0):
    try:
        return async_task_processor._do_action_iter(
            result, bot, user, action, attrs,
            users.get_group_members, settings.OPTIONS, time.sleep, level,
        )
    except async_task_processor.TaskProcessingError as error:
        abort_json(error.status_code, error.public_message)


def process_action_task(bot, user_str, action, attrs):
    """HTTPとSQSから同じaction処理を呼ぶための共通入口。"""
    return async_task_processor.process_decoded_action(
        bot, user_str, action, attrs,
        users.User, users.get_group_members, settings.OPTIONS, time.sleep,
    )


def process_group_batch_task(bot_name, bot, task_id, batch_index):
    """HTTPとSQSから同じgroup batch処理を呼ぶための共通入口。"""
    from group_message_task_manager import GroupMessageTaskManager
    return async_task_processor.process_group_batch(
        bot_name, bot, task_id, batch_index, GroupMessageTaskManager)


@app.post('/api/v1/bots/<bot_name>/action')
@app.get('/api/v1/bots/<bot_name>/action')
def do_action(bot_name):
    response.set_header('Content-Type', 'text/plain; charset=utf-8')

    bot = main.get_bot(bot_name)
    if not bot:
        abort_json(404, 'not found')

    user_str = request.params.getunicode('user', '').strip()
    action, attrs = utility.decode_action_string(request.params.getunicode('action', ''))
    token = _get_auth_token()

    # token チェック
    if not auth.check_token(token):
        abort_json(401, 'invalid token')

    logging.info(f"API call: bot_name: {bot_name}, user: {user_str}, action: {action}")

    try:
        result = process_action_task(
            bot, user_str, action, attrs,
        )
    except async_task_processor.TaskProcessingError as error:
        abort_json(error.status_code, error.public_message)

    return utility.make_ok_json(result)


@app.get('/_ah/start')
def start_handler():
    response.set_header('Content-Type', 'text/plain; charset=utf-8')
    return 'Start successful'


@app.get('/_ah/stop')
def stop_handler():
    response.set_header('Content-Type', 'text/plain; charset=utf-8')
    return 'Stop successful'


def _parse_group_member_ids():
    """グループ追加APIのJSON本文からメンバーIDを取得する。"""
    try:
        data = request.json
    except Exception as error:
        if getattr(error, 'status_code', None) == 413:
            raise
        logging.warning('グループメンバー追加APIのJSON解析に失敗しました')
        abort_json(400, 'invalid request format')

    if not isinstance(data, dict):
        abort_json(400, 'invalid request format')

    members_str = data.get('members', '')
    if not isinstance(members_str, str):
        abort_json(400, 'invalid request format')

    members_str = members_str.strip()
    if not members_str:
        return []
    if '\n' in members_str:
        return [member.strip() for member in members_str.split('\n')
                if member.strip()]
    return [member.strip() for member in members_str.split(',')
            if member.strip()]


@app.post('/api/v1/groups/<group_id>/add_members')
def add_group_members(group_id):
    response.set_header('Content-Type', 'application/json; charset=utf-8')

    # 未認証要求では本文を解析しない。
    if not auth.check_token(_get_auth_token()):
        abort_json(401, 'invalid token')

    member_ids = _parse_group_member_ids()
    added_count = 0
    failed_ids = []

    for member_id in member_ids:
        try:
            user = users.User.deserialize(member_id)
            if user is None:
                failed_ids.append(member_id)
                continue
            users.append_group_member(group_id, user)
            added_count += 1
        except Exception as error:
            logging.error(
                'グループメンバーの追加に失敗しました: error_type=%s',
                type(error).__name__)
            failed_ids.append(member_id)

    return utility.make_ok_json(
        f'グループ {group_id} に {added_count} 人のメンバーを追加しました',
        {
            'group_id': group_id,
            'added_count': added_count,
            'failed_count': len(failed_ids),
            'failed_ids': failed_ids,
        },
    )


@app.get('/api/v1/groups/<group_id>/members')
def get_group_members(group_id):
    response.set_header('Content-Type', 'application/json; charset=utf-8')

    if not auth.check_token(_get_auth_token()):
        abort_json(401, 'invalid token')

    try:
        members = users.get_group_members(group_id)
        member_ids = [member.serialize() for member in members]
    except Exception as error:
        logging.error(
            'グループメンバーの取得に失敗しました: error_type=%s',
            type(error).__name__)
        abort_json(500, 'failed to get group members')

    return utility.make_ok_json(
        f'グループ {group_id} のメンバー情報を取得しました ({len(member_ids)} 件)',
        {
            'group_id': group_id,
            'count': len(member_ids),
            'members': member_ids,
        },
    )


@app.route('/api/v1/bots/<bot_name>/process_group_batch', method=['OPTIONS'])
def options_process_group_batch(bot_name):
    set_json_response_headers()
    return ''


@app.post('/api/v1/bots/<bot_name>/process_group_batch')
def process_group_batch(bot_name):
    logging.info(f"process_group_batch: bot_name: {bot_name}")
    response.set_header('Content-Type', 'application/json; charset=utf-8')

    token = request.headers.get('X-API-Token', '')
    if not auth.check_token(token):
        abort_json(401, 'invalid token')

    bot = main.get_bot(bot_name)
    if not bot:
        abort_json(404, 'bot not found')

    task_id = request.params.getunicode('message_task_id', '').strip()
    batch_index = int(request.params.getunicode('batch_index', '0'))

    try:
        result = process_group_batch_task(
            bot_name, bot, task_id, batch_index)
    except async_task_processor.TaskProcessingError as error:
        logging.error(
            f"Error processing batch: {error.status_code} - "
            f"{error.public_message}"
        )
        abort_json(error.status_code, error.public_message)

    return utility.make_ok_json(result.get('message', '処理完了'), result)


if __name__ == "__main__":
    app.run(host='localhost', port=8080, debug=True)
