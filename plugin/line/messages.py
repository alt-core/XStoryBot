# coding: utf-8
"""LINE Messaging API のメッセージオブジェクト（送信JSON）を組み立てる。

key 名は公式リファレンスの camelCase をそのまま使う。
https://developers.line.biz/ja/reference/messaging-api/#message-objects

- 値が None の項目は送らない
- Flex の contents はシナリオ作者の JSON をそのまま渡し、検査も変換もしない
- 新しい項目を使いたいときは、リファレンスの key 名で引数を足すだけでよい
"""


def _fields(**fields):
    """None の項目を除いた dict を返す。"""
    return {key: value for key, value in fields.items() if value is not None}


# --- メッセージ ---

def text(text, sender=None):
    return _fields(type='text', text=text, sender=sender)


def image(original_content_url, preview_image_url, sender=None):
    return _fields(
        type='image',
        originalContentUrl=original_content_url,
        previewImageUrl=preview_image_url,
        sender=sender)


def video(original_content_url, preview_image_url, tracking_id=None, sender=None):
    return _fields(
        type='video',
        originalContentUrl=original_content_url,
        previewImageUrl=preview_image_url,
        trackingId=tracking_id,
        sender=sender)


def audio(original_content_url, duration, sender=None):
    return _fields(
        type='audio',
        originalContentUrl=original_content_url,
        duration=duration,
        sender=sender)


def template(alt_text, template, sender=None):
    return _fields(type='template', altText=alt_text, template=template, sender=sender)


def imagemap(base_url, alt_text, width, height, actions, sender=None):
    return _fields(
        type='imagemap',
        baseUrl=base_url,
        altText=alt_text,
        baseSize={'width': width, 'height': height},
        actions=actions,
        sender=sender)


def flex(alt_text, contents, sender=None):
    return _fields(type='flex', altText=alt_text, contents=contents, sender=sender)


# --- 共通部品 ---

def sender(name, icon_url=None):
    return _fields(name=name, iconUrl=icon_url)


def quick_reply(actions):
    return {'items': [{'type': 'action', 'action': action} for action in actions]}


# --- テンプレート ---

def buttons_template(text, actions, title=None, thumbnail_image_url=None):
    return _fields(
        type='buttons',
        text=text,
        title=title,
        thumbnailImageUrl=thumbnail_image_url,
        actions=actions)


def confirm_template(text, actions):
    return _fields(type='confirm', text=text, actions=actions)


def carousel_template(columns):
    return {'type': 'carousel', 'columns': columns}


def carousel_column(text, actions, title=None, thumbnail_image_url=None):
    return _fields(
        text=text,
        title=title,
        thumbnailImageUrl=thumbnail_image_url,
        actions=actions)


# --- アクション ---

def message_action(label, text):
    return {'type': 'message', 'label': label, 'text': text}


def postback_action(label, data, display_text=None):
    return _fields(type='postback', label=label, data=data, displayText=display_text)


def uri_action(label, uri):
    return {'type': 'uri', 'label': label, 'uri': uri}


# --- イメージマップ ---

def imagemap_area(x, y, width, height):
    return {'x': x, 'y': y, 'width': width, 'height': height}


def imagemap_message_action(text, area):
    return {'type': 'message', 'text': text, 'area': area}


def imagemap_uri_action(link_uri, area):
    return {'type': 'uri', 'linkUri': link_uri, 'area': area}
