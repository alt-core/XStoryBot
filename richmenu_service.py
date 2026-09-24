"""メニュー単位のLINE反映と、ビルド用の対応表の読込み。"""

from datetime import datetime, timezone
import io
import json
import logging
import re
import time

import requests

from cloud_backend import create_object_store, get_provider
from cloud_backend.contracts import ObjectNotFoundError
from plugin.line.api import LineApiError
from richmenu_spec import RAW_ID_PATTERN, content_digest, parse_bot_settings


MAX_IMAGE_BYTES = 1024 * 1024


class _ImageSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.auth = lambda request: request

    def rebuild_auth(self, prepared_request, response):
        prepared_request.headers.pop('Authorization', None)



def _key(bot):
    return f'richmenu/{bot}.json'


def load_record(store, bot):
    try:
        value = json.loads(store.load_private(_key(bot)))
        if (not isinstance(value, dict) or value.get('version') != 1 or value.get('bot') != bot
                or not isinstance(value.get('menus'), dict)):
            raise ValueError()
        for item in value['menus'].values():
            if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                    or not RAW_ID_PATTERN.fullmatch(item['id'])
                    or not isinstance(item.get('hash'), str)
                    or not re.fullmatch('[0-9a-f]{64}', item['hash'])):
                raise ValueError()
        return value
    except ObjectNotFoundError:
        return {'version': 1, 'bot': bot, 'menus': {}}
    except (ValueError, UnicodeError) as error:
        raise ValueError(f'記録 {_key(bot)} が破損しています。内容を確認し、削除して再反映すると作り直せます') from error


def build_menu_ids(bot, names):
    if bot.get_interface('line') is None:
        return {}
    if get_provider() == 'local':
        return {name: f'xsb-local-{name}' for name in names}
    record = load_record(create_object_store(), bot.name)
    return {name: item['id'] for name, item in record['menus'].items() if name in names}


class RichmenuService:
    """一つのHTTP要求では、一つのメニューか既定設定だけを扱う。"""

    def __init__(self, bot, bot_settings, constants, store=None):
        self.bot = bot
        self.menus = parse_bot_settings(bot_settings, constants)
        interface = bot.get_interface('line')
        if interface is None:
            raise ValueError('LINE interfaceが設定されていません')
        self.api = interface.api
        self.store = store if store is not None else create_object_store()
        self.deadline = time.monotonic() + 24
        self.stage = 'record'
        self.name = None
        self.richmenu_id = None

    def _timeout(self):
        remaining = self.deadline - time.monotonic() - 2
        if remaining <= 0:
            raise TimeoutError('リッチメニューの処理が時間内に完了しませんでした')
        return (min(3, remaining / 2), min(10, remaining / 2))

    def _image(self, menu):
        self.stage = 'download'
        url = menu.data['image']
        try:
            # netrcの認証情報を画像配信先へ送らない。
            with _ImageSession() as session:
                with session.get(url, stream=True, timeout=self._timeout()) as response:
                    if response.status_code != 200:
                        raise ValueError(f'画像の取得に失敗しました（HTTP {response.status_code}）')
                    chunks = []
                    size = 0
                    for chunk in response.iter_content(65536):
                        self._timeout()
                        size += len(chunk)
                        if size > MAX_IMAGE_BYTES:
                            raise ValueError('画像は1MB以下にしてください')
                        chunks.append(chunk)
            data = b''.join(chunks)
            self.stage = 'validate'
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ('PNG', 'JPEG'):
                    raise ValueError('画像はPNGまたはJPEGにしてください')
                if image.size != (menu.data['size']['width'], menu.data['size']['height']):
                    raise ValueError('画像寸法とsizeが一致しません')
                content_type = 'image/png' if image.format == 'PNG' else 'image/jpeg'
                image.verify()
            return data, content_type
        except (requests.RequestException, OSError, ValueError) as error:
            raise ValueError(f'リッチメニュー {self.name} の画像 {url}: {type(error).__name__}: {error}') from error

    def _prepare(self, name):
        self.name = name
        if name not in self.menus.menus:
            raise ValueError('リッチメニューが定義されていません')
        menu = self.menus.menus[name]
        data, content_type = self._image(menu)
        self.stage = 'record'
        record = load_record(self.store, self.bot.name)
        previous = record['menus'].get(name)
        self.richmenu_id = previous['id'] if previous else None
        digest = content_digest(menu, data)
        status = 'create' if previous is None else 'update'
        if previous and previous['hash'] == digest:
            self.stage = 'lookup'
            status = 'unchanged' if self.api.get_rich_menu(previous['id'], timeout=self._timeout()) else 'recreate'
        plan = {'name': name, 'status': status, 'current_id': previous['id'] if previous else None}
        return menu, data, content_type, digest, record, plan

    def plan(self, name):
        return self._prepare(name)[-1]

    def _save(self, record):
        self.stage = 'record'
        record['updated_at'] = datetime.now(timezone.utc).isoformat()
        self.store.store_private(_key(self.bot.name), json.dumps(record).encode(), 'application/json')

    def apply(self, name):
        menu, data, content_type, digest, record, plan = self._prepare(name)
        if plan['status'] == 'unchanged':
            return plan
        self.stage = 'create'
        new_id = self.api.create_rich_menu(menu.line_object(self.bot.name, name, digest), timeout=self._timeout())
        if not isinstance(new_id, str) or not RAW_ID_PATTERN.fullmatch(new_id):
            raise ValueError('LINEから返されたrichMenuIdが不正です')
        self.richmenu_id = new_id
        self.stage = 'upload'
        self.api.upload_rich_menu_image(new_id, data, content_type, timeout=self._timeout())
        record['menus'][name] = {'id': new_id, 'hash': digest, 'updated_at': datetime.now(timezone.utc).isoformat()}
        self._save(record)
        result = {**plan, 'new_id': new_id, 'previous_id': plan['current_id']}
        self.log('created', previous_id=plan['current_id'], hash=digest, status=plan['status'])
        return result

    def default_plan(self):
        name = self.menus.default
        if name is None:
            return {'configured': None, 'action': 'none'}
        plan = self.plan(name)
        self.stage = 'default'
        external = False
        try:
            current = self.api.get_default_rich_menu_id(timeout=self._timeout())
        except LineApiError as error:
            if error.status_code != 403:
                raise
            current = None
            external = True
        return {'configured': name, 'line_current': current, 'external': external,
                'target_id': plan['current_id'], 'ready': plan['status'] == 'unchanged',
                'action': 'none' if plan['status'] == 'unchanged' and current == plan['current_id'] else 'set_default'}

    def apply_default(self):
        plan = self.default_plan()
        if plan['action'] == 'none':
            return plan
        if not plan['ready']:
            raise ValueError('既定メニューを先にLINEへ反映してください')
        self.api.set_default_rich_menu(plan['target_id'], timeout=self._timeout())
        record = load_record(self.store, self.bot.name)
        record['default'] = plan['configured']
        self._save(record)
        self.log('default-set', previous_id=plan['line_current'])
        return plan

    def log(self, event, **values):
        logging.info(json.dumps({'type': 'XSBRichmenu', 'event': event,
                                 'bot': self.bot.name, 'menu': self.name, 'stage': self.stage,
                                 'richmenu_id': self.richmenu_id,
                                 **values}, ensure_ascii=False))
