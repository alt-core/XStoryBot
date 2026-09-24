"""リッチメニュー定義の展開・検証。外部I/Oや画像ライブラリに依存しない。"""

import copy
from dataclasses import dataclass
import hashlib
import json
import re
import string
from urllib.parse import urlsplit

from utility import normalize_name


RAW_ID_PATTERN = re.compile(r'^richmenu-[0-9a-f]{32}$')
NAME_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def resolve_line_id(arg, ids):
    name = resolve_menu_name(arg, ids)
    return arg if name is None else ids[name]


def resolve_menu_name(arg, names):
    if arg.startswith('richmenu-'):
        return None
    name = normalize_name(arg)
    if name not in names:
        raise ValueError(f'リッチメニュー {arg} を解決できません。定義とビルド内容を確認してください')
    return name


def menu_definitions(bot_settings):
    definitions = bot_settings.get('richmenus', {})
    if not isinstance(definitions, dict):
        raise ValueError('richmenusはmappingにしてください')
    for name in definitions:
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name) or name.startswith('richmenu-'):
            raise ValueError(f'リッチメニューの論理名が不正です: {name}')
    default = bot_settings.get('default_richmenu')
    if default is not None and (not isinstance(default, str) or normalize_name(default) not in definitions):
        raise ValueError('default_richmenuには定義済みの論理名を指定してください')
    return definitions


def referenced_constants(value):
    if isinstance(value, dict):
        return set().union(*(referenced_constants(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(referenced_constants(item) for item in value))
    if isinstance(value, str):
        return {normalize_name(field) for _, field, _, _ in string.Formatter().parse(value) if field}
    return set()


def _expand(value, constants):
    if isinstance(value, dict):
        return {key: _expand(item, constants) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, constants) for item in value]
    if not isinstance(value, str):
        return value
    parts = []
    for literal, field, spec, conversion in string.Formatter().parse(value):
        parts.append(literal)
        if field is None:
            continue
        if not field or field.isdecimal() or any(char in field for char in '.[]') or spec or conversion:
            raise ValueError('定数参照には単純な名前だけを指定してください')
        name = normalize_name(field)
        if name not in constants:
            raise ValueError(f'定数 {field} が定義されていません')
        parts.append(str(constants[name]))
    return ''.join(parts)


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError('必須項目がないか、未知の項目があります')


def _text(value, maximum):
    if not isinstance(value, str) or not 1 <= len(value.encode('utf-16-le')) // 2 <= maximum:
        raise ValueError(f'1〜{maximum}文字の文字列にしてください')


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{minimum}以上の整数にしてください')


def _url(value, schemes):
    parsed = urlsplit(value)
    if parsed.scheme not in schemes or (parsed.scheme == 'https' and not parsed.hostname):
        raise ValueError('URLの形式が不正です')
    if parsed.scheme == 'tel' and not parsed.path:
        raise ValueError('電話番号がありません')
    if parsed.username or parsed.password or any(c.isspace() for c in value) or '\\' in value:
        raise ValueError('URLの形式が不正です')


@dataclass
class Menu:
    data: dict

    def line_object(self, bot, name, digest):
        return {**{key: copy.deepcopy(value) for key, value in self.data.items() if key != 'image'},
                'name': f'xsb:{bot}:{name}:{digest[:12]}'[:300]}

    def hash_source(self):
        return canonical({key: value for key, value in self.data.items() if key != 'image'})

    def webchat_spec(self, name, image_url):
        areas = []
        for area in self.data['areas']:
            action = area['action']
            label = action.get('label') or action.get('text') or action.get('displayText') or action.get('uri') or 'メニュー'
            if action['type'] == 'uri':
                result = {'type': 'uri', 'label': label, 'href': action['uri']}
            else:
                result = {'type': 'menu', 'label': label, 'echo_text': (
                    action['text'] if action['type'] == 'message' else action.get('displayText'))}
            areas.append({**area['bounds'], 'action': result})
        return {
            'id': name, 'revision': hashlib.sha256(canonical(self.data)).hexdigest(),
            'chat_bar_text': self.data['chatBarText'], 'selected': self.data['selected'],
            'image_url': image_url, **self.data['size'], 'areas': areas,
        }


@dataclass
class RichmenuSet:
    menus: dict
    default: str | None


def parse_bot_settings(bot_settings, constants, allow_local_http=False):
    definitions = menu_definitions(bot_settings)
    menus = {}
    for name, source in definitions.items():
        field = '定義'
        try:
            value = _expand(source, constants)
            _keys(value, ('image', 'size', 'chatBarText', 'areas'), ('selected',))
            field = 'image'
            _text(value['image'], 2000)
            _url(value['image'], ('https',))
            field = 'size'
            _keys(value['size'], ('width', 'height'))
            w, h = value['size']['width'], value['size']['height']
            _integer(w, 800); _integer(h, 250)
            if w > 2500 or w / h < 1.45:
                raise ValueError('幅は800〜2500、縦横比は1.45以上にしてください')
            field = 'chatBarText'
            _text(value['chatBarText'], 14)
            field = 'selected'
            value.setdefault('selected', False)
            if type(value['selected']) is not bool:
                raise ValueError('boolにしてください')
            field = 'areas'
            if not isinstance(value['areas'], list) or not 1 <= len(value['areas']) <= 20:
                raise ValueError('領域は1〜20件にしてください')
            for index, area in enumerate(value['areas']):
                field = f'areas[{index}]'
                _keys(area, ('bounds', 'action'))
                _keys(area['bounds'], ('x', 'y', 'width', 'height'))
                bounds = area['bounds']
                for key in ('x', 'y', 'width', 'height'):
                    _integer(bounds[key], 1 if key in ('width', 'height') else 0)
                if bounds['x'] + bounds['width'] > w or bounds['y'] + bounds['height'] > h:
                    raise ValueError('領域が画像の外へはみ出しています')
                action = area['action']
                if not isinstance(action, dict):
                    raise ValueError('actionはmappingにしてください')
                kind = action.get('type')
                required = {'message': 'text', 'postback': 'data', 'uri': 'uri'}.get(kind)
                if required is None:
                    raise ValueError('actionはmessage／postback／uriだけ使えます')
                _keys(action, ('type', required), ('label', 'displayText') if kind == 'postback' else ('label',))
                _text(action[required], 1000 if kind == 'uri' else 300)
                if 'label' in action:
                    _text(action['label'], 20)
                if 'displayText' in action:
                    _text(action['displayText'], 300)
                if kind == 'postback' and '@@' in action['data']:
                    raise ValueError('postback.dataには@@を含められません')
                if kind == 'uri':
                    parsed = urlsplit(action['uri'])
                    local_http = (allow_local_http and parsed.hostname == '127.0.0.1'
                                  and parsed.port is not None)
                    _url(action['uri'], ('https', 'tel', 'http') if local_http else ('https', 'tel'))
            menus[name] = Menu(value)
        except (ValueError, TypeError) as error:
            raise ValueError(f'リッチメニュー {name} の {field}: {error}') from error
    default = bot_settings.get('default_richmenu')
    if default is not None:
        default = normalize_name(default)
    return RichmenuSet(menus, default)


def content_digest(menu, image_bytes):
    return hashlib.sha256(menu.hash_source() + b'\n' + image_bytes).hexdigest()
