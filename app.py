# GAE や Cloud Run に指定するエントリーポイント (API サーバー)

import os
from bottle import Bottle, static_file, response, debug
import importlib

import main # 読み込むことで initialize が完了する
import settings

app = Bottle()

@app.route('/')
def server_root():
    response.status = 404
    response.set_header("X-Robots-Tag", "noindex, nofollow")
    return "404 Not Found"

@app.get('/healthz')
def health_check():
    response.set_header('Content-Type', 'application/json; charset=utf-8')
    return '{"status":"ok"}'

@app.route('/static/<filepath:path>')
def server_static(filepath):
    return static_file(filepath, root='static')

import webapi as root_webapi
app.merge(root_webapi.app)

import dashboard
app.merge(dashboard.app)


# plugin をループして、存在する場合はマージする
for plugin_name in main.get_plugins().keys():
    try:
        plugin_webapi = importlib.import_module(f'plugin.{plugin_name}.webapi')
    except ModuleNotFoundError as e:
        continue
    app.merge(plugin_webapi.app)

if __name__ == '__main__':
    if settings.DEPLOY_ENV == 'dev':
        debug(True)
    else:
        debug(False)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
