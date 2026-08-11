import logging
import time

from bottle import Bottle, abort, request, response

import auth
import main
import settings
import users
import utility


app = Bottle()


def abort_json(code, msg, data=None):
    abort(code, utility.make_error_json(code, msg, data))


def set_json_response_headers():
    response.set_header('Content-Type', 'text/plain; charset=utf-8')
    response.set_header('Access-Control-Allow-Origin', '*')


def _get_auth_token():
    # ヘッダーから認証トークンを取得（優先）
    token = request.headers.get('X-API-Token')
    if token is None:
        # 後方互換性のためにパラメータからも取得
        token = request.params.getunicode('token', '').strip()
    return token


def _do_action_iter(result, bot, user, action, attrs, level=0):
    if level > 20:
        logging.warning(f'group infinite loop: {user} {action}')
        abort_json(400, 'infinite loop is detected')

    if user.service_name == 'group':
        for member in users.get_group_members(user.user_id):
            _do_action_iter(result, bot, member, action, attrs, level+1)
            # OPTIONS['group_interval'] ミリ秒待つ
            interval = settings.OPTIONS.get('group_interval', 100)
            if interval > 0:
                time.sleep(interval / 1000)
    else:
        interface = bot.get_interface(user.service_name)
        if interface is not None:
            context = interface.create_context(user, action, attrs)
            result.append(str(bot.handle_action(context)))
        else:
            if level == 0:
                abort_json(404, 'not found')
            else:
                logging.warning(f'interface not found: {user} {action}')


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

    user = None
    if user_str:
        user = users.User.deserialize(user_str)
    if user is None or action is None:
        abort_json(400, 'invalid parameter')

    bot.check_reload()

    result = []
    _do_action_iter(result, bot, user, action, attrs)

    return utility.make_ok_json("".join(result))


@app.get('/_ah/start')
def start_handler():
    response.set_header('Content-Type', 'text/plain; charset=utf-8')
    return 'Start successful'


@app.get('/_ah/stop')
def stop_handler():
    response.set_header('Content-Type', 'text/plain; charset=utf-8')
    return 'Stop successful'


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

    # パラメータ取得
    task_id = request.params.getunicode('message_task_id', '').strip()
    batch_index = int(request.params.getunicode('batch_index', '0'))

    if not task_id:
        abort_json(400, 'missing task_id parameter')

    bot.check_reload()

    from group_message_task_manager import GroupMessageTaskManager
    processor = GroupMessageTaskManager(bot_name, bot_instance=bot)

    result, status_code = processor.handle_batch_process_request(task_id, batch_index)

    if status_code != 200:
        logging.error(f"Error processing batch: {status_code} - {result}")
        abort_json(status_code, result.get('error', 'Unknown error'))

    return utility.make_ok_json(result.get('message', '処理完了'), result)


if __name__ == "__main__":
    app.run(host='localhost', port=8080, debug=True)
