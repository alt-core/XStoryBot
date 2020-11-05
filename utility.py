# coding: utf-8
import re
from unicodedata import normalize
import json


def to_hankaku(unicode_str):
    return normalize('NFKC', unicode_str)


def parse_url(cell):
    m = re.match(r'^(https?://|tel:)', cell)
    if m:
        return m.group(0)
    else:
        return None


def parse_sender(raw_msg):
    parts = raw_msg.split(u"\n", 1)
    first_line = parts[0].strip()
    if len(parts) >= 2 and (first_line.endswith(u"：") or first_line.endswith(u":")):
        return first_line[:-1], parts[1]
    else:
        return None, raw_msg


def safe_list_get(li, index, default_value):
    return li[index] if len(li) > index else default_value


def encode_action_string(action, action_token):
    return action + u'@@' + action_token


def decode_action_string(data):
    arr = data.split(u'@@', 1)
    action = arr[0]
    attrs = {}
    if len(arr) > 1:
        attrs['action_token'] = arr[1]
    return action, attrs


def is_special_action(action):
    return re.match(ur'^[*＊#＃]', action)


def sanitize_action(action):
    if is_special_action(action):
        # 先頭にスペースを詰めてサニタイズ
        return u" " + action
    return action


def remove_tail_empty_cells(row):
    # 空のセルは右端から順に消す
    while row:
        if row[-1] is not None and row[-1] != u'':
            break
        row.pop()


def merge_params(dic1, dic2):
    dic = dic1.copy()
    dic.update(dic2)
    return dic


def extract_params(dic, names):
    params = {}
    for name in names:
        if name in dic:
            params[name] = dic[name]
    return params


def table_to_str(values):
    if not values: return 'No entry\n'
    output = u''
    for row in values:
        for cell in row:
            output += u"'{}',".format(cell)
        output += u"\n"
    return output


def make_ok_json(msg):
    return json.dumps({u'code': 200, u'result': u'Success', u'message': msg}, ensure_ascii=False)


def make_ng_json(msg):
    return json.dumps({u'code': 200, u'result': u'Failure', u'message': msg}, ensure_ascii=False)


def make_error_json(code, msg):
    return json.dumps({u'code': code, u'message': msg}, ensure_ascii=False)


def to_str(str_or_unicode):
    if isinstance(str_or_unicode, str):
        return str_or_unicode
    if not isinstance(str_or_unicode, unicode):
        str_or_unicode = unicode(str_or_unicode)
    return str_or_unicode.encode('utf-8')


class CascadingDictionary(dict):
    def __init__(self, *dicts):
        self.dicts = dicts

    def __getitem__(self, key):
        for d in self.dicts:
            if key in d:
                return d[key]
        raise KeyError

    def __contains__(self, key):
        for d in self.dicts:
            if key in d:
                return True
        return False

