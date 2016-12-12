# coding: utf-8
from StringIO import StringIO
import urllib
from PIL import Image
import re
import logging

if __name__ == "__main__":
    import os, sys, json, subprocess
    gcloud_info = json.loads(subprocess.check_output(['gcloud', 'info', '--format=json']))
    sdk_path = os.path.join(gcloud_info["installation"]["sdk_root"], 'platform', 'google_appengine')
    sys.path.append(sdk_path)
    sys.path.append(os.path.join(sdk_path, 'lib', 'yaml', 'lib'))
    sys.path.insert(0, './lib')
    from google.appengine.api import memcache
    from google.appengine.ext import testbed
    tb = testbed.Testbed()
    tb.activate()
    tb.init_memcache_stub()
else:
    from google.appengine.api import memcache

from bottle import request, Bottle, abort, response, HTTPResponse
import httplib2
import time

CACHE_SEC = 3600

app = Bottle()
http = httplib2.Http(cache=memcache)

formats = {
    'image/png': 'PNG',
    'image/jpeg': 'JPEG'
}

@app.route('/line/image/<request_path:path>')
def image(request_path):
    image_path = request_path
    option = None

    cache_etag = memcache.get('imagecacheetag:' + request_path)
    ims = request.environ.get('HTTP_IF_NONE_MATCH')
    if ims and ims == cache_etag:
        # Not Modified
        return HTTPResponse(status=304)

    m = re.match('^(.*)/([^/]*)$', request_path)
    if m and m.group(2) in ['preview', '1040', '700', '460', '300', '240']:
        image_path = m.group(1)
        option = m.group(2)

    image_url = urllib.unquote_plus(image_path)
    #print image_path, image_url
    # TODO: open-proxy にならないように image_url にホワイトリスト判定を行う

    resp, content = http.request(image_url)
    content_type = resp['content-type']
    if content_type not in formats:
        logging.error('imagefile not found:' + image_url + '; ' + content_type)
        abort(404)

    if cache_etag is not None and cache_etag == resp['etag']:
        # キャッシュがヒットしたので返す
        cache = memcache.get('imagecache:' + request_path)
        if cache is not None:
            response.content_type = content_type
            response.set_header('Content-Length', str(len(cache)))
            response.set_header('ETag', cache_etag)
            response.set_header('Expires', time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + CACHE_SEC)))
            #response.set_header('Cache-Control', 'max-age='+str(CACHE_SEC))
            return cache if request.method != 'HEAD' else ''

    logging.info('image cache miss-hit:' + request_path)

    input = StringIO(content)
    image = Image.open(input)

    if option is None:
        resize_to = 1024
    elif option == 'preview':
        resize_to = 240
    else:
        resize_to = int(option)

    if image.size[0] < resize_to and image.size[1] < resize_to and len(content) < 1024*1024:
        output_buffer = content
    else:
        resize_x = image.size[0]
        resize_y = image.size[1]
        if resize_x >= resize_y:
            resize_y = resize_y * resize_to / resize_x
            resize_x = resize_to
        else:
            resize_x = resize_x * resize_to / resize_y
            resize_y = resize_to
        image = image.resize((resize_x, resize_y))

        output = StringIO()
        image.save(output, formats[content_type])
        output_buffer = output.getvalue()
        del content
        output.close()

    del image
    input.close()

    memcache.set('imagecacheetag:' + request_path, resp['etag'], time=CACHE_SEC)
    memcache.set('imagecache:' + request_path, output_buffer, time=CACHE_SEC+1)

    response.content_type = content_type
    response.set_header('Content-Length', str(len(output_buffer)))
    response.set_header('ETag', resp['etag'])
    response.set_header('Expires', time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + CACHE_SEC)))
    #response.set_header('Cache-Control', 'max-age='+str(CACHE_SEC))
    return output_buffer if request.method != 'HEAD' else ''


if __name__ == "__main__":
    app.run(host='localhost', port=8080, debug=True)
