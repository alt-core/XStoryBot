import requests


class WebchatError(Exception):
    """Webchat経路で固定HTTP errorへ変換する基底例外。"""

    status = 500
    code = 'internal-error'


class InvalidWebchatConfiguration(WebchatError):
    status = 503
    code = 'service-unavailable'


class InvalidStateToken(WebchatError):
    status = 401
    code = 'invalid-state'


class IncompatibleState(WebchatError):
    status = 409
    code = 'incompatible-state'


class ActionNotActive(WebchatError):
    status = 409
    code = 'action-not-active'


class BotNotWebCompatible(WebchatError):
    status = 422
    code = 'bot-not-web-compatible'


class BotResponseTooLarge(WebchatError):
    status = 500
    code = 'bot-response-too-large'


class TurnDeadlineExceeded(WebchatError):
    status = 504
    code = 'turn-timeout'


class ExternalHttpError(requests.RequestException, WebchatError):
    status = 502
    code = 'external-http-error'


class ExternalHttpTimeout(requests.Timeout, WebchatError):
    status = 504
    code = 'external-http-timeout'
