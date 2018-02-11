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


def safe_list_get(li, index, default_value):
    return li[index] if len(li) > index else default_value


def append_visit_id(data, visit_id):
    return data + u'@@' + visit_id


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
