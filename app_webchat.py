"""DynamoDBへ依存しないWebchat専用Bottle entry point。"""

import logging

import settings
from bottle import Bottle, response, static_file

import commands
import common_commands
import hub
from plugin.line import quick_reply, quick_reply_v2
from plugin.webchat import more as webchat_more
from plugin.webchat.errors import InvalidWebchatConfiguration
from plugin.webchat.interface import WebchatInterfaceFactory
from plugin.webchat import webapi
from runtime import BotRuntime


logging.getLogger().setLevel(logging.INFO)


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
    webchat_more.load_plugin(_plugin_params('line.more'))

_factory = WebchatInterfaceFactory(_plugin_params('webchat'))
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
            _name, _interface_settings.get('params', {}))
        if not _interface.enabled:
            continue
        _bots[_name] = BotRuntime(
            _name,
            {'webchat': _interface},
            scenario_loader=None,
            state_namespace=_bot_settings.get('state_namespace', _name),
        )
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
                "frame-src 'self' https:"
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
