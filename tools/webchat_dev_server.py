#!/usr/bin/env python3
"""Webchat参照UIを外部依存なしで確認するローカル専用server。"""

import argparse
import base64
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import struct
import time
from urllib.parse import unquote, urlsplit
import uuid
import wave
import io


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TURN_PATH = re.compile(r'^/api/webchat/v1/bots/([^/]+)/turn$')
QUICK_REPLY_PROMPT = (
    '試したい機能名を入力するか、以下のボタンから選んでください。')


def _state_token(bot, revision):
    raw = json.dumps({
        'bot': bot,
        'revision': revision,
    }, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _state_revision(token, bot):
    if not isinstance(token, str) or not token:
        raise ValueError('state tokenがありません')
    try:
        padding = '=' * (-len(token) % 4)
        value = json.loads(base64.urlsafe_b64decode(
            (token + padding).encode('ascii')))
    except Exception as error:
        raise ValueError('state tokenが不正です') from error
    if value.get('bot') != bot or not isinstance(value.get('revision'), int):
        raise ValueError('state tokenが不正です')
    return value['revision']


def _action(action_type, label, value, echo=None):
    if action_type == 'message':
        return {'type': 'message', 'label': label, 'text': value}
    if action_type == 'postback':
        return {
            'type': 'postback',
            'label': label,
            'token': value,
            'echo_text': echo,
        }
    return {'type': 'uri', 'label': label, 'href': value}


def _sender(name, base_url, color):
    return {
        'name': name,
        'icon_url': f'{base_url}/devmedia/avatar-{color}.svg',
    }


def _message(message_type, request_id, index, sender, **values):
    return {
        'id': f'{request_id}:{index}',
        'role': 'assistant',
        'sender': sender,
        'type': message_type,
        **values,
    }


def _quick_replies():
    return [
        _action('message', '画像', 'image'),
        _action('message', '音声', 'audio'),
        _action('message', '動画', 'video'),
        _action('message', 'ボタン', 'button'),
        _action('message', 'イメージマップ', 'imagemap'),
        _action('message', '長文', 'long'),
        _action('message', '続きを読む', 'more'),
        _action('postback', '選択肢(postback)', 'selected', '選択しました'),
        _action('uri', 'ヘルプ', 'https://example.com/help'),
    ]


def _quick_reply_prompt(request_id, index, sender):
    return _message(
        'text', request_id, index, sender,
        text=QUICK_REPLY_PROMPT,
        quick_replies=_quick_replies())


def build_turn(bot, body, base_url, sleep=time.sleep):
    """モックturnを生成し、statusとJSON本文を返す。"""
    input_data = body.get('input') if isinstance(body, dict) else None
    if not isinstance(input_data, dict):
        return 400, {'code': 'invalid-request', 'title': 'invalid-request'}
    input_type = input_data.get('type')
    if input_type == 'start':
        revision = 0
        keyword = 'start'
        echo = None
    else:
        try:
            revision = _state_revision(body.get('state_token'), bot) + 1
        except ValueError:
            return 401, {'code': 'invalid-state', 'title': 'invalid-state'}
        if input_type == 'text' and isinstance(input_data.get('text'), str):
            keyword = input_data['text'].strip().lower()
            echo = input_data['text']
        elif (
                input_type == 'postback'
                and isinstance(input_data.get('postback_token'), str)):
            keyword = input_data['postback_token']
            if keyword == 'selected':
                echo = '選択しました'
            elif keyword in ('long-2', 'long-3'):
                echo = '続きを読む'
            else:
                echo = None
        else:
            return 400, {'code': 'invalid-request', 'title': 'invalid-request'}

    if keyword == 'error':
        return 500, {
            'code': 'internal-error',
            'title': 'モックのエラーです',
        }
    if keyword == 'slow':
        sleep(3)

    request_id = str(uuid.uuid4())
    guide = _sender('案内人', base_url, 'brown')
    messages = []
    if keyword == 'start':
        messages = [
            _message(
                'text', request_id, 0, guide,
                text='こんにちは。ここはWebchat参照UIのデモです。'),
            _message(
                'button', request_id, 1, guide,
                title='表示デモ', text='どの表示を確認しますか？',
                image_url=f'{base_url}/devmedia/card.svg',
                actions=[
                    _action('message', '画像を見る', 'image'),
                    _action('message', 'ボタンカード', 'button'),
                    _action('postback', '物語を進める', 'selected', '物語を進める'),
                    _action(
                        'uri', '公式サイト',
                        'https://example.com/?openExternalBrowser=1'),
                ]),
            _quick_reply_prompt(request_id, 2, guide),
        ]
    elif keyword == 'image':
        messages = [_message(
            'image', request_id, 0, guide,
            original_url=f'{base_url}/devmedia/photo.svg',
            preview_url=f'{base_url}/devmedia/photo.svg',
            alt='夕暮れの街並み')]
    elif keyword == 'audio':
        messages = [_message(
            'audio', request_id, 0, guide,
            url=f'{base_url}/devmedia/silence.wav',
            duration_ms=500, mime_type='audio/wav')]
    elif keyword == 'video':
        messages = [_message(
            'video', request_id, 0, guide,
            url=f'{base_url}/devmedia/video.mp4',
            poster_url=f'{base_url}/devmedia/poster.svg',
            completion_action=_action(
                'postback', '', 'video-complete', None))]
    elif keyword == 'video-complete':
        messages = [_message(
            'text', request_id, 0, guide,
            text='動画の再生完了actionを受け取りました。')]
    elif keyword == 'button':
        messages = [_message(
            'button', request_id, 0, guide,
            title='ボタンカード', text='actionの表示を確認できます。',
            image_url=f'{base_url}/devmedia/card.svg',
            actions=[
                _action('message', '通常message', 'message'),
                _action('postback', 'postback', 'selected', '選択しました'),
                _action(
                    'uri', 'チャット内link',
                    f'{base_url}/devpage/help'),
                _action(
                    'uri', '外部browser',
                    'https://example.com/?openExternalBrowser=1'),
            ])]
    elif keyword == 'imagemap':
        messages = [_message(
            'imagemap', request_id, 0, guide,
            image_url=f'{base_url}/devmedia/map.svg',
            sources=[
                {'url': f'{base_url}/devmedia/map.svg', 'width': 460},
                {'url': f'{base_url}/devmedia/map.svg', 'width': 1040},
            ],
            width=1040, height=520, alt='道の選択',
            areas=[
                {
                    'x': 0, 'y': 220, 'width': 480, 'height': 300,
                    'action': _action('message', '左の道', 'left'),
                },
                {
                    'x': 560, 'y': 220, 'width': 480, 'height': 300,
                    'action': _action('message', '右の道', 'right'),
                },
            ])]
    elif keyword in ('long', 'long-2', 'long-3'):
        page = {'long': 0, 'long-2': 1, 'long-3': 2}[keyword]
        first_index = page * 4
        messages = [
            _message(
                'text', request_id, index, guide,
                text=(
                    f'長文表示の確認 {first_index + index + 1}。'
                    + ('本文です。' * 10)))
            for index in range(4)
        ]
        if page < 2:
            messages[-1]['quick_replies'] = [_action(
                'postback', '続きを読む', f'long-{page + 2}', '続きを読む')]
    elif keyword == 'more':
        messages = [_message(
            'text', request_id, 0, guide,
            text='――物語のつづき。夜が明けて、街に光が差し込む。')]
    elif keyword == 'menu':
        messages = [_quick_reply_prompt(request_id, 0, guide)]
    elif keyword == 'slow':
        messages = [_message(
            'text', request_id, 0, guide,
            text='お待たせしました。3秒かかる応答でした。')]
    else:
        messages = [_message(
            'text', request_id, 0, guide,
            text=f'入力を受け取りました: {keyword}')]

    if keyword not in ('start', 'menu', 'long', 'long-2'):
        messages.append(
            _quick_reply_prompt(request_id, len(messages), guide))

    token = _state_token(bot, revision)
    return 200, {
        'schema_version': 1,
        'request_id': request_id,
        'state': {
            'id': hashlib.sha256(token.encode('utf-8')).hexdigest(),
            'revision': revision,
        },
        'state_token': token,
        'echo_message': echo,
        'messages': messages,
    }


def _scene_svg(title, show_play=False):
    title = escape(title)
    play = '''<g aria-label="再生" transform="translate(520 220)">
<circle r="58" fill="#111827" fill-opacity=".72" stroke="white" stroke-width="5"/>
<path d="M-15 -29 34 0-15 29Z" fill="white"/>
</g>''' if show_play else ''
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1040" height="520" viewBox="0 0 1040 520" role="img" aria-label="{title}">
<defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#344a78"/><stop offset="1" stop-color="#d9856d"/></linearGradient></defs>
<rect width="1040" height="520" fill="url(#sky)"/>
<circle cx="790" cy="132" r="68" fill="#ffd98b" fill-opacity=".94"/>
<path id="mountains-back" d="M0 355 165 205 300 325 455 165 625 335 790 225 1040 360V520H0Z" fill="#667495"/>
<path id="mountains-front" d="M0 390 210 270 370 385 590 245 770 380 920 290 1040 365V520H0Z" fill="#35445f"/>
<rect y="390" width="1040" height="130" fill="#1d2638"/>
<g id="city" fill="#111827">
<rect x="35" y="330" width="105" height="110"/><rect x="155" y="350" width="80" height="90"/><rect x="250" y="305" width="125" height="135"/><rect x="390" y="345" width="95" height="95"/><rect x="500" y="290" width="145" height="150"/><rect x="660" y="335" width="90" height="105"/><rect x="765" y="300" width="120" height="140"/><rect x="900" y="350" width="105" height="90"/>
</g>
<g fill="#f7d77b"><rect x="62" y="355" width="16" height="13"/><rect x="95" y="355" width="16" height="13"/><rect x="280" y="332" width="18" height="14"/><rect x="325" y="332" width="18" height="14"/><rect x="535" y="320" width="20" height="15"/><rect x="585" y="320" width="20" height="15"/><rect x="798" y="328" width="18" height="14"/><rect x="842" y="328" width="18" height="14"/></g>
{play}
</svg>'''.encode('utf-8')


def _map_svg():
    return '''<svg xmlns="http://www.w3.org/2000/svg" width="1040" height="520" viewBox="0 0 1040 520" role="img" aria-label="左右に分かれる道">
<defs><linearGradient id="map-sky" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#4e6596"/><stop offset="1" stop-color="#d99a73"/></linearGradient></defs>
<rect width="1040" height="520" fill="url(#map-sky)"/>
<circle cx="520" cy="105" r="55" fill="#ffe19a"/>
<path d="M0 285 170 165 315 275 465 145 610 275 790 175 1040 290V520H0Z" fill="#52617c"/>
<rect y="285" width="1040" height="235" fill="#24344a"/>
<path id="left-road" d="M520 520C485 430 410 350 105 285L245 246C438 325 505 388 536 462Z" fill="#7d8493"/>
<path id="right-road" d="M520 520C555 430 630 350 935 285L795 246C602 325 535 388 504 462Z" fill="#9a7880"/>
<path d="M515 505C480 420 395 350 185 285M525 505C560 420 645 350 855 285" fill="none" stroke="white" stroke-opacity=".75" stroke-width="8" stroke-dasharray="20 18"/>
<path d="M500 220H540V360H500Z" fill="#6a4937"/><path d="M520 215 405 160H635Z" fill="#f4e2b8" stroke="#6a4937" stroke-width="8"/>
</svg>'''.encode('utf-8')


def _avatar_svg(color):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="0 0 120 120" role="img" aria-label="案内役の顔">
<rect width="120" height="120" rx="24" fill="{color}"/>
<g id="face"><circle cx="60" cy="58" r="39" fill="white" fill-opacity=".2"/><circle cx="45" cy="52" r="5" fill="white"/><circle cx="75" cy="52" r="5" fill="white"/><path d="M42 72Q60 88 78 72" fill="none" stroke="white" stroke-width="6" stroke-linecap="round"/></g>
</svg>'''.encode('utf-8')


def _silence_wav():
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(struct.pack('<h', 0) * 4000)
    return stream.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = 'XStoryBotWebchatDev/1'

    def log_message(self, format, *args):
        print(f'[webchat-dev] {format % args}')

    def _base_url(self):
        return f'http://{self.headers.get("Host", "127.0.0.1")}'

    def _send(
            self, status, body, content_type, cache='no-store',
            frame_ancestors="'none'"):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        if content_type.startswith('text/html'):
            self.send_header(
                'Content-Security-Policy',
                "default-src 'self'; base-uri 'none'; object-src 'none'; "
                f"frame-ancestors {frame_ancestors}; form-action 'self'; "
                "script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' https:; "
                "media-src 'self' https:; frame-src 'self' https:")
            self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, relative):
        path = (PROJECT_ROOT / relative).resolve()
        try:
            path.relative_to(PROJECT_ROOT)
        except ValueError:
            self.send_error(404)
            return
        if not path.is_file():
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        self._send(200, path.read_bytes(), content_type, 'no-cache')

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/devpage/help':
            body = '''<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>チャット内リンク</title></head><body>
<main><h1>チャット内リンク</h1><p>iframe表示の確認ページです。</p></main>
</body></html>'''.encode('utf-8')
            self._send(
                200, body, 'text/html; charset=utf-8',
                frame_ancestors="'self'")
            return
        if re.fullmatch(r'/chat/[^/]+', path):
            self._serve_file('static/webchat/index.html')
            return
        if path.startswith('/static/webchat/'):
            self._serve_file(path.lstrip('/'))
            return
        if path.startswith('/webchat-client/'):
            self._serve_file(path.lstrip('/'))
            return
        media = {
            '/devmedia/avatar-brown.svg': _avatar_svg('#8d655c'),
            '/devmedia/card.svg': _scene_svg('表示デモ'),
            '/devmedia/photo.svg': _scene_svg('夕暮れの街並み'),
            '/devmedia/poster.svg': _scene_svg('動画デモ', show_play=True),
            '/devmedia/map.svg': _map_svg(),
        }
        if path in media:
            self._send(200, media[path], 'image/svg+xml')
            return
        if path == '/devmedia/silence.wav':
            self._send(200, _silence_wav(), 'audio/wav')
            return
        if path == '/devmedia/video.mp4':
            self._serve_file('tests/fixtures/webchat/video.mp4')
            return
        self.send_error(404)

    def do_POST(self):
        match = TURN_PATH.fullmatch(urlsplit(self.path).path)
        if match is None:
            self.send_error(404)
            return
        media_type = self.headers.get('Content-Type', '').split(';', 1)[0].strip()
        if media_type != 'application/json':
            self._send(415, b'{"code":"unsupported-media-type"}',
                       'application/problem+json')
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            body = json.loads(self.rfile.read(size))
        except (TypeError, ValueError, json.JSONDecodeError):
            self._send(400, b'{"code":"invalid-request"}',
                       'application/problem+json')
            return
        bot = unquote(match.group(1))
        status, result = build_turn(bot, body, self._base_url())
        encoded = json.dumps(
            result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        content_type = (
            'application/json' if status == 200 else 'application/problem+json')
        self._send(status, encoded, content_type)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Webchat参照UIのローカル確認serverを起動します。')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'http://{args.host}:{args.port}/chat/bot')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
