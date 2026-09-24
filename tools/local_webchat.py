"""完成したローカルScenarioを既存Webchatへ接続する。"""

import base64
import copy
import errno
import os
from pathlib import Path
import secrets
import signal
import sys
from urllib.parse import unquote, urlsplit
from wsgiref.simple_server import make_server

import requests

from cloud_backend.contracts import (
    InvalidObjectReferenceError, ObjectNotFoundError, ObjectStoreError,
)
from cloud_backend.local.object_store import LocalObjectStore
from cloud_backend.local.storage import atomic_write, checked_path
from plugin.webchat.token import WebchatTokenCodec
import utility


def _origin(public_base_url):
    port = urlsplit(public_base_url).port
    return 'http://127.0.0.1' + (f':{port}' if port != 80 else '')


def prepare_settings(config, bot_name, scenario_uri, asset_urls):
    """実settingsをimportせず、選択Botと固定済みのLIFF連携先を準備する。"""
    if config.get('cloud', {}).get('provider') != 'local':
        raise ValueError('ローカルWebchatにはcloud.provider=localが必要です')
    if bot_name not in config.get('bots', {}):
        raise ValueError('指定したBotが設定にありません')
    local = config['local']
    if not Path(local['storage_root']).is_absolute():
        raise ValueError('local.storage_rootは解決済みの絶対pathで指定してください')
    store = LocalObjectStore(local['storage_root'], local['public_base_url'])
    store.load_scenario(scenario_uri)
    media_base = f'{store.public_base_url}/{store.store_id}/'
    normalized_assets = {}
    for source, target in asset_urls.items():
        if not isinstance(source, str) or not isinstance(target, str):
            raise ValueError('媒体URL mapには文字列を指定してください')
        source = requests.utils.requote_uri(source)
        if source in normalized_assets:
            raise ValueError('正規化後の媒体URLが重複しています')
        parsed = urlsplit(target)
        if not target.startswith(media_base) or parsed.query or parsed.fragment:
            raise ValueError('媒体URL mapの保存先がこのrootではありません')
        store.public_file(unquote(target[len(media_base):]))
        normalized_assets[source] = target

    key_path = checked_path(store.storage_root, 'private/webchat-signing-key')
    if not key_path.exists():
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii')
        try:
            atomic_write(key_path, key, exclusive=True)
        except FileExistsError:
            pass
    key = key_path.read_text(encoding='ascii').strip()
    WebchatTokenCodec(key)

    prepared = copy.deepcopy(config)
    bot = prepared['bots'][bot_name]
    interface_params = next((item.get('params', {}) for item in bot.get('interfaces', [])
                             if item.get('type') == 'webchat'), {})
    params = utility.merge_params(
        prepared.get('plugins', {}).get('webchat', {}), interface_params)
    params['constants'] = {
        **utility.normalize_constants(prepared.get('plugins', {}).get('webchat', {}).get('constants', {}), scalar_only=True),
        **utility.normalize_constants(interface_params.get('constants', {}), scalar_only=True),
    }
    origin = _origin(store.public_base_url)
    params.update({
        'enabled': True,
        'scenario_uri': scenario_uri,
        'signing_key': key,
        'deployment': store.store_id,
        'scenario_compatibility_epoch': params.get('scenario_compatibility_epoch') or 'local-v1',
        'start_action': params.get('start_action') or '##line.follow',
        'self_origin': [origin],
        'allowed_origins': [origin],
        'external_http_origins': [],
        'allowed_commands': [],
    })
    def liff_interfaces(definition):
        return [item for item in definition.get('interfaces', []) if item.get('type') == 'liff']

    bot['interfaces'] = [{'type': 'webchat', 'params': params}] + liff_interfaces(bot)
    selected = {bot_name: bot}
    apps = params.get('liff_apps', {}) or {}
    if not isinstance(apps, dict):
        raise ValueError('liff_appsにはページ名ごとの設定を指定してください')
    for app in apps.values():
        name = app.get('bot') if isinstance(app, dict) else None
        if name in selected:
            continue
        peer = prepared['bots'].get(name)
        if peer is None or not liff_interfaces(peer):
            raise ValueError('LIFF連携先のBotとliff interfaceを設定してください')
        peer_options = next((item.get('params') or {} for item in peer.get('interfaces', [])
                             if item.get('type') == 'webchat'), {})
        peer_params = utility.merge_params(prepared.get('plugins', {}).get('webchat', {}), peer_options)
        peer_params['constants'] = {
            **utility.normalize_constants(prepared.get('plugins', {}).get('webchat', {}).get('constants', {}), scalar_only=True),
            **utility.normalize_constants(peer_options.get('constants', {}), scalar_only=True),
        }
        peer_uri = peer_params.get('scenario_uri')
        if not peer_uri:
            raise ValueError('LIFF連携先のwebchat.scenario_uriに、このrootのビルド済みURIを指定してください')
        store.load_scenario(peer_uri)
        for key in ('enabled', 'signing_key', 'deployment', 'self_origin', 'allowed_origins',
                    'external_http_origins', 'allowed_commands'):
            peer_params[key] = copy.deepcopy(params[key])
        peer_params['scenario_compatibility_epoch'] = peer_params.get('scenario_compatibility_epoch') or 'local-v1'
        peer_params['start_action'] = peer_params.get('start_action') or '##line.follow'
        peer_params['liff_apps'] = {}
        peer['interfaces'] = [{'type': 'webchat', 'params': peer_params}] + liff_interfaces(peer)
        selected[name] = peer
    prepared['bots'] = selected
    prepared['plugins'] = {
        name: values for name, values in prepared.get('plugins', {}).items()
        if name in ('line.quick_reply', 'line.quick_reply_v2', 'line.more', 'liff')
    }
    prepared['auth'] = {}
    prepared['local'].pop('assets', None)
    prepared['local']['store_id'] = store.store_id
    prepared['local']['webchat_asset_urls'] = normalized_assets
    return prepared


def create_app(webchat_app, store):
    """公開媒体routeを追加し、全routeのHostと読取要求のOriginを絞る。"""
    from bottle import Bottle, abort, static_file

    app = Bottle()

    @app.route('/local-media/<store_id>/<key:path>', method=['GET', 'HEAD'])
    def public_media(store_id, key):
        if store_id != store.store_id:
            abort(404, '公開媒体がありません')
        try:
            path, content_type = store.public_file(key)
        except (InvalidObjectReferenceError, ObjectNotFoundError):
            abort(404, '公開媒体がありません')
        except ObjectStoreError:
            abort(500, '公開媒体を読み込めません')
        return static_file(path.name, root=str(path.parent), mimetype=content_type,
                           headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-cache'})

    app.merge(webchat_app)
    origin = _origin(store.public_base_url)
    hosts = {urlsplit(store.public_base_url).netloc, urlsplit(origin).netloc}

    def guarded(environ, start_response):
        host_ok = environ.get('HTTP_HOST') in hosts
        supplied_origin = environ.get('HTTP_ORIGIN')
        origin_ok = (environ.get('REQUEST_METHOD') not in ('GET', 'HEAD')
                     or supplied_origin is None or supplied_origin == origin)
        if not host_ok or not origin_ok:
            body = b'Forbidden'
            start_response('403 Forbidden', [
                ('Content-Type', 'text/plain; charset=utf-8'),
                ('Content-Length', str(len(body))), ('Cache-Control', 'no-store'),
            ])
            return [] if environ.get('REQUEST_METHOD') == 'HEAD' else [body]
        return app(environ, start_response)

    return guarded


def run_server():
    """環境を確定した専用processでのみ、実Webchatを起動する。"""
    if (os.environ.get('XSBOT_CLOUD_PROVIDER') != 'local'
            or not os.environ.get('XSBOT_SETTINGS_FILE')):
        raise ValueError('local providerとserve用settingsを環境変数へ指定してください')
    import settings

    if settings.CLOUD_SETTINGS.get('provider') != 'local' or len(settings.BOTS) != 1:
        raise ValueError('serve用設定はlocal providerの1 Botに限定してください')
    local = settings.BACKEND_SETTINGS
    store = LocalObjectStore(local['storage_root'], local['public_base_url'])
    import app_webchat

    bot_name = next(iter(settings.BOTS))
    bot = app_webchat.get_bot(bot_name)
    if bot is None:
        raise ValueError('serve用BotのWebchatが有効ではありません')
    bot.get_interface('webchat').ensure_scenario(bot)
    port = urlsplit(store.public_base_url).port
    with make_server('127.0.0.1', port, create_app(app_webchat.app, store)) as server:
        print(f'Webchat: {_origin(store.public_base_url)}/chat/{bot_name}', file=sys.stderr)
        _serve(server)


def _serve(server):
    stopped = False

    def request_stop(signum, frame):
        nonlocal stopped
        stopped = True

    previous = signal.signal(signal.SIGTERM, request_stop)
    try:
        ready_file = os.environ.get('XSBOT_LOCAL_READY_FILE')
        if ready_file:
            Path(ready_file).write_text('ready', encoding='ascii')
        server.timeout = 0.2
        # SIGTERMでは処理中のturnを完了してから終了する。長時間停止は親が打ち切る。
        while not stopped:
            server.handle_request()
    finally:
        signal.signal(signal.SIGTERM, previous)


def main():
    try:
        run_server()
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        reason = ('指定したポートが使用中です'
                  if isinstance(error, OSError) and error.errno == errno.EADDRINUSE
                  else f'設定・保存先・ポートを確認してください（{type(error).__name__}）')
        print(f'ローカルWebchatを起動できません: {reason}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
