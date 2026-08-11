# GAE や Cloud Run に指定するエントリーポイント (ビルドのバッチ処理用サーバー)

import logging
import os

from bottle import Bottle, abort, debug, request, response

import auth
import main
import settings
import utility


app = Bottle()


def abort_json(code, msg):
    abort(code, utility.make_error_json(code, msg))


def set_json_response_headers():
    response.set_header('Content-Type', 'text/plain; charset=utf-8')
    response.set_header(
        'Access-Control-Allow-Origin',
        settings.SERVICE_SETTINGS['app']['base_url'])
    response.set_header('Access-Control-Allow-Headers', 'Authorization,Content-Type,X-API-Token')
    response.set_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')


def _get_auth_token():
    # ヘッダーから認証トークンを取得
    return request.headers.get('X-API-Token', '')


@app.route('/')
def server_root():
    response.status = 404
    response.set_header("X-Robots-Tag", "noindex, nofollow")
    return "404 Not Found"


@app.get('/healthz')
def health_check():
    response.set_header('Content-Type', 'application/json; charset=utf-8')
    return '{"status":"ok"}'


# OPTIONS リクエストに対するハンドラを app.route を使って追加
@app.route('/api/build/<bot_name>', method=['OPTIONS'])
def options_api_build(bot_name):
    set_json_response_headers()
    return ''


@app.post('/api/build/<bot_name>')
def api_build(bot_name):
    set_json_response_headers()
    if not auth.check_token(_get_auth_token()):
        abort_json(401, 'invalid token')

    bot = main.get_bot(bot_name)
    if not bot:
        abort_json(404, 'not found')

    task_id = request.params.getunicode('task_id', '').strip()

    options = {}
    options['skip_image'] = (request.params.getunicode('skip_image') == 'true')
    options['force'] = (request.params.getunicode('force') == 'true')

    version = main.get_options().get('scenario_version', 1)

    logging.info(f"start building...: options: {options}, version: {version}")

    ok, err = bot.build_scenario(task_id=task_id, options=options, version=version)

    if ok:
        # リロードに成功した
        return utility.make_ok_json("反映作業に成功しました。")
    else:
        return utility.make_ng_json(f"反映作業に失敗しました。\n\n{err}")


@app.get('/_ah/start')
def start_handler():
    response.set_header('Content-Type', 'text/plain; charset=utf-8')
    return 'Start successful'


@app.get('/_ah/stop')
def stop_handler():
    response.set_header('Content-Type', 'text/plain; charset=utf-8')
    return 'Stop successful'


if __name__ == "__main__":
    if settings.DEPLOY_ENV == 'dev':
        debug(True)
    else:
        debug(False)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
