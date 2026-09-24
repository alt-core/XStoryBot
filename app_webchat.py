"""DynamoDBへ依存しないWebchat専用Bottle entry point。"""

import settings
from bottle import Bottle, response, static_file

import commands
import common_commands
import hub
import log_config
from plugin.line import quick_reply, quick_reply_v2
from plugin.webchat.errors import InvalidWebchatConfiguration
from plugin.webchat.interface import WebchatInterfaceFactory
from plugin.webchat.session import session_bots
from plugin.liff.interface import LiffPlugin_Interface
from plugin.webchat import webapi
from runtime import BotRuntime


log_config.configure(settings.CLOUD_SETTINGS.get('provider'), settings.DEPLOY_ENV)


def _plugin_params(name):
    params = settings.OPTIONS.copy()
    params.update(settings.PLUGINS.get(name, {}))
    return params


hub.clear()
commands.clear()
common_commands.setup(settings.OPTIONS)

if 'line.quick_reply' in settings.PLUGINS:
    quick_reply.load_plugin(_plugin_params('line.quick_reply'))
if 'line.quick_reply_v2' in settings.PLUGINS:
    quick_reply_v2.load_plugin(_plugin_params('line.quick_reply_v2'))
if 'line.more' in settings.PLUGINS:
    from plugin.webchat import more as webchat_more
    webchat_more.load_plugin(_plugin_params('line.more'))

_factory = WebchatInterfaceFactory(
    _plugin_params('webchat'),
    constants=settings.CONSTANTS,
    local_settings=(settings.BACKEND_SETTINGS
                    if settings.CLOUD_SETTINGS.get('provider') == 'local' else None))
_bots = {}
_initialization_error = None
try:
    for _name, _bot_settings in settings.BOTS.items():
        _interface_settings = next((
            item for item in _bot_settings.get('interfaces', [])
            if item.get('type') == 'webchat'
        ), None)
        if _interface_settings is None:
            continue
        _interface = _factory.create_interface(
            _name, _interface_settings.get('params', {}), bot_settings=_bot_settings)
        if not _interface.enabled:
            continue
        _interfaces = {'webchat': _interface}
        _liff_settings = next((item for item in _bot_settings.get('interfaces', [])
                               if item.get('type') == 'liff'), None)
        if _liff_settings is not None:
            _liff_params = _plugin_params('liff')
            _liff_params.update(_liff_settings.get('params') or {})
            _liff_params.setdefault('allow_origin', '')
            _interfaces['liff'] = LiffPlugin_Interface(_name, _liff_params)
        _bots[_name] = BotRuntime(
            _name,
            _interfaces,
            scenario_loader=None,
            state_namespace=_bot_settings.get('state_namespace', _name),
        )
    for _bot in _bots.values():
        session_bots(_bot, _bots.get)
except InvalidWebchatConfiguration as error:
    _bots.clear()
    _initialization_error = error


def get_bot(bot_name):
    if _initialization_error is not None:
        raise _initialization_error
    return _bots.get(bot_name)


webapi.configure(get_bot)
app = Bottle()


@app.get('/healthz')
def health_check():
    response.content_type = 'application/json; charset=utf-8'
    return '{"status":"ok"}'


@app.get('/chat/<bot_name>')
def chat(bot_name):
    try:
        bot = get_bot(bot_name)
    except InvalidWebchatConfiguration:
        response.status = 503
        return '503 Service Unavailable'
    if bot is None:
        response.status = 404
        return '404 Not Found'
    interface = bot.get_interface('webchat')
    media_sources = ' '.join(interface.media_origins) or 'https:'
    if not interface.allow_external_media:
        media_sources = ''
    frame_sources = ' '.join({interface._origin(app['url'])
                              for app in interface.liff_apps.values()})
    return static_file(
        'index.html',
        root='static/webchat',
        headers={
            'Cache-Control': 'no-store',
            'Content-Security-Policy': (
                "default-src 'self'; base-uri 'none'; object-src 'none'; "
                "frame-ancestors 'none'; form-action 'self'; "
                "script-src 'self'; style-src 'self' 'unsafe-inline'; "
                f"connect-src 'self'; img-src 'self' {media_sources}; "
                f"media-src 'self' {media_sources}; "
                f"frame-src 'self' https: {frame_sources}"
            ),
            'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff',
        },
    )


@app.get('/static/webchat/<filepath:path>')
def webchat_static(filepath):
    return static_file(
        filepath, root='static/webchat',
        headers={
            'Cache-Control': 'no-cache',
            'X-Content-Type-Options': 'nosniff',
        })


@app.get('/webchat-client/<filepath:path>')
def webchat_client(filepath):
    return static_file(
        filepath, root='webchat-client',
        headers={
            'Cache-Control': 'no-cache',
            'X-Content-Type-Options': 'nosniff',
        })


app.merge(webapi.app)
