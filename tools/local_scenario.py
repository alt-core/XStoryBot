#!/usr/bin/env python3
"""ローカルのシナリオをビルドし、LINE検証または実Webchatで確認する。"""

import argparse
import contextlib
import copy
import hashlib
import json
import logging
import math
import mimetypes
import os
from pathlib import Path
import pickle
import random
import re
import socket
import subprocess
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.local_support import (
    LocalInputError, read_json, require_keys, validate_suite, write_bytes, write_json,
)
from tools.local_process import run_process, stop_on_sigterm


ALLOWED_PLUGINS = frozenset({
    'line', 'line.quick_reply', 'line.quick_reply_v2', 'line.image_text',
    'line.more', 'google_sheets', 'tsv', 'webchat', 'liff',
})
SHARED_TABLE_PARAMS = frozenset({
    'evaluate_formula', 'script_sheet', 'constant_sheet', 'ignore_sheet',
})


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise LocalInputError(message)


def parse_args(argv):
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('build', 'webchat', 'verify'):
        child = commands.add_parser(command)
        child.add_argument('--settings', default='settings.yaml')
        child.add_argument('--bot', required=True)
        child.add_argument('--tsv', help='今回だけ使用するTSV manifest')
        child.add_argument('--timeout', type=float, default=600,
                           help='取得・build・各caseの子process上限秒数（既定600）')
        if command == 'webchat':
            child.add_argument('--watch', action='store_true',
                               help='TSV等の変更後に自動で再ビルド・再起動する')
        if command == 'verify':
            child.add_argument('--suite', required=True)
            child.add_argument('--case')
            child.add_argument('--session')
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise LocalInputError('--timeoutは正の有限数にしてください')
    return args


def _mapping(value, label):
    if not isinstance(value, dict):
        raise LocalInputError(f'{label}はobjectで指定してください')
    return value


def _file_path(value, base, label):
    if not isinstance(value, str) or not value:
        raise LocalInputError(f'{label}にファイルpathを指定してください')
    return str((base / value).resolve())


def load_config(args):
    # 実settingsより先に確認し、親のクラウド資格情報処理へ入らない。
    import yaml
    from utility import deep_merge, load_settings_yaml, merge_params
    path = Path(args.settings).resolve()
    try:
        source = _mapping(load_settings_yaml(path), 'settings')
    except yaml.YAMLError as error:
        mark = getattr(error, 'problem_mark', None)
        position = f'（{mark.line + 1}行目）' if mark else ''
        raise LocalInputError(f'設定YAMLの構文が不正です{position}') from None
    environment = os.environ.get('XSBOT_DEPLOY_ENV', '')
    merged = deep_merge(
        _mapping(source.get('*', {}), 'settings.*'),
        _mapping(source.get(environment, {}), '環境別設定'))
    if _mapping(merged.get('cloud', {}), 'cloud').get('provider') != 'local':
        raise LocalInputError('local用settingsのcloud.providerにlocalを指定してください')
    if not re.fullmatch(r'[-_a-zA-Z0-9]+', args.bot):
        raise LocalInputError('Bot名の形式が不正です')
    bots = _mapping(merged.get('bots'), 'bots')
    if args.bot not in bots:
        raise LocalInputError(f'Botが見つかりません: {args.bot}')
    bot = _mapping(bots[args.bot], 'Bot')
    local = copy.deepcopy(_mapping(merged.get('local'), 'local'))
    # 検証caseの保存先はCLIが決める。元設定の内部値は引き継がない。
    local.pop('state_storage_root', None)
    local['storage_root'] = _file_path(local.get('storage_root'), path.parent, 'storage_root')
    if not isinstance(local.get('public_base_url'), str) or not local['public_base_url']:
        raise LocalInputError('local.public_base_urlを指定してください')
    if type(local.get('allow_external_media', False)) is not bool:
        raise LocalInputError('allow_external_mediaは真偽値で指定してください')
    if args.command == 'verify':
        local['allow_external_media'] = False
    assets = _mapping(local.get('assets', {}), 'assets')
    local['assets'] = {
        url: _file_path(file_path, path.parent, 'asset') for url, file_path in assets.items()
    }
    plugins = copy.deepcopy(_mapping(merged.get('plugins', {}), 'plugins'))
    unknown = set(plugins) - ALLOWED_PLUGINS
    if unknown:
        raise LocalInputError(f'ローカルで未対応のpluginです: {sorted(unknown)}')
    if 'line' not in plugins:
        raise LocalInputError('build用にplugins.lineを設定してください')
    for name, params in plugins.items():
        _mapping(params, f'plugins.{name}')
    for key in ('access_token', 'channel_secret', 'line_access_token', 'line_channel_secret'):
        plugins['line'].pop(key, None)
    for key in ('signing_key', 'scenario_uri'):
        plugins.get('webchat', {}).pop(key, None)
    frames = _mapping(plugins.get('line.image_text', {}).get('frames', {}), 'frames')
    for frame in frames.values():
        _mapping(frame, 'frame')
        if frame.get('font_path'):
            frame['font_path'] = _file_path(frame['font_path'], path.parent, 'font_path')
    options = copy.deepcopy(_mapping(merged.get('options', {}), 'options'))
    options.pop('api_token', None)
    scenario = copy.deepcopy(_mapping(bot.get('scenario'), 'scenario'))
    kind = scenario.get('type')
    bot_params = _mapping(scenario.get('params', {}), 'scenario.params')
    params = dict(options)
    if args.tsv:
        # 入力元だけを切り替え、表の解釈は引き継ぐ。元sourceの資格情報等は持ち越さない。
        params.update({key: value for key, value in plugins.get(kind, {}).items()
                       if key in SHARED_TABLE_PARAMS})
        params.update(plugins.get('tsv', {}))
        params.update({key: value for key, value in bot_params.items()
                       if key in SHARED_TABLE_PARAMS})
        params['manifest'] = str(Path(args.tsv).resolve())
        kind = 'tsv'
        scenario = {'type': kind}
    else:
        params.update(plugins.get(kind, {}))
        params.update(bot_params)
    if kind not in ('tsv', 'google_sheets'):
        raise LocalInputError('scenario.typeはtsvまたはgoogle_sheetsにしてください')
    if kind == 'tsv':
        params['manifest'] = _file_path(params.get('manifest'), path.parent, 'manifest')
    else:
        credential = params.get('key_file_json')
        if not isinstance(credential, str) or credential.lstrip().startswith(('{', '[')):
            raise LocalInputError('key_file_jsonにはJSON本文ではなくファイルpathを指定してください')
        params['key_file_json'] = _file_path(credential, path.parent, 'key_file_json')
        if not isinstance(params.get('sheet_id'), str) or not params['sheet_id']:
            raise LocalInputError('Google Sheetsのsheet_idを指定してください')
    scenario['params'] = params
    # loader設定は選択済みscenarioへ集約し、未使用の資格情報を持ち越さない。
    plugins.pop('google_sheets', None)
    plugins.pop('tsv', None)
    namespace = bot.get('state_namespace', args.bot)
    if not isinstance(namespace, str) or not namespace:
        raise LocalInputError('state_namespaceは空でない文字列にしてください')
    interfaces = []
    configured_interfaces = bot.get('interfaces', [])
    if not isinstance(configured_interfaces, list):
        raise LocalInputError('interfacesは配列にしてください')
    for interface in configured_interfaces:
        _mapping(interface, 'interface')
        if interface.get('type') not in ('line', 'webchat', 'liff'):
            continue
        interface = copy.deepcopy(interface)
        interface_params = _mapping(interface.get('params') or {}, 'interface.params')
        for key in ('access_token', 'channel_secret', 'line_access_token',
                    'line_channel_secret', 'signing_key', 'scenario_uri'):
            interface_params.pop(key, None)
        interface['params'] = interface_params
        interfaces.append(interface)
    config = {
        'cloud': {'provider': 'local'}, 'local': local,
        'auth': {}, 'options': options, 'plugins': plugins,
        'constants': copy.deepcopy(merged.get('constants', {})),
        'bots': {args.bot: {'scenario': scenario, 'state_namespace': namespace, 'interfaces': interfaces,
                            **{key: copy.deepcopy(bot[key]) for key in ('richmenus', 'default_richmenu') if key in bot}}},
    }
    if args.command == 'webchat':
        webchat_params = merge_params(plugins.get('webchat', {}), next((
            item['params'] for item in interfaces if item['type'] == 'webchat'), {}))
        apps = _mapping(webchat_params.get('liff_apps') or {}, 'liff_apps')
        for app in apps.values():
            name = _mapping(app, 'LIFFページ').get('bot')
            if not isinstance(name, str) or not name or name not in bots:
                raise LocalInputError('LIFF連携先のBotが設定にありません')
            if name in config['bots']:
                continue
            peer = _mapping(bots[name], 'LIFF連携先Bot')
            peer_interfaces = peer.get('interfaces', [])
            if not isinstance(peer_interfaces, list):
                raise LocalInputError('LIFF連携先のinterfacesは配列にしてください')
            selected_interfaces = []
            for item in peer_interfaces:
                _mapping(item, 'LIFF連携先interface')
                if item.get('type') not in ('webchat', 'liff'):
                    continue
                peer_params = copy.deepcopy(_mapping(item.get('params') or {}, 'interface.params'))
                for key in ('access_token', 'channel_secret', 'line_access_token', 'line_channel_secret', 'signing_key'):
                    peer_params.pop(key, None)
                selected_interfaces.append({'type': item['type'], 'params': peer_params})
            peer_namespace = peer.get('state_namespace', name)
            if not isinstance(peer_namespace, str) or not peer_namespace:
                raise LocalInputError('state_namespaceは空でない文字列にしてください')
            # 固定URIの実行設定だけを残し、BのSheets等の資格情報を取得しない。
            config['bots'][name] = {'state_namespace': peer_namespace, 'interfaces': selected_interfaces}
    return config, environment


def save_settings(path, config):
    import yaml
    write_bytes(path, yaml.safe_dump(
        {'*': config}, allow_unicode=True, sort_keys=False).encode('utf-8'))


def inspect_watch_inputs(args):
    """元の入力だけを列挙し、壊れた・欠落したmanifestも監視に残す。"""
    from plugin.scenario_table import SheetSelector
    from plugin.tsv_values import sync_baselines

    files = {str(Path(args.settings).resolve())}
    fonts = set()
    result = {}
    if args.tsv:
        files.add(str(Path(args.tsv).resolve()))
    try:
        config, environment = load_config(args)
        local = config['local']
        result['identity'] = (local['storage_root'], local['public_base_url'], args.bot)
        scenario = config['bots'][args.bot]['scenario']
        result['source_type'] = scenario['type']
        if scenario['type'] != 'tsv':
            raise LocalInputError('--watchはTSV入力用です。Sheetsは再実行で取得するか--tsvを指定してください')
        files.update(local.get('assets', {}).values())
        for frame in config['plugins'].get('line.image_text', {}).get('frames', {}).values():
            if frame.get('font_path'):
                fonts.add(frame['font_path'])
        manifest_path = Path(scenario['params']['manifest'])
        files.add(str(manifest_path))
        files.add(str(manifest_path.parent / 'sheets-sync.json'))
        manifest = read_json(manifest_path)
        require_keys(manifest, ('sheets',), label='TSV manifest')
        if not isinstance(manifest['sheets'], list):
            raise LocalInputError('TSV manifestのsheetsは配列にしてください')
        paths = {}
        for sheet in manifest['sheets']:
            require_keys(sheet, ('name', 'path'), label='TSVシート')
            name = sheet['name']
            if not isinstance(name, str) or not name or name in paths:
                raise LocalInputError('TSVシート名は空でない、重複のない文字列にしてください')
            paths[name] = _file_path(sheet['path'], manifest_path.parent, 'TSV')
        selected = SheetSelector(scenario['params']).select(paths, environment)
        if scenario['params'].get('evaluate_formula', False):
            # 選択外の補助シートも参照できる。本文は読まず、全候補の変更を監視する。
            files.update(paths.values())
        else:
            files.update(paths[name] for name, _logical, _constant in selected)
        # 同期基準は式評価の有無によらずセル型へ影響する。
        files.update(str(path) for path in sync_baselines(manifest_path, paths).values())
    except (OSError, ValueError, KeyError, TypeError, re.error) as error:
        result['error'] = str(error)
    result.update(files=sorted(files | fonts), fonts=sorted(fonts))
    return result


def freeze_assets(config, storage_root):
    from requests.utils import requote_uri
    files, provenance = {}, []
    for url, filename in config['local'].get('assets', {}).items():
        if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
            raise LocalInputError('assetのkeyは元のHTTP/HTTPS URLにしてください')
        key = requote_uri(url)
        if key in files:
            raise LocalInputError(f'asset URLが正規化後に重複しています: {key}')
        original = Path(filename)
        data = original.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        target = storage_root / 'inputs' / 'assets' / (digest + original.suffix.lower())
        if not target.exists():
            # 内容hashで固定した入力はrunを跨いで共用する。
            write_bytes(target, data)
        files[key] = str(target)
        provenance.append({'url': url, 'path': str(original), 'sha256': digest})
    config['local']['assets'] = files
    return provenance


def render_input_signature(config):
    """内容keyに含まれないfont・rendererの変更だけを検出する。"""
    if 'line.image_text' not in config['plugins'] and not config['local'].get('assets'):
        return None
    import PIL

    paths = {PROJECT_ROOT / 'convert_image.py', PROJECT_ROOT / 'plugin/render_text/renderer.py'}
    if 'line.image_text' in config['plugins']:
        frames = config['plugins']['line.image_text'].get('frames') or {'default': {}}
        for frame in frames.values():
            default = 'ipaexg_tate.ttf' if frame.get('is_vertical', False) else 'ipaexg.ttf'
            paths.add(Path(frame.get('font_path') or PROJECT_ROOT / 'plugin/render_text/font' / default))
    digest = hashlib.sha256(PIL.__version__.encode('utf-8'))
    for path in sorted(paths):
        digest.update(str(path).encode('utf-8'))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _deny_network():
    def deny(*args, **kwargs):
        raise RuntimeError('このローカル処理では外部通信を許可していません')
    # 短命workerの誤接続検出。任意Pythonを隔離するsandboxではない。
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.create_connection = deny


def _prepare_source(request):
    import settings
    scenario = settings.BOTS[request['bot']]['scenario']
    if scenario['type'] == 'tsv':
        from plugin.tsv import TsvPlugin_Loader
        _deny_network()
        loader = TsvPlugin_Loader(scenario['params'])
        source = {'type': 'tsv', 'path': scenario['params']['manifest']}
    else:
        from plugin.google_sheets import GoogleSheetPlugin_Loader
        loader = GoogleSheetPlugin_Loader(scenario['params'])
        source = {'type': 'google_sheets', 'sheet_id': scenario['params']['sheet_id']}
    tables, constants = loader.load_scenario()
    encoded = pickle.dumps((tables, constants), protocol=4)
    write_bytes(request['source_path'], encoded)
    source['sha256'] = hashlib.sha256(encoded).hexdigest()
    return {'ok': True, 'source': source}


class FrozenLoader:
    def __init__(self, path):
        self.path = path

    def load_scenario(self):
        # 親がこのrun用に生成した非公開snapshotだけを読む。
        with open(self.path, 'rb') as stream:
            return pickle.load(stream)


def _build(request):
    import settings
    import commands
    import common_commands
    import hub
    import plugin
    from cloud_backend import create_object_store, create_state_store
    from runtime import BotRuntime
    from tools.local_line import LocalLineInterface

    if not settings.BACKEND_SETTINGS.get('allow_external_media', False):
        _deny_network()
    random.seed(request.get('case', {}).get('seed', 0))
    hub.clear()
    commands.clear()
    common_commands.setup(settings.OPTIONS)
    plugin.load_plugins(settings.OPTIONS, {
        name: params for name, params in settings.PLUGINS.items()
        if name not in ('google_sheets', 'tsv', 'webchat')
    })
    bot_settings = settings.BOTS[request['bot']]
    interface_params = next((item.get('params', {}) for item in bot_settings['interfaces']
                             if item['type'] == 'line'), {})
    interface = LocalLineInterface(request['bot'], {
        **settings.OPTIONS, **settings.PLUGINS['line'], **interface_params,
    })
    namespace = bot_settings.get('state_namespace', request['bot'])
    objects = create_object_store()
    state = create_state_store()
    scenario_uri = request.get('scenario_uri')
    asset_urls = request.get('asset_urls', {})
    signature_path = objects.storage_root / 'metadata' / 'render-inputs.json'
    signature = request.get('render_signature')
    if scenario_uri is None:
        force = False
        if signature is not None:
            try:
                previous = read_json(signature_path).get('signature')
            except (OSError, ValueError, AttributeError):
                previous = None
            force = force or previous != signature
            if force:
                # 途中失敗後にfontを戻しても、不完全な描画cacheを再利用しない。
                signature_path.unlink(missing_ok=True)
        asset_urls = {}
        for url, filename in settings.BACKEND_SETTINGS.get('assets', {}).items():
            path = Path(filename)
            content = path.read_bytes()
            mime = mimetypes.guess_type(path.name)[0]
            if mime is None or not mime.startswith(('image/', 'audio/', 'video/')):
                raise LocalInputError(f'assetには媒体の拡張子が必要です: {path.name}')
            key = f'assets/{hashlib.sha256(content).hexdigest()}{path.suffix}'
            asset_urls[url] = objects.store_public(key, content, mime)
        builder = BotRuntime(request['bot'], {'line': interface},
                             FrozenLoader(request['source_path']), state_namespace=namespace)
        ok, error = builder.build_scenario(
            task_id=request['run_id'],
            options={
                'local_media_files': settings.BACKEND_SETTINGS.get('assets', {}),
                'allow_external_media': settings.BACKEND_SETTINGS.get('allow_external_media', False),
                'force': force,
            },
            version=settings.OPTIONS.get('scenario_version', 1),
        )
        if not ok:
            return {'ok': False, 'phase': 'build', 'error': error}
        scenario_uri = builder.scenario_uri
    bot = BotRuntime(request['bot'], {'line': interface}, None, state_namespace=namespace)
    from scenario import Scenario
    try:
        # 共有cacheの最新pointerではなく、このrunで完成した固定URIを読む。
        bot.scenario = Scenario.load_from_uri(scenario_uri)
        bot.scenario_uri = scenario_uri
    except Exception as error:
        return {'ok': False, 'phase': 'load', 'error': str(error)}
    if request.get('scenario_uri') is None and signature is not None:
        write_json(signature_path, {'signature': signature})
    result = {'ok': True, 'scenario_uri': bot.scenario_uri, 'asset_urls': asset_urls,
              'storage_root': str(state.storage_root), 'object_storage_root': str(objects.storage_root)}
    if 'case' in request:
        from tools.local_line import run_case
        result['case'] = run_case(bot, interface, state, request['case'],
                                  state.storage_root, request['input_hash'])
        result['ok'] = result['case']['passed']
        result['artifacts'], result['unresolved_media'] = collect_artifacts(
            result['case']['steps'], objects, asset_urls)
    return result


def collect_artifacts(steps, store, asset_urls):
    from urllib.parse import unquote
    from requests.utils import requote_uri
    from tools.local_line import messages_from_records
    urls = set()
    for step in steps:
        for message in messages_from_records(step.get('payloads', [])):
            for key in ('originalContentUrl', 'previewImageUrl'):
                if message.get(key):
                    urls.add(message[key])
            if message.get('baseUrl'):
                urls.add(message['baseUrl'].rstrip('/') + '/1040')
            icon = message.get('sender', {}).get('iconUrl')
            if icon:
                urls.add(icon)
            template = message.get('template', {})
            for item in [template, *template.get('columns', [])]:
                if item.get('thumbnailImageUrl'):
                    urls.add(item['thumbnailImageUrl'])
    artifacts, unresolved = [], []
    prefix = store.public_base_url.rstrip('/') + '/' + store.store_id + '/'
    for url in sorted(urls):
        resolved = asset_urls.get(requote_uri(url), url)
        if not resolved.startswith(prefix):
            unresolved.append({'url': url, 'reason': 'ローカル媒体として未解決'})
            continue
        try:
            path, mime = store.public_file(unquote(resolved[len(prefix):]))
            item = {'url': url, 'path': str(path), 'content_type': mime}
            if mime.startswith('image/'):
                from PIL import Image
                with Image.open(path) as picture:
                    item.update(format=picture.format, width=picture.width, height=picture.height)
            artifacts.append(item)
        except Exception as error:
            unresolved.append({'url': url, 'reason': str(error)})
    return artifacts, unresolved


def worker(request_path):
    request = read_json(request_path)
    code = 1
    try:
        if os.environ.get('XSBOT_CLOUD_PROVIDER') != 'local':
            raise LocalInputError('ローカルworkerはprovider=localで起動してください')
        with contextlib.redirect_stdout(sys.stderr):
            logging.basicConfig(level=logging.INFO)
            result = _prepare_source(request) if request['phase'] == 'source' else _build(request)
        code = 0 if result['ok'] else 1
    except Exception as error:
        logging.exception('ローカル処理が失敗しました')
        code = 2 if isinstance(error, LocalInputError) else 1
        result = {'ok': False, 'phase': request['phase'], 'error_type': type(error).__name__, 'error': str(error)}
    result['exit_code'] = code
    write_json(request['result_path'], result)
    return code


def run_worker(config, environment, request, directory, timeout):
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / 'settings.yaml'
    save_settings(config_path, config)
    request_path = directory / 'request.json'
    result_path = directory / 'result.json'
    request = {**request, 'result_path': str(result_path)}
    write_json(request_path, request)
    env = {**os.environ, 'XSBOT_CLOUD_PROVIDER': 'local',
           'XSBOT_DEPLOY_ENV': environment, 'XSBOT_SETTINGS_FILE': str(config_path)}
    try:
        completed = run_process(
            [sys.executable, str(Path(__file__).resolve()), '_worker', str(request_path)],
            cwd=PROJECT_ROOT, env=env, timeout=timeout, stdout=sys.stderr,
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'exit_code': 1, 'phase': request['phase'], 'error': '子processがtimeoutしました'}
    if not result_path.exists():
        return {'ok': False, 'exit_code': 2, 'phase': request['phase'],
                'error': f'子processが結果を返さず終了しました: {completed.returncode}'}
    result = read_json(result_path)
    if completed.returncode and result.get('ok'):
        return {'ok': False, 'exit_code': 2, 'error': '子processの終了と結果が一致しません'}
    return result


def execute(args, serve=True, expected_identity=None):
    if args.command == 'webchat' and getattr(args, 'watch', False) and serve:
        from tools.local_watch import run_watch

        def rebuild(identity=None):
            result = execute(args, serve=False, expected_identity=identity)
            return {'result': result, 'settings_path': result.get('webchat_settings'),
                    'environment': result.get('environment', '')}

        return run_watch(args, rebuild, lambda: inspect_watch_inputs(args))
    config, environment = load_config(args)
    if expected_identity is not None:
        identity = (config['local']['storage_root'], config['local']['public_base_url'], args.bot)
        if tuple(expected_identity) != identity:
            raise LocalInputError('--watch中はstorage_root・public_base_url・Botを変更できません')
        if config['bots'][args.bot]['scenario']['type'] != 'tsv':
            raise LocalInputError('--watch中にSheetsへ切り替えることはできません')
    cases = None
    if args.command == 'verify':
        cases = validate_suite(read_json(args.suite), args.case)
        if args.session and len(cases) != 1:
            raise LocalInputError('--sessionでは--caseで1ケースを選んでください')
    from cloud_backend.local.object_store import LocalObjectStore
    local = config['local']
    cache_root = Path(local['storage_root']) / 'verify-cache' / args.bot
    if args.command == 'verify' and args.session:
        if Path(args.session).resolve().is_relative_to(cache_root.parent):
            raise LocalInputError('verify-cacheはセーブの再開先に指定できません')
    # 配信基点の変更等を、入力取得やbuildより先に検出する。
    LocalObjectStore(local['storage_root'], local.get('public_base_url', ''))
    runs = Path(local['storage_root']) / 'runs'
    runs.mkdir(parents=True, exist_ok=True)
    run_directory = Path(tempfile.mkdtemp(prefix='run-', dir=runs))
    input_root = Path(local['storage_root'])
    if args.command == 'verify':
        input_root = Path(args.session).resolve() if args.session else cache_root
    assets = freeze_assets(config, input_root)
    render_signature = render_input_signature(config)
    source_path = run_directory / 'inputs' / 'source.pickle'
    request = {'phase': 'source', 'bot': args.bot, 'source_path': str(source_path),
               'run_id': run_directory.name}
    source_result = run_worker(config, environment, request, run_directory / 'source', args.timeout)
    if not source_result['ok']:
        return source_result
    source = source_result['source']
    input_hash = hashlib.sha256(json.dumps({
        'source': source, 'assets': assets, 'options': config['options'],
        'constants': config['constants'], 'plugins': config['plugins'],
        'bot': config['bots'][args.bot], 'environment': environment,
        'render_signature': render_signature,
    }, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    request.update(phase='build', input_hash=input_hash, render_signature=render_signature)
    result = {'schema_version': 1, 'command': args.command, 'bot': args.bot,
              'environment': environment, 'run_directory': str(run_directory),
              'source': source, 'assets': assets, 'input_hash': input_hash,
              'allow_external_media': local.get('allow_external_media', False)}
    if args.command == 'verify':
        results = []
        if not args.session:
            build_config = copy.deepcopy(config)
            build_config['local']['storage_root'] = str(cache_root)
            built = run_worker(build_config, environment, request,
                               run_directory / 'build', args.timeout)
            if not built['ok']:
                result.update(built, cases=[])
                write_json(run_directory / 'result.json', result)
                return result
            request.update(scenario_uri=built['scenario_uri'], asset_urls=built.get('asset_urls', {}))
            result['cache_root'] = str(cache_root)
        for index, case in enumerate(cases):
            case_config = copy.deepcopy(config)
            root = Path(args.session).resolve() if args.session else run_directory / 'cases' / str(index)
            case_config['local']['storage_root'] = str(root if args.session else cache_root)
            if not args.session:
                # 媒体を再複製せず、Player/NextLabel/sessionの保存だけをcaseへ分ける。
                case_config['local']['state_storage_root'] = str(root)
            case_result = run_worker(
                case_config, environment, {**request, 'case': case},
                run_directory / 'workers' / str(index), args.timeout)
            case_result.update(name=case['name'], storage_root=str(root),
                               database=str(root / 'state.sqlite3'), continued=bool(args.session))
            results.append(case_result)
        result.update(ok=all(item['ok'] for item in results), cases=results,
                      exit_code=max(item.get('exit_code', 1) for item in results))
    else:
        built = run_worker(config, environment, request, run_directory / 'build', args.timeout)
        result.update(built)
        if args.command == 'webchat' and result['ok']:
            from tools.local_webchat import prepare_settings
            serve_config = prepare_settings(config, args.bot, built['scenario_uri'], built['asset_urls'])
            serve_path = run_directory / 'webchat-settings.yaml'
            save_settings(serve_path, serve_config)
            result['webchat_settings'] = str(serve_path)
            write_json(run_directory / 'result.json', result)
            if not serve:
                return result
            env = {**os.environ, 'XSBOT_CLOUD_PROVIDER': 'local', 'XSBOT_DEPLOY_ENV': environment,
                   'XSBOT_SETTINGS_FILE': str(serve_path)}
            try:
                completed = run_process(
                    [sys.executable, '-m', 'tools.local_webchat'], cwd=PROJECT_ROOT,
                    env=env, stdout=sys.stderr)
                result['exit_code'] = completed.returncode
                result['ok'] = completed.returncode == 0
                if not result['ok']:
                    result.update(phase='serve', error='Webchatの起動または実行に失敗しました')
            except KeyboardInterrupt:
                result.update(ok=True, exit_code=0)
    write_json(run_directory / 'result.json', result)
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == '_worker':
        return worker(argv[1])
    command = argv[0] if argv else None
    try:
        args = parse_args(argv)
        with stop_on_sigterm(), contextlib.redirect_stdout(sys.stderr):
            result = execute(args)
    except KeyboardInterrupt:
        result = {'ok': False, 'exit_code': 130, 'error': '処理を中断しました'}
    except (LocalInputError, OSError, ValueError) as error:
        result = {'ok': False, 'exit_code': 2, 'error_type': type(error).__name__, 'error': str(error)}
    except Exception as error:
        logging.exception('ローカルツールが失敗しました')
        result = {'ok': False, 'exit_code': 2, 'error_type': type(error).__name__, 'error': str(error)}
    if command != 'webchat':
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    elif not result['ok']:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), file=sys.stderr)
    return result.get('exit_code', 0 if result['ok'] else 1)


if __name__ == '__main__':
    raise SystemExit(main())
