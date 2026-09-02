import base64
import hashlib

from itsdangerous import BadData, URLSafeSerializer

from plugin.webchat.errors import (
    ActionNotActive,
    IncompatibleState,
    InvalidStateToken,
    InvalidWebchatConfiguration,
)


TOKEN_VERSION = 1
STATE_TYPE = 'xstorybot-webchat-state'
POSTBACK_TYPE = 'xstorybot-webchat-postback'
STATE_SALT = 'xstorybot-webchat-client-state-v1'
POSTBACK_SALT = 'xstorybot-webchat-postback-v1'


def _decode_key(value):
    if not isinstance(value, str) or not value:
        raise InvalidWebchatConfiguration('Webchat署名鍵が設定されていません')
    try:
        padding = '=' * (-len(value) % 4)
        key = base64.b64decode(
            (value + padding).encode('ascii'), altchars=b'-_', validate=True)
    except (ValueError, UnicodeError, base64.binascii.Error) as error:
        raise InvalidWebchatConfiguration(
            'Webchat署名鍵のbase64url形式が不正です') from error
    if len(key) < 32:
        raise InvalidWebchatConfiguration(
            'Webchat署名鍵は32 byte以上必要です')
    return key


class WebchatTokenCodec:
    """stateとpostbackへ用途別saltでHMAC署名する。"""

    def __init__(self, signing_key):
        key = _decode_key(signing_key)
        signer_kwargs = {'digest_method': hashlib.sha256}
        self._state = URLSafeSerializer(
            key, salt=STATE_SALT, signer_kwargs=signer_kwargs)
        self._postback = URLSafeSerializer(
            key, salt=POSTBACK_SALT, signer_kwargs=signer_kwargs)

    @staticmethod
    def state_id(token):
        if not isinstance(token, str):
            raise InvalidStateToken('state tokenの形式が不正です')
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    @staticmethod
    def _validate_common(payload, expected_type, deployment, bot,
                         compatibility_epoch):
        if not isinstance(payload, dict):
            raise InvalidStateToken('token payloadがobjectではありません')
        if payload.get('v') != TOKEN_VERSION:
            raise InvalidStateToken('token versionが不正です')
        if payload.get('typ') != expected_type:
            raise InvalidStateToken('token typeが不正です')
        if payload.get('deployment') != deployment:
            raise InvalidStateToken('token deploymentが一致しません')
        if payload.get('bot') != bot:
            raise InvalidStateToken('token botが一致しません')
        if payload.get('scenario_compatibility_epoch') != compatibility_epoch:
            raise IncompatibleState('Scenario互換epochが一致しません')

    @staticmethod
    def _loads(serializer, token):
        if not isinstance(token, str) or not token:
            raise InvalidStateToken('tokenがありません')
        try:
            return serializer.loads(token)
        except BadData as error:
            raise InvalidStateToken('token署名を検証できません') from error

    def dump_state(self, payload):
        return self._state.dumps(payload)

    def load_state(self, token, deployment, bot, compatibility_epoch):
        payload = self._loads(self._state, token)
        self._validate_common(
            payload, STATE_TYPE, deployment, bot, compatibility_epoch)
        if not isinstance(payload.get('conversation_id'), str):
            raise InvalidStateToken('conversation IDが不正です')
        if not isinstance(payload.get('revision'), int):
            raise InvalidStateToken('state revisionが不正です')
        if not isinstance(payload.get('player'), dict):
            raise InvalidStateToken('player stateが不正です')
        return payload

    def dump_postback(self, payload):
        return self._postback.dumps(payload)

    def load_postback(self, token, state_payload, deployment, bot,
                      compatibility_epoch):
        payload = self._loads(self._postback, token)
        self._validate_common(
            payload, POSTBACK_TYPE, deployment, bot, compatibility_epoch)
        if payload.get('conversation_id') != state_payload.get('conversation_id'):
            raise ActionNotActive('postbackのconversationが一致しません')
        player = state_payload.get('player', {})
        if payload.get('action_generation') != player.get('action_generation'):
            raise ActionNotActive('postbackのaction世代が一致しません')
        if not isinstance(payload.get('resolved_action'), str):
            raise InvalidStateToken('postback actionが不正です')
        return payload
