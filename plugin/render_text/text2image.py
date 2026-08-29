# coding: utf-8
import logging
import hashlib
import re
import json
from urllib.parse import unquote_plus

import build_cache
from bottle import request, Bottle, abort, response, HTTPResponse
import time

import settings
from plugin.render_text import renderer

CACHE_SEC = 3600
DEFAULT_RENDERING_OPTIONS = {
    'size_x': 1024,
    'size_y': 1024,
    'margin_x': 12,
    'margin_y': 12,
    'is_vertical': False,
    'font_size': 100,
    'color': 'black',
    'background': 'white',
    'line_height': 1.5,
}

app = Bottle()

def error(msg):
    logging.error(msg)
    abort(404)


@app.route('/text2image/<encoded_text>')
def text2image(encoded_text):
    content_type = 'image/png'

    if len(encoded_text) > 2000:
        error(f'too long request: {len(encoded_text)}')

    options = DEFAULT_RENDERING_OPTIONS.copy()
    options.update(settings.PLUGINS.get('render_text', {}).get('text2image', {}))

    size_x = int(request.params.get('size_x', options['size_x']))
    logging.info(f'size_x: {size_x}')
    size_y = int(request.params.get('size_y', options['size_y']))
    margin_x = int(request.params.get('margin_x', options['margin_x']))
    margin_y = int(request.params.get('margin_y', options['margin_y']))

    text_rendering_options = {}
    for key in ['is_vertical', 'font_path', 'font_size', 'color', 'background', 'line_height', 'base_line_offset', 'disable_word_wrap', 'burasagari_chars', 'special_char_table']:
        param = request.params.get(key, None)
        if param is not None:
            if key in ['is_vertical', 'disable_word_wrap']:
                text_rendering_options[key] = (param == 'True')
            elif key in ['font_size', 'line_height', 'base_line_offset']:
                text_rendering_options[key] = float(param)
            elif key in ['color', 'background']:
                m = re.match(r'\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', param)
                if m:
                    text_rendering_options[key] = (int(m.group(1)),int(m.group(2)),int(m.group(3)))
                else:
                    # background は拡張子の付いた画像ファイル読み込みの機能がある
                    text_rendering_options[key] = param.replace('.', '')
            elif key in ['burasagari_chars']:
                text_rendering_options[key] = request.params.getunicode(key)
            else:
                # 外部からの上書きを禁止しているパラメータ
                pass
        if not (key in text_rendering_options) and (key in options):
            text_rendering_options[key] = options[key]

    source_hash = hashlib.md5((encoded_text + json.dumps([size_x, size_y, margin_x, margin_y, text_rendering_options])).encode()).hexdigest()
    cache_etag = build_cache.get_cache('text2imagecacheetag:' + source_hash)
    ims = request.environ.get('HTTP_IF_NONE_MATCH')
    if ims and ims == cache_etag:
        # Not Modified
        return HTTPResponse(status=304)

    if cache_etag is not None:
        # キャッシュがヒットしたので返す
        cache = build_cache.get_cache('text2imagecache:' + source_hash)
        if cache is not None:
            response.content_type = content_type
            response.set_header('Content-Length', str(len(cache)))
            response.set_header('ETag', cache_etag)
            response.set_header('Expires', time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + CACHE_SEC)))
            #response.set_header('Cache-Control', 'max-age='+str(CACHE_SEC))
            return cache if request.method != 'HEAD' else ''

    logging.info('text2image cache miss-hit:' + encoded_text)

    text = unquote_plus(encoded_text)
    output_buffer, _ = renderer.render_text_to_png(text, size_x, size_y, margin_x, margin_x, margin_y, margin_y, **text_rendering_options)

    etag = hashlib.md5(output_buffer).hexdigest()
    build_cache.set_cache('text2imagecacheetag:' + source_hash, etag , sec=CACHE_SEC)
    build_cache.set_cache('text2imagecache:' + source_hash, output_buffer , sec=CACHE_SEC+30)

    response.content_type = content_type
    response.set_header('Content-Length', str(len(output_buffer)))
    response.set_header('ETag', etag)
    response.set_header('Expires', time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + CACHE_SEC)))
    #response.set_header('Cache-Control', 'max-age='+str(CACHE_SEC))
    return output_buffer if request.method != 'HEAD' else ''


if __name__ == "__main__":
    app.run(host='localhost', port=8080, debug=True)
