import hashlib
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from urllib.parse import unquote, urljoin
from urllib.parse import urlsplit, urlunsplit

import requests
import utility

from plugin.webchat.context import WebchatActionContext
from plugin.webchat.errors import (
    BotNotWebCompatible,
    ExternalHttpError,
    ExternalHttpTimeout,
    InvalidWebchatConfiguration,
)
from plugin.webchat.presenter import WebchatPresenter, register_runtime
from plugin.webchat.token import (
    BUNDLE_VERSION,
    POSTBACK_TYPE,
    STATE_TYPE,
    TOKEN_VERSION,
    WebchatTokenCodec,
)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _format_origin(parsed):
    host = parsed.hostname
    if not host:
        raise InvalidWebchatConfiguration('originのhostがありません')
    try:
        port = parsed.port
    except ValueError as error:
        raise InvalidWebchatConfiguration('originのportが不正です') from error
    if ':' in host:
        host = f'[{host}]'
    origin = f'{parsed.scheme.lower()}://{host.lower()}'
    default_port = 443 if parsed.scheme.lower() == 'https' else 80
    if port is not None and port != default_port:
        origin += f':{port}'
    return origin


def _normalize_origins(values, allow_empty=False, local_origin=None):
    if values is None and allow_empty:
        values = []
    if isinstance(values, str):
        values = [value.strip() for value in values.split(',')]
    if not isinstance(values, list):
        raise InvalidWebchatConfiguration('allowed_originsは配列で指定してください')
    result = []
    for value in values:
        value = str(value).strip()
        if not value and allow_empty:
            continue
        parsed = urlsplit(value)
        if (
                parsed.scheme.lower() not in ('http', 'https')
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in ('', '/')
                or parsed.query
                or parsed.fragment):
            raise InvalidWebchatConfiguration(
                'allowed_originsにはHTTPS originを指定してください')
        origin = _format_origin(parsed)
        if parsed.scheme.lower() != 'https' and origin != local_origin:
            raise InvalidWebchatConfiguration(
                'allowed_originsにはHTTPS originを指定してください')
        if origin not in result:
            result.append(origin)
    if not result and not allow_empty:
        raise InvalidWebchatConfiguration('allowed_originsが空です')
    return tuple(result)


class WebchatInterface:
    supports_task_execution = False

    def __init__(self, bot_name, params, local_settings=None, bot_settings=None, constants=None):
        self.bot_name = bot_name
        self.params = params
        self.local_origin = None
        self.local_media_base = None
        self.local_asset_urls = {}
        self.allow_external_media = True
        self.liff_apps = {}
        self.constants_override = {}
        self.richmenus = None
        self.richmenu_specs = {}
        if local_settings is not None:
            from cloud_backend import get_provider
            if get_provider() != 'local':
                raise InvalidWebchatConfiguration('local媒体設定はlocal provider専用です')
            base_url = local_settings.get('public_base_url', '')
            parsed = urlsplit(base_url)
            try:
                port = parsed.port
            except ValueError as error:
                raise InvalidWebchatConfiguration('local媒体のportが不正です') from error
            if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                    or port is None or port < 1
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path != '/local-media' or parsed.query or parsed.fragment):
                raise InvalidWebchatConfiguration('local媒体には固定loopback URLを指定してください')
            store_id = local_settings.get('store_id')
            if not isinstance(store_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', store_id):
                raise InvalidWebchatConfiguration('local媒体のstore_idが不正です')
            self.local_origin = _format_origin(parsed)
            self.local_media_base = f'{base_url}/{store_id}/'
            self.local_asset_urls = dict(local_settings.get('webchat_asset_urls', {}))
            if any(not isinstance(key, str) or not isinstance(value, str)
                   or not value.startswith(self.local_media_base)
                   for key, value in self.local_asset_urls.items()):
                raise InvalidWebchatConfiguration('local媒体mapの形式または保存先が不正です')
            self.allow_external_media = _as_bool(
                local_settings.get('allow_external_media', False))
        self.enabled = _as_bool(params.get('enabled'))
        allowed_commands = params.get('allowed_commands', []) or []
        if not isinstance(allowed_commands, (list, tuple, set)):
            raise InvalidWebchatConfiguration(
                'allowed_commandsは配列で指定してください')
        self.allowed_commands = set(allowed_commands)
        self.sender_icon_urls = params.get('sender_icon_urls', {}) or {}
        if not isinstance(self.sender_icon_urls, dict):
            raise InvalidWebchatConfiguration(
                'sender_icon_urlsはobjectで指定してください')
        self.reply_fallback_message = params.get(
            'reply_fallback_message', '選択してください')
        self.alt_text = params.get('alt_text', '選択可能な画像')
        try:
            self.turn_deadline_seconds = float(
                params.get('turn_deadline_seconds', 29.0))
        except (TypeError, ValueError) as error:
            raise InvalidWebchatConfiguration(
                'turn deadlineが不正です') from error
        if (
                not math.isfinite(self.turn_deadline_seconds)
                or self.turn_deadline_seconds <= 0):
            raise InvalidWebchatConfiguration('turn deadlineが不正です')
        self._presenter = WebchatPresenter(self)
        self._scenario_loaded = False
        self._scenario_lock = threading.Lock()
        if not self.enabled:
            self.codec = None
            self.allowed_origins = ()
            self.deployment = ''
            self.scenario_uri = ''
            self.scenario_revision = ''
            self.compatibility_epoch = ''
            self.start_action = ''
            self.external_http_origins = ()
            self.media_origins = ()
            return

        try:
            from richmenu_spec import parse_bot_settings
            self.constants_override = utility.normalize_constants(params.get('constants', {}), scalar_only=True)
            self.richmenus = parse_bot_settings(bot_settings or {}, {**(constants or {}), **self.constants_override},
                                                allow_local_http=self.local_origin is not None)
        except ValueError as error:
            raise InvalidWebchatConfiguration(str(error)) from error

        self.deployment = str(
            params.get('deployment') or os.getenv('XSBOT_DEPLOY_ENV') or 'prod')
        self.scenario_uri = str(params.get('scenario_uri') or '')
        if not self.scenario_uri:
            raise InvalidWebchatConfiguration('固定Scenario URIがありません')
        self.scenario_revision = self._derive_revision(self.scenario_uri)
        self.compatibility_epoch = str(
            params.get('scenario_compatibility_epoch') or '')
        if not self.compatibility_epoch:
            raise InvalidWebchatConfiguration('Scenario互換epochがありません')
        self.start_action = str(params.get('start_action') or '')
        if not self.start_action:
            raise InvalidWebchatConfiguration('Webchat start actionがありません')
        allowed_origins = list(_normalize_origins(
            params.get('allowed_origins', []), allow_empty=True,
            local_origin=self.local_origin))
        for origin in _normalize_origins(
                params.get('self_origin', []), allow_empty=True,
                local_origin=self.local_origin):
            if origin not in allowed_origins:
                allowed_origins.append(origin)
        if not allowed_origins:
            raise InvalidWebchatConfiguration('allowed_originsが空です')
        self.allowed_origins = tuple(allowed_origins)
        self.external_http_origins = _normalize_origins(
            params.get('external_http_origins', []), allow_empty=True)
        self.media_origins = _normalize_origins(
            params.get('media_origins', []), allow_empty=True,
            local_origin=self.local_origin)
        for icon_url in self.sender_icon_urls.values():
            self.validate_media_url(icon_url)
        for name, menu in self.richmenus.menus.items():
            try:
                image_url = self.validate_media_url(menu.data['image'])
            except BotNotWebCompatible as error:
                raise InvalidWebchatConfiguration(f'リッチメニュー {name}: {error}') from error
            self.richmenu_specs[name] = menu.webchat_spec(name, image_url)
        self.codec = WebchatTokenCodec(params.get('signing_key'))
        apps = params.get('liff_apps', {}) or {}
        if not isinstance(apps, dict):
            raise InvalidWebchatConfiguration('liff_appsにはページ名ごとの設定を指定してください')
        pages = set()
        liff_ids = set()
        for name, app in apps.items():
            if (not isinstance(name, str) or not name or not isinstance(app, dict)
                    or not {'url', 'bot'} <= set(app) or set(app) - {'url', 'bot', 'match', 'liff_id'}
                    or not isinstance(app['bot'], str) or not app['bot']
                    or not isinstance(app['url'], str)):
                raise InvalidWebchatConfiguration('LIFFページにはurlとbotを指定してください')
            try:
                parsed = urlsplit(app['url'])
                origin = _format_origin(parsed)
            except (ValueError, InvalidWebchatConfiguration) as error:
                raise InvalidWebchatConfiguration('LIFFページのURLが不正です') from error
            local_http = (self.local_origin is not None and parsed.scheme == 'http'
                          and parsed.hostname == '127.0.0.1' and parsed.port is not None)
            if (not parsed.hostname or (parsed.scheme != 'https' and not local_http)
                    or parsed.username is not None or parsed.password is not None
                    or '\\' in app['url'] or any(c.isspace() or ord(c) < 32 for c in app['url'])):
                raise InvalidWebchatConfiguration('LIFFページにはHTTPS URLを指定してください')
            page = (origin, parsed.path or '/')
            if page in pages:
                raise InvalidWebchatConfiguration('同じLIFFページのoriginとpathを重複登録できません')
            pages.add(page)
            matching = app.get('match', 'exact')
            liff_id = app.get('liff_id')
            if matching not in ('exact', 'prefix'):
                raise InvalidWebchatConfiguration('LIFFページのmatchはexactかprefixにしてください')
            if liff_id is not None:
                if (not isinstance(liff_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', liff_id)
                        or liff_id in liff_ids):
                    raise InvalidWebchatConfiguration('liff_idは重複しないLIFF IDを指定してください')
                liff_ids.add(liff_id)
            self.liff_apps[name] = {
                'url': urlunsplit((parsed.scheme, origin.split('://', 1)[1],
                                  parsed.path or '/', parsed.query, parsed.fragment)),
                'bot': app['bot'],
                **({'match': matching} if matching != 'exact' else {}),
                **({'liff_id': liff_id} if liff_id is not None else {}),
            }

    def public_liff_apps(self):
        return [{'id': name, **{key: value for key, value in app.items() if key != 'bot'}}
                for name, app in self.liff_apps.items()]

    @staticmethod
    def _derive_revision(uri):
        parsed = urlsplit(uri)
        value = parsed.path.rstrip('/').rsplit('/', 1)[-1]
        if value:
            return value
        return hashlib.sha256(uri.encode('utf-8')).hexdigest()

    def get_service_list(self):
        return {'webchat': self} if self.enabled else {}

    def get_retry_count(self):
        return 0

    def should_raise_exceptions(self):
        return True

    def origin_allowed(self, origin):
        return isinstance(origin, str) and origin in self.allowed_origins

    @staticmethod
    def _origin(url):
        parsed = urlsplit(url)
        if (
                parsed.scheme.lower() not in ('http', 'https')
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None):
            raise BotNotWebCompatible('外部HTTP URLが不正です')
        try:
            return _format_origin(parsed)
        except InvalidWebchatConfiguration as error:
            raise BotNotWebCompatible('外部HTTP URLが不正です') from error

    def validate_media_url(self, url):
        url = str(url)
        if self.local_origin is not None:
            url = self.local_asset_urls.get(requests.utils.requote_uri(url), url)
        parsed = urlsplit(url)
        if self.local_origin is not None and url.startswith(self.local_media_base):
            key = unquote(parsed.path).split('/', 3)[-1]
            if (parsed.query or parsed.fragment or '\\' in key
                    or any(part in ('', '.', '..') for part in key.split('/'))):
                raise BotNotWebCompatible('local媒体のkeyが不正です')
            return url
        if self.local_origin is not None and not self.allow_external_media:
            raise BotNotWebCompatible('媒体URLをlocal.assetsへ登録してください')
        if (
                parsed.scheme.lower() != 'https'
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None):
            raise BotNotWebCompatible('media URLはHTTPSで指定してください')
        try:
            origin = _format_origin(parsed)
        except InvalidWebchatConfiguration as error:
            raise BotNotWebCompatible('media URLが不正です') from error
        if self.media_origins and origin not in self.media_origins:
            raise BotNotWebCompatible('media originが許可されていません')
        return url

    def request_external(self, context, method, url, **kwargs):
        started_at = time.monotonic()
        current_url = str(url)
        current_method = str(method).upper()
        request_options = dict(kwargs)
        while True:
            if self._origin(current_url) not in self.external_http_origins:
                raise BotNotWebCompatible('外部HTTP originが許可されていません')
            remaining = context.deadline - time.monotonic()
            if remaining <= 0.5:
                raise ExternalHttpTimeout('外部HTTPの実行時間がありません')
            try:
                response = requests.request(
                    current_method,
                    current_url,
                    timeout=max(0.1, remaining - 0.5),
                    allow_redirects=False,
                    stream=True,
                    **request_options,
                )
            except requests.Timeout as error:
                raise ExternalHttpTimeout(
                    '外部HTTPがtimeoutしました') from error
            except requests.RequestException as error:
                raise ExternalHttpError(
                    '外部HTTPに失敗しました') from error
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get('Location')
                if location:
                    status = response.status_code
                    response.close()
                    current_url = urljoin(current_url, location)
                    # paramsは最初のURLへ一度だけ適用する。redirect先では
                    # Locationに含まれるqueryだけを使用する。
                    request_options.pop('params', None)
                    if (
                            status == 303
                            or (
                                status in (301, 302)
                                and current_method not in ('GET', 'HEAD')
                            )):
                        current_method = 'GET'
                        request_options.pop('data', None)
                        request_options.pop('json', None)
                        headers = dict(request_options.get('headers', {}))
                        headers.pop('Content-Type', None)
                        request_options['headers'] = headers
                    continue
            chunks = []
            received = 0
            provider_limit = 6 * 1024 * 1024
            try:
                content_length = response.headers.get('Content-Length')
                try:
                    declared_length = (
                        int(content_length) if content_length is not None
                        else None
                    )
                except ValueError:
                    declared_length = None
                if declared_length is not None and declared_length > provider_limit:
                    raise ExternalHttpError(
                        '外部HTTP responseが同期Lambda境界を超えました')
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if context.deadline - time.monotonic() <= 0.5:
                        raise ExternalHttpTimeout(
                            '外部HTTPがturn deadlineを超えました')
                    received += len(chunk)
                    if received > provider_limit:
                        raise ExternalHttpError(
                            '外部HTTP responseが同期Lambda境界を超えました')
                    chunks.append(chunk)
            except requests.Timeout as error:
                raise ExternalHttpTimeout(
                    '外部HTTPがtimeoutしました') from error
            except requests.RequestException as error:
                if isinstance(error, (ExternalHttpError, ExternalHttpTimeout)):
                    raise
                raise ExternalHttpError(
                    '外部HTTP responseの読込に失敗しました') from error
            finally:
                response.close()
            response._content = b''.join(chunks)
            response._content_consumed = True
            logging.info(json.dumps({
                'type': 'XSBWebchat',
                'event': 'external-http',
                'request_id': context.request_id,
                'conversation': context.user.user_id,
                'method': current_method,
                'origin': self._origin(current_url),
                'status': response.status_code,
                'response_bytes': received,
                'elapsed_ms': int((time.monotonic() - started_at) * 1000),
            }, ensure_ascii=False, separators=(',', ':')))
            return response

    def ensure_scenario(self, bot):
        if (
                self._scenario_loaded
                and bot.scenario is not None
                and bot.scenario_uri == self.scenario_uri):
            return
        with self._scenario_lock:
            if (
                    self._scenario_loaded
                    and bot.scenario is not None
                    and bot.scenario_uri == self.scenario_uri):
                return
            try:
                from scenario import Scenario
                bot.scenario = Scenario.load_from_uri(self.scenario_uri)
            except Exception as error:
                raise InvalidWebchatConfiguration(
                    '固定Scenarioを読み込めません') from error
            bot.scenario_uri = self.scenario_uri
            self._scenario_loaded = True

    def create_start_context(self, request_id, deadline_seconds=None):
        conversation_id = secrets.token_urlsafe(32)
        context = WebchatActionContext(
            self.bot_name, self, conversation_id, self.start_action, {},
            deadline_seconds=deadline_seconds)
        context.state_payload = {
            'conversation_id': conversation_id,
            'revision': -1,
            'player': {},
        }
        context.echo_message = None
        context.request_id = request_id
        return context

    def current_richmenu(self, player):
        name = player.get('richmenu')
        if name and name not in self.richmenu_specs:
            logging.warning('保存されたリッチメニューが定義にありません: %s', name)
        if name not in self.richmenu_specs:
            name = self.richmenus.default if self.richmenus else None
        return self.richmenu_specs.get(name)

    def richmenu_input(self, input_data, player):
        menu = self.current_richmenu(player)
        if menu is None or input_data['menu'] != menu['id'] or input_data['revision'] != menu['revision']:
            return None
        index = input_data['area']
        areas = self.richmenus.menus[menu['id']].data['areas']
        if index >= len(areas) or areas[index]['action']['type'] == 'uri':
            raise ValueError('リッチメニューの領域が不正です')
        action = areas[index]['action']
        if action['type'] == 'message':
            return utility.sanitize_action(action['text']), action['text']
        return action['data'], action.get('displayText')

    def create_context_from_state(self, state_payload, action, request_id,
                                  echo_message=None, deadline_seconds=None):
        context = WebchatActionContext(
            self.bot_name,
            self,
            state_payload['conversation_id'],
            action,
            state_payload['player'],
            deadline_seconds=deadline_seconds,
        )
        context.state_payload = state_payload
        context.echo_message = echo_message
        context.request_id = request_id
        return context

    def create_context(self, user, action, attrs):
        raise InvalidWebchatConfiguration(
            'Webchat generic create_contextは利用できません')

    def load_state(self, token):
        return self.codec.load_state(
            token, self.deployment, self.bot_name,
            self.compatibility_epoch)

    def load_postback(self, token, state_payload):
        return self.codec.load_postback(
            token, state_payload, self.deployment, self.bot_name,
            self.compatibility_epoch)

    def make_postback_action(self, context, label, resolved_action, echo_text):
        payload = {
            'v': TOKEN_VERSION,
            'typ': POSTBACK_TYPE,
            'deployment': self.deployment,
            'bot': self.bot_name,
            'scenario_compatibility_epoch': self.compatibility_epoch,
            'scenario_revision': self.scenario_revision,
            'conversation_id': context.user.user_id,
            'action_generation': context.status.action_token,
            'resolved_action': resolved_action,
            'echo_text': echo_text,
        }
        return {
            'type': 'postback',
            'label': label,
            'token': self.codec.dump_postback(payload),
            'echo_text': echo_text,
        }

    @staticmethod
    def preview_image_url(url):
        return re.sub(r'_1024\.', '_240.', url)

    def respond_reaction(self, context, reactions):
        return self.make_response(context, self.present_reactions(context, reactions))

    def present_reactions(self, context, reactions):
        messages = self._presenter.present(context, reactions)
        for index, message in enumerate(messages):
            message['id'] = f'{context.request_id}:{index}'
        return messages

    def make_response(self, context, messages, peer_players=None, peer_epochs=None):
        next_revision = int(context.state_payload.get('revision', -1)) + 1
        state_payload = {
            'v': TOKEN_VERSION,
            'typ': STATE_TYPE,
            'deployment': self.deployment,
            'bot': self.bot_name,
            'scenario_compatibility_epoch': self.compatibility_epoch,
            'scenario_revision': self.scenario_revision,
            'conversation_id': context.user.user_id,
            'revision': next_revision,
            'player': context.saved_player,
        }
        if peer_epochs is not None:
            state_payload['v'] = BUNDLE_VERSION
            state_payload['peer_players'] = peer_players
            state_payload['peer_epochs'] = peer_epochs
        state_token = self.codec.dump_state(state_payload)
        result = {
            'schema_version': 1,
            'request_id': context.request_id,
            'state': {
                'id': self.codec.state_id(state_token),
                'revision': next_revision,
            },
            'state_token': state_token,
            'echo_message': context.echo_message,
            'messages': messages,
        }
        if self.richmenu_specs:
            result['richmenu'] = self.current_richmenu(context.saved_player)
        return result


class WebchatInterfaceFactory:
    def __init__(self, params, local_settings=None, constants=None):
        self.params = params
        self.local_settings = local_settings
        self.constants = constants
        register_runtime()

    def create_interface(self, bot_name, params, bot_settings=None):
        merged = utility.merge_params(self.params, params)
        try:
            merged['constants'] = {
                **utility.normalize_constants(self.params.get('constants', {}), scalar_only=True),
                **utility.normalize_constants((params or {}).get('constants', {}), scalar_only=True),
            }
        except ValueError as error:
            raise InvalidWebchatConfiguration(str(error)) from error
        return WebchatInterface(
            bot_name, merged, local_settings=self.local_settings,
            bot_settings=bot_settings, constants=self.constants)
