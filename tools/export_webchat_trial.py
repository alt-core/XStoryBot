#!/usr/bin/env python3
"""限定シナリオを、会話APIなしで遊べるWebchatへ書き出す。"""

import contextlib
import hashlib
import ipaddress
import json
import logging
import math
import mimetypes
import os
from pathlib import Path
import pickle
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.export_webchat import ASSET_SOURCES, Parser, _page, _replace_once, write_site, add_page_arguments, read_theme
from tools.local_support import LocalInputError, read_json, write_json
from tools.local_process import stop_on_sigterm


MARKER = 'data-webchat-trial="1"'
BUILD_MEDIA_URL = 'http://127.0.0.1:1/local-media'
EXTRA_ASSETS = {'trial.js': 'webchat-client/trial.js', 'trial-runtime.js': 'webchat-client/trial-runtime.js'}


def interface_params(config, bot, kind):
    import utility
    base = config['plugins'].get(kind, {})
    own = next((item.get('params', {}) for item in config['bots'][bot].get('interfaces', []) if item['type'] == kind), {})
    result = {**config['options'], **base, **own}
    if kind == 'webchat':
        result['constants'] = {
            **utility.normalize_constants(base.get('constants', {}), scalar_only=True),
            **utility.normalize_constants(own.get('constants', {}), scalar_only=True),
        }
    return result


def configuration_warnings(config, bot):
    """設定の共用は許容し、API側への影響とepochの継承を知らせる。"""
    from plugin.webchat.interface import _as_bool
    params = interface_params(config, bot, 'webchat')
    own = next((item.get('params', {}) for item in config['bots'][bot].get('interfaces', [])
                if item['type'] == 'webchat'), {})
    location = f'bots.{bot}.interfaces[type=webchat].params'
    warnings = []
    if _as_bool(params.get('enabled')):
        warnings.append({'code': 'api-webchat-enabled', 'location': location + '.enabled',
                         'message': 'このBotはAPI Webchatでも有効です。同じ設定をサーバーで使うと、'
                         '設定次第で公開または起動失敗につながります。体験版専用の設定ファイルを使うか、'
                         '体験版専用Botではenabled: falseを明示してください。'})
    if params.get('scenario_compatibility_epoch') and 'scenario_compatibility_epoch' not in own:
        warnings.append({'code': 'inherited-compatibility-epoch',
                         'location': location + '.scenario_compatibility_epoch',
                         'message': '互換epochを共通設定から継承しています。共通値の変更で体験版の再開も'
                         '影響を受けます。独立して更新する場合はBot側へ明示してください。'})
    return warnings


def local_url_warnings(program):
    """公開データのURL欄だけを確認する。台詞や正規表現の文字列は走査しない。"""
    url_fields = {'url', 'href', 'uri', 'image_url', 'icon_url', 'original_url', 'preview_url', 'poster_url'}
    warnings = []

    def visit(value, location):
        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f'{location}[{index}]')
        elif isinstance(value, dict):
            for key, child in value.items():
                path = f'{location}.{key}'
                if key in url_fields and isinstance(child, str):
                    host = (urlsplit(child).hostname or '').rstrip('.').lower()
                    loopback = host == 'localhost'
                    try:
                        loopback = loopback or ipaddress.ip_address(host).is_loopback
                    except ValueError:
                        pass
                    if loopback:
                        warnings.append({'code': 'local-url', 'location': path,
                                         'message': 'ローカル確認用URLが配布物に残っています。'
                                         '公開前に配信先のURLへ変更してください。'})
                visit(child, path)

    visit(program, 'scenario')
    return warnings


class MediaFiles:
    """既存StoreのURLを確認し、参照された媒体だけを公開用に集める。"""

    def __init__(self, store, assets, origins):
        from plugin.webchat.interface import _normalize_origins
        self.store = store
        self.prefix = f'{store.public_base_url}/{store.store_id}/'
        self.assets = assets
        self.origins = _normalize_origins(origins or [], allow_empty=True)
        self.files = {}

    def validate(self, value):
        import requests
        from plugin.webchat.interface import _format_origin
        value = self.assets.get(requests.utils.requote_uri(str(value)), str(value))
        parsed = urlsplit(value)
        if value.startswith(self.prefix):
            if parsed.query or parsed.fragment:
                raise ValueError('内部媒体URLのquery／fragmentは使えません')
            self.store.public_file(unquote(value[len(self.prefix):]))
            return value
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or '\\' in value or any(c.isspace() for c in value)):
            raise ValueError('公開媒体にはHTTPS URLかlocal.assetsを指定してください')
        if self.origins and _format_origin(parsed) not in self.origins:
            raise ValueError('媒体URLのoriginが許可されていません')
        return value

    def package(self, value):
        value = self.validate(value)
        if not value.startswith(self.prefix):
            return value
        path, content_type = self.store.public_file(unquote(value[len(self.prefix):]))
        if not content_type.startswith(('image/', 'video/', 'audio/')):
            raise ValueError('画像・音声・動画だけを公開できます')
        data = path.read_bytes()
        suffix = mimetypes.guess_extension(content_type) or path.suffix
        name = f'media/{hashlib.sha256(data).hexdigest()}{suffix}'
        self.files[name] = str(path)
        # JSONはassets/<revision>/にある。
        return '../../' + name

    def rewrite(self, value):
        media_keys = {'image_url', 'icon_url', 'original_url', 'preview_url', 'poster_url'}
        if isinstance(value, list):
            return [self.rewrite(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, child in value.items():
            is_media = key in media_keys or (key == 'url' and ('width' in value or value.get('type') in ('audio', 'video')))
            result[key] = self.package(child) if is_media and child else self.rewrite(child)
        return result


def build_program(request):
    import settings
    import commands
    import common_commands
    import hub
    import plugin
    import utility
    from cloud_backend import create_object_store
    from plugin.webchat.interface import normalize_liff_apps
    from richmenu_spec import menu_definitions, parse_bot_settings
    from tools.local_scenario import _deny_network
    from tools.trial_scenario import TrialBuildError, build_tables, compile_scenario

    config = request['config']
    bot = request['bot']
    if 'line.quick_reply_v2' in settings.PLUGINS:
        raise TrialBuildError('体験版ではline.quick_replyを指定してください。quick_reply_v2は対象外です')
    if settings.OPTIONS.get('scenario_version', 1) != 3:
        raise TrialBuildError('体験版はscenario_version: 3に対応しています')
    if not settings.BACKEND_SETTINGS.get('allow_external_media', False):
        _deny_network()
    hub.clear()
    commands.clear()
    common_commands.setup(settings.OPTIONS)
    build_plugins = {name: dict(values) for name, values in settings.PLUGINS.items()
                     if name not in ('webchat', 'liff', 'google_sheets', 'tsv')}
    build_plugins['line'].setdefault('alt_text', '選択可能な画像')
    plugin.load_plugins(settings.OPTIONS, build_plugins)
    with Path(request['source_path']).open('rb') as source:
        tables, constants = pickle.load(source)
    store = create_object_store()
    signature_path = store.storage_root / 'metadata' / 'trial-render-inputs.json'
    previous = read_json(signature_path).get('signature') if signature_path.exists() else None
    force = request['render_signature'] != previous
    if force:
        signature_path.unlink(missing_ok=True)
    asset_urls = {}
    for url, filename in settings.BACKEND_SETTINGS.get('assets', {}).items():
        path = Path(filename)
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0]
        if mime is None or not mime.startswith(('image/', 'video/', 'audio/')):
            raise LocalInputError('local.assetsには画像・音声・動画を指定してください')
        key = f'assets/{hashlib.sha256(data).hexdigest()}{path.suffix}'
        asset_urls[url] = store.store_public(key, data, mime)
    scenario = build_tables(tables, constants, {
        'local_media_files': settings.BACKEND_SETTINGS.get('assets', {}),
        'allow_external_media': settings.BACKEND_SETTINGS.get('allow_external_media', False),
        'force': force,
    })
    params = interface_params(config, bot, 'webchat')
    params['liff_apps'] = normalize_liff_apps(params.get('liff_apps'), allow_local_http=True)
    liff = None
    if params['liff_apps']:
        if any(app['bot'] != bot for app in params['liff_apps'].values()):
            raise TrialBuildError('体験版のLIFF連携先は同じBotにしてください')
        if not any(item['type'] == 'liff' for item in config['bots'][bot].get('interfaces', [])):
            raise TrialBuildError('同じBotにliff interfaceを設定してください')
        values = interface_params(config, bot, 'liff')
        prefix, ignore = values.get('action_prefix', '##liff.'), values.get('ignore_unhandled_action', False)
        if not isinstance(prefix, str) or type(ignore) is not bool:
            raise TrialBuildError('LIFFのaction_prefixまたはignore_unhandled_actionが不正です')
        liff = {'action_prefix': prefix, 'ignore_unhandled_action': ignore}
    media = MediaFiles(store, asset_urls, params.get('media_origins'))
    bot_settings = config['bots'][bot]
    definitions = menu_definitions(bot_settings)
    default = bot_settings.get('default_richmenu')
    menu, menu_actions = None, []
    if default:
        default = utility.normalize_name(default)
        selected = parse_bot_settings({'default_richmenu': default, 'richmenus': {default: definitions[default]}},
                                      {**settings.CONSTANTS, **params['constants']}, allow_local_http=True)
        definition = selected.menus[default]
        menu = definition.webchat_spec(default, media.validate(definition.data['image']))
        menu_actions = [area['action'] for area in definition.data['areas']]
    quick = {**settings.OPTIONS, **settings.PLUGINS.get('line.quick_reply', {})}
    program = compile_scenario(scenario, bot=bot, params=params, constants=settings.CONSTANTS,
                               quick_reply=quick, media_url=media.validate,
                               richmenu=menu, menu_actions=menu_actions, liff=liff)
    program = media.rewrite(program)
    if BUILD_MEDIA_URL in json.dumps(program, ensure_ascii=False):
        raise TrialBuildError('出力データに未変換の内部媒体URLがあります')
    write_json(request['program_path'], program)
    write_json(signature_path, {'signature': request['render_signature']})
    return {'ok': True, 'media_files': media.files, 'sheets': [name for name, _rows in tables]}


def worker(path):
    request = read_json(path)
    try:
        if os.environ.get('XSBOT_CLOUD_PROVIDER') != 'local':
            raise LocalInputError('書出しworkerはlocal providerで実行してください')
        with contextlib.redirect_stdout(sys.stderr):
            logging.basicConfig(level=logging.INFO)
            result = build_program(request)
    except Exception as error:
        from tools.trial_scenario import TrialBuildError
        result = {'ok': False, 'exit_code': 1 if isinstance(error, TrialBuildError) else 2,
                  'error_type': type(error).__name__, 'error': str(error)}
        logging.error('体験版の書出しに失敗しました: %s', error)
    write_json(request['result_path'], result)
    return result.get('exit_code', 0)


def package_site(program, media_files, output, *, title='Webchat', storage='indexeddb', theme_assets=None):
    sources = {**ASSET_SOURCES, **EXTRA_ASSETS}
    assets = {name: (PROJECT_ROOT / path).read_bytes() for name, path in sources.items()}
    assets.update(theme_assets or {})
    script = assets['app.js'].decode('utf-8')
    script = _replace_once(script,
        "import { createWebchatClient, WebchatClientError } from '../../webchat-client/index.js';",
        "import { WebchatClientError } from './client.js';\nimport { createTrialClient } from './trial.js';")
    script = _replace_once(script, "'../../webchat-client/liff-host.js'", "'./liff-host.js'")
    script = _replace_once(script, "'./ui_logic.mjs'", "'./ui_logic.js'")
    script = _replace_once(script, 'const client = createWebchatClient({ apiBaseUrl, bot, storage });',
                           "const client = createTrialClient({ bot, programUrl: new URL('./scenario.json', import.meta.url), storage });")
    script = _replace_once(script, '`xstorybot-webchat-richmenu:${apiBaseUrl}|${bot}`', '`xstorybot-webchat-richmenu:trial:${bot}`')
    assets['app.js'] = script.encode('utf-8')
    for name in EXTRA_ASSETS:
        assets[name] = _replace_once(assets[name].decode('utf-8'), "'./index.js'", "'./client.js'").encode('utf-8')
    assets['scenario.json'] = json.dumps(program, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    digest = hashlib.sha256()
    for name, data in sorted(assets.items()):
        digest.update(name.encode() + b'\0' + data + b'\0')
    revision = digest.hexdigest()[:16]
    files = {f'assets/{revision}/{name}': data for name, data in assets.items()}
    files.update({name: Path(path).read_bytes() for name, path in media_files.items()})
    files['LICENSE'] = (PROJECT_ROOT / 'LICENSE').read_bytes()
    frame_origins = sorted({f"{urlsplit(app['url']).scheme}://{urlsplit(app['url']).netloc}"
                            for app in program['liff_apps'].values() if app['url'].startswith('http:')})
    files['index.html'] = _page((PROJECT_ROOT / 'static/webchat/index.html').read_text(), '', program['bot'], revision, '',
                              marker=MARKER, frame_origins=frame_origins,
                              title=title, storage=storage, themed=bool(theme_assets))
    entry = write_site(output, files, marker=MARKER,
                       inputs={(PROJECT_ROOT / path).resolve() for path in sources.values()} | {(PROJECT_ROOT / 'static/webchat/index.html').resolve(), (PROJECT_ROOT / 'LICENSE').resolve()})
    return {'ok': True, 'output': str(Path(output).resolve()), 'entry': entry,
            'bot': program['bot'], 'revision': revision, 'files': sorted(files), 'title': title, 'storage': storage}


def export_trial(args):
    from tools.local_scenario import load_config, freeze_assets, render_input_signature, run_worker
    settings_path = Path(args.settings).resolve()
    cache = settings_path.parent / 'outputs' / '.trial-cache' / args.bot
    output = Path(args.output).resolve()
    theme_assets = read_theme(args.theme, output)
    if output.is_relative_to(cache) or cache.is_relative_to(output):
        raise LocalInputError('公開先とビルドcacheは別のディレクトリにしてください')
    local_args = SimpleNamespace(**vars(args), command='build')
    config, environment = load_config(local_args, local_override={
        'storage_root': str(cache), 'public_base_url': BUILD_MEDIA_URL,
    })
    warnings = configuration_warnings(config, args.bot)
    config['local'].setdefault('allow_external_media', True)
    freeze_assets(config, cache)
    signature = render_input_signature(config)
    with tempfile.TemporaryDirectory(prefix='xsbot-trial-') as temporary:
        directory = Path(temporary)
        source_path, program_path = directory / 'source.pickle', directory / 'program.json'
        request = {'bot': args.bot, 'source_path': str(source_path), 'phase': 'source'}
        source = run_worker(config, environment, request, directory / 'source', args.timeout)
        if not source['ok']:
            return {**source, 'warnings': warnings}
        request.update(phase='trial', config=config, program_path=str(program_path), render_signature=signature)
        built = run_worker(config, environment, request, directory / 'build', args.timeout, worker_script=Path(__file__).resolve())
        if not built['ok']:
            return {**built, 'warnings': warnings}
        program = read_json(program_path)
        warnings.extend(local_url_warnings(program))
        result = package_site(program, built['media_files'], output,
                              title=args.title, storage=args.storage, theme_assets=theme_assets)
        result['warnings'] = warnings
        result['sheets'] = source.get('source', {}).get('sheets', built['sheets'])
        return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == '_worker':
        return worker(argv[1])
    parser = Parser(description=__doc__)
    parser.add_argument('--settings', default='settings.yaml')
    parser.add_argument('--bot', required=True)
    parser.add_argument('--tsv')
    parser.add_argument('--output', required=True)
    parser.add_argument('--timeout', type=float, default=600)
    add_page_arguments(parser)
    try:
        args = parser.parse_args(argv)
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise LocalInputError('--timeoutは正の有限数にしてください')
        with stop_on_sigterm(), contextlib.redirect_stdout(sys.stderr):
            result = export_trial(args)
    except KeyboardInterrupt:
        result = {'ok': False, 'exit_code': 130, 'error': '処理を中断しました'}
    except (ValueError, OSError) as error:
        result = {'ok': False, 'exit_code': 2, 'error': str(error)}
    for warning in result.get('warnings', []):
        print(f"警告 [{warning['code']}] {warning['location']}: {warning['message']}", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return result.get('exit_code', 0 if result['ok'] else 1)


if __name__ == '__main__':
    raise SystemExit(main())
