# coding: utf-8
"""LINE Webhook の署名検証と event の取り出し。

https://developers.line.biz/ja/reference/messaging-api/#signature-validation
event は LINE が送る JSON をそのまま dict で扱う（型変換はしない）。
"""
import base64
import hashlib
import hmac
import json


class InvalidSignatureError(Exception):
    pass


def verify_signature(channel_secret, body, signature):
    """body（bytes または str）の HMAC-SHA256 を base64 にした値が signature と一致するか。"""
    if isinstance(body, str):
        body = body.encode('utf-8')
    expected = base64.b64encode(
        hmac.new(channel_secret.encode('utf-8'), body, hashlib.sha256).digest()
    )
    # str 同士の compare_digest は非 ASCII 文字で TypeError になるので bytes で比べる
    if signature is None:
        signature = ''
    if isinstance(signature, str):
        signature = signature.encode('utf-8')
    return hmac.compare_digest(expected, signature)


def parse(channel_secret, body, signature):
    """署名を検証し、events の list（各要素は dict）を返す。"""
    if not verify_signature(channel_secret, body, signature):
        raise InvalidSignatureError('LINE の署名が一致しません')
    return json.loads(body).get('events', [])
