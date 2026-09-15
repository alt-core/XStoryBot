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


def _page(source, api_base_url, bot, revision, origin):
    policy = (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        f"connect-src 'self' {origin}; img-src 'self' https: {origin}; "
        f"media-src 'self' https: {origin}; frame-src 'self' https:"
    )
    replacements = (
        ('data-webchat-api-base-url=""',
         f'data-webchat-api-base-url="{escape(api_base_url, quote=True)}" {EXPORT_MARKER}'),
        ('data-webchat-bot=""', f'data-webchat-bot="{escape(bot, quote=True)}"'),
        ('<!-- WEBCHAT_EXPORT_META -->',
         f'<meta http-equiv="Content-Security-Policy" content="{escape(policy, quote=True)}">\n'
         '  <meta name="referrer" content="no-referrer">'),
        ('href="/static/webchat/style.css"', f'href="./assets/{revision}/style.css"'),
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


def export_webchat(api_base_url, bot, output):
    api_base_url, origin = _api_url(api_base_url)
    if not isinstance(bot, str) or not bot.strip():
        raise ValueError('Bot名を指定してください')
    output = Path(output).resolve()
    if output.exists() and not output.is_dir():
        raise ValueError('出力先にはディレクトリを指定してください')

    # 固定した公開ソースだけを読む。settingsやScenario、媒体は取り込まない。
    assets = {name: (PROJECT_ROOT / path).read_bytes() for name, path in ASSET_SOURCES.items()}
    script = assets['app.js'].decode('utf-8')
    script = _replace_once(script, "'../../webchat-client/index.js'", "'./client.js'")
    script = _replace_once(script, "'./ui_logic.mjs'", "'./ui_logic.js'")
    assets['app.js'] = script.encode('utf-8')
    digest = hashlib.sha256()
    for name, data in sorted(assets.items()):
        digest.update(name.encode('utf-8') + b'\0' + data + b'\0')
    revision = digest.hexdigest()[:16]
    page = _page((PROJECT_ROOT / 'static/webchat/index.html').read_text(encoding='utf-8'),
                 api_base_url, bot, revision, origin)
    files = {f'assets/{revision}/{name}': data for name, data in assets.items()}
    files['LICENSE'] = (PROJECT_ROOT / 'LICENSE').read_bytes()
    files['index.html'] = page

    inputs = {(PROJECT_ROOT / path).resolve() for path in ASSET_SOURCES.values()}
    inputs.update({(PROJECT_ROOT / 'LICENSE').resolve(),
                   (PROJECT_ROOT / 'static/webchat/index.html').resolve()})
    for name in files:
        target = (output / name).resolve()
        if not target.is_relative_to(output) or target in inputs:
            raise ValueError('出力先が配布ディレクトリ外や元のソースを指しています')
    entry = output / 'index.html'
    generated = output.exists() and any(output.iterdir())
    if generated:
        if not entry.is_file() or EXPORT_MARKER not in entry.read_text(encoding='utf-8'):
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
    return {
        'ok': True, 'output': str(output), 'entry': str(entry),
        'api_base_url': api_base_url, 'bot': bot,
        'revision': revision, 'files': sorted(files),
    }


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument('--api-base-url', required=True, help='Chat APIのHTTP/HTTPS基点URL')
    parser.add_argument('--bot', required=True, help='接続するBot名')
    parser.add_argument('--output', required=True, help='静的ファイルの出力ディレクトリ')
    try:
        args = parser.parse_args(argv)
        result = export_webchat(args.api_base_url, args.bot, args.output)
    except (OSError, ValueError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
