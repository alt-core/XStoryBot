import hashlib
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from urllib.parse import urljoin
from urllib.parse import urlsplit

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


def _normalize_origins(values, allow_empty=False):
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
                parsed.scheme.lower() != 'https'
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in ('', '/')
                or parsed.query
                or parsed.fragment):
            raise InvalidWebchatConfiguration(
                'allowed_originsにはHTTPS originを指定してください')
        origin = _format_origin(parsed)
        if origin not in result:
            result.append(origin)
    if not result and not allow_empty:
        raise InvalidWebchatConfiguration('allowed_originsが空です')
    return tuple(result)


class WebchatInterface:
    def __init__(self, bot_name, params):
        self.bot_name = bot_name
        self.params = params
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
            params.get('allowed_origins', []), allow_empty=True))
        for origin in _normalize_origins(
                params.get('self_origin', []), allow_empty=True):
            if origin not in allowed_origins:
                allowed_origins.append(origin)
        if not allowed_origins:
            raise InvalidWebchatConfiguration('allowed_originsが空です')
        self.allowed_origins = tuple(allowed_origins)
        self.external_http_origins = _normalize_origins(
            params.get('external_http_origins', []), allow_empty=True)
        self.media_origins = _normalize_origins(
            params.get('media_origins', []), allow_empty=True)
        for icon_url in self.sender_icon_urls.values():
            self.validate_media_url(icon_url)
        self.codec = WebchatTokenCodec(params.get('signing_key'))

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
        parsed = urlsplit(str(url))
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
        return str(url)

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
        messages = self._presenter.present(context, reactions)
        for index, message in enumerate(messages):
            message['id'] = f'{context.request_id}:{index}'

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
        state_token = self.codec.dump_state(state_payload)
        return {
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


class WebchatInterfaceFactory:
    def __init__(self, params):
        self.params = params
        register_runtime()

    def create_interface(self, bot_name, params):
        return WebchatInterface(
            bot_name, utility.merge_params(self.params, params))
