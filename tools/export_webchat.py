#!/usr/bin/env python3
"""既存Webchat UIを、任意のサブパスへ置ける静的ファイル一式にする。"""

import argparse
import hashlib
from html import escape
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPORT_MARKER = 'data-webchat-export="1"'
ASSET_SOURCES = {
    'app.js': 'static/webchat/app.js',
    'style.css': 'static/webchat/style.css',
    'ui_logic.js': 'static/webchat/ui_logic.mjs',
    'client.js': 'webchat-client/index.js',
    'liff-host.js': 'webchat-client/liff-host.js',
}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _api_url(value):
    value = value.strip().rstrip('/')
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('API URLのportが不正です') from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or '?' in value or '#' in value or '\\' in value
            or any(character.isspace() or ord(character) < 32 for character in value)
            or any(character in parsed.netloc for character in "\"'<>;")):
        raise ValueError('API URLには認証情報・query・fragmentのないHTTP/HTTPS URLを指定してください')
    host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
    origin = f'{parsed.scheme}://{host}' + (f':{port}' if port is not None else '')
    return value, origin


def _replace_once(source, old, new):
    # ソース構造が変わった場合、壊れた配布物を生成せず対応箇所を示す。
    if source.count(old) != 1:
        raise ValueError(f'Webchatソースのexport対象を確認してください: {old}')
    return source.replace(old, new, 1)


def _title_argument(value):
    if not value.strip():
        raise argparse.ArgumentTypeError('題名には空でない文字列を指定してください')
    return value


def add_page_arguments(parser):
    parser.add_argument('--title', default='Webchat', type=_title_argument, help='画面とブラウザのタブに表示する題名')
    parser.add_argument('--storage', choices=('indexeddb', 'memory'), default='indexeddb', help='進行の保存方式')
    parser.add_argument('--theme', help='style.cssと公開する素材を入れたフォルダ')


def read_theme(theme, output):
    """明示された公開用フォルダだけを、相対参照を保って同梱する。"""
    if theme is None:
        return {}
    root, output = Path(theme).resolve(), Path(output).resolve()
    if not root.is_dir() or not (root / 'style.css').is_file():
        raise ValueError('テーマフォルダの直下にstyle.cssを置いてください')
    if root.is_relative_to(output) or output.is_relative_to(root):
        raise ValueError('テーマと出力先は互いに含まれない別のフォルダにしてください')
    def scan_error(error):
        raise error
    assets = {}
    for directory, folders, filenames in os.walk(root, onerror=scan_error):
        folders[:] = sorted(name for name in folders if not name.startswith('.'))
        filenames = sorted(name for name in filenames if not name.startswith('.'))
        for name in folders + filenames:
            if (Path(directory) / name).is_symlink():
                raise ValueError('テーマ内のシンボリックリンクは同梱できません')
        for name in filenames:
            path = Path(directory) / name
            if not path.is_file():
                raise ValueError('テーマには通常のファイルとフォルダを置いてください')
            assets['theme/' + path.relative_to(root).as_posix()] = path.read_bytes()
    return assets


def _page(source, api_base_url, bot, revision, origin, marker=EXPORT_MARKER, frame_origins=(),
          *, title='Webchat', storage='indexeddb', themed=False):
    if not isinstance(title, str) or not title.strip():
        raise ValueError('題名には空でない文字列を指定してください')
    if storage not in ('indexeddb', 'memory'):
        raise ValueError('storageはindexeddbかmemoryを指定してください')
    policy = (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        f"connect-src 'self' {origin}; img-src 'self' https: {origin}; "
        f"media-src 'self' https: {origin}; frame-src 'self' https: {origin} {' '.join(frame_origins)}"
    )
    replacements = (
        ('data-webchat-api-base-url=""',
         f'data-webchat-api-base-url="{escape(api_base_url, quote=True)}" {marker}'),
        ('data-webchat-bot=""', f'data-webchat-bot="{escape(bot, quote=True)}"'),
        ('data-webchat-storage="indexeddb"', f'data-webchat-storage="{storage}"'),
        ('<title>Webchat</title>', f'<title>{escape(title)}</title>'),
        ('<h1 id="chat-title">Webchat</h1>', f'<h1 id="chat-title">{escape(title)}</h1>'),
        ('<!-- WEBCHAT_EXPORT_META -->',
         f'<meta http-equiv="Content-Security-Policy" content="{escape(policy, quote=True)}">\n'
         '  <meta name="referrer" content="no-referrer">'),
        ('<link rel="stylesheet" href="/static/webchat/style.css">',
         f'<link rel="stylesheet" href="./assets/{revision}/style.css">'
         + (f'\n  <link rel="stylesheet" href="./assets/{revision}/theme/style.css">' if themed else '')),
        ('src="/static/webchat/app.js"', f'src="./assets/{revision}/app.js"'),
    )
    for old, new in replacements:
        source = _replace_once(source, old, new)
    return source.encode('utf-8')


def _write_public(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
        temporary = Path(target.name)
        try:
            target.write(data)
            target.close()
            temporary.chmod(0o644)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def write_site(output, files, *, inputs=(), marker=EXPORT_MARKER):
    """所有印と出力先を確認し、公開入口を最後に差し替える。"""
    output = Path(output).resolve()
    if output.exists() and not output.is_dir():
        raise ValueError('出力先にはディレクトリを指定してください')
    for name in files:
        target = (output / name).resolve()
        if not target.is_relative_to(output) or target in inputs:
            raise ValueError('出力先が配布ディレクトリ外や元のソースを指しています')
    entry = output / 'index.html'
    generated = output.exists() and any(output.iterdir())
    if generated:
        if not entry.is_file() or marker not in entry.read_text(encoding='utf-8'):
            raise ValueError('出力先には空のディレクトリか、このツールの生成済みディレクトリを指定してください')

    # 初回は完成したフォルダを公開し、失敗時に半端な出力を残さない。
    if not generated:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.webchat-export-', dir=output.parent) as temporary:
            ready = Path(temporary) / 'ready'
            for name, data in files.items():
                _write_public(ready / name, data)
            if output.exists():
                output.rmdir()
            ready.rename(output)
    else:
        # 公開入口は最後に差し替える。古いassetは既存ページのために残す。
        for name, data in files.items():
            _write_public(output / name, data)
    return str(entry)


def export_webchat(api_base_url, bot, output, *, title='Webchat', storage='indexeddb', theme=None):
    api_base_url, origin = _api_url(api_base_url)
    if not isinstance(bot, str) or not bot.strip():
        raise ValueError('Bot名を指定してください')
    output = Path(output).resolve()
    if output.exists() and not output.is_dir():
        raise ValueError('出力先にはディレクトリを指定してください')

    # 参照UIと明示された公開テーマを読む。Bot設定やScenarioは取り込まない。
    assets = {name: (PROJECT_ROOT / path).read_bytes() for name, path in ASSET_SOURCES.items()}
    theme_assets = read_theme(theme, output)
    assets.update(theme_assets)
    script = assets['app.js'].decode('utf-8')
    script = _replace_once(script, "'../../webchat-client/index.js'", "'./client.js'")
    script = _replace_once(script, "'../../webchat-client/liff-host.js'", "'./liff-host.js'")
    script = _replace_once(script, "'./ui_logic.mjs'", "'./ui_logic.js'")
    assets['app.js'] = script.encode('utf-8')
    digest = hashlib.sha256()
    for name, data in sorted(assets.items()):
        digest.update(name.encode('utf-8') + b'\0' + data + b'\0')
    revision = digest.hexdigest()[:16]
    page = _page((PROJECT_ROOT / 'static/webchat/index.html').read_text(encoding='utf-8'),
                 api_base_url, bot, revision, origin, title=title, storage=storage, themed=bool(theme_assets))
    files = {f'assets/{revision}/{name}': data for name, data in assets.items()}
    files['LICENSE'] = (PROJECT_ROOT / 'LICENSE').read_bytes()
    files['index.html'] = page

    inputs = {(PROJECT_ROOT / path).resolve() for path in ASSET_SOURCES.values()}
    inputs.update({(PROJECT_ROOT / 'LICENSE').resolve(),
                   (PROJECT_ROOT / 'static/webchat/index.html').resolve()})
    entry = write_site(output, files, inputs=inputs)
    return {
        'ok': True, 'output': str(output), 'entry': str(entry),
        'api_base_url': api_base_url, 'bot': bot,
        'title': title, 'storage': storage,
        'revision': revision, 'files': sorted(files),
    }


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument('--api-base-url', required=True, help='Chat APIのHTTP/HTTPS基点URL')
    parser.add_argument('--bot', required=True, help='接続するBot名')
    parser.add_argument('--output', required=True, help='静的ファイルの出力ディレクトリ')
    add_page_arguments(parser)
    try:
        args = parser.parse_args(argv)
        result = export_webchat(args.api_base_url, args.bot, args.output,
                               title=args.title, storage=args.storage, theme=args.theme)
    except (OSError, ValueError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
