import re
from unicodedata import normalize
import json
import yaml
import os


def to_hankaku(text):
    """文字列を半角に正規化"""
    return normalize('NFKC', text)


def parse_url(cell):
    m = re.match(r'^(https?://|tel:)', cell)
    if m:
        return m.group(0)
    else:
        return None


def is_valid_web_url(url):
    return re.match(r'^https?://', url) is not None


def parse_sender(raw_msg):
    parts = raw_msg.split("\n", 1)
    first_line = parts[0].strip()
    if len(parts) >= 2 and (first_line.endswith("：") or first_line.endswith(":")):
        return first_line[:-1], parts[1]
    else:
        return None, raw_msg


def safe_list_get(li, index, default_value):
    return li[index] if len(li) > index else default_value


def encode_action_string(action, action_token):
    return action + '@@' + action_token


def decode_action_string(data):
    arr = data.split('@@', 1)
    action = arr[0]
    attrs = {}
    if len(arr) > 1:
        attrs['action_token'] = arr[1]
    return action, attrs


def is_special_action(action):
    return re.match(r'^[*＊#＃]', action)


def is_internal_action(action):
    return re.match(r'^[:：]', action)


def sanitize_action(action):
    if is_special_action(action) or is_internal_action(action):
        # 先頭にスペースを詰めてサニタイズ
        return " " + action
    return action


def remove_tail_empty_cells(row):
    # 空のセルは右端から順に消す
    while row:
        if row[-1] is not None and row[-1] != '':
            break
        row.pop()


def merge_params(dic1, dic2):
    if dic1 is None:
        dic = {}
    else:
        dic = dic1.copy()
    if dic2 is not None:
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
    output = ''
    for row in values:
        for cell in row:
            output += "'{}',".format(cell)
        output += "\n"
    return output


def make_ok_json(msg, data=None):
    response = {'code': 200, 'result': 'Success', 'message': msg}
    if data is not None:
        response['data'] = data
    return json.dumps(response, ensure_ascii=False)


def make_ng_json(msg):
    return json.dumps({'code': 200, 'result': 'Failure', 'message': msg}, ensure_ascii=False)


def make_error_json(code, msg, data=None):
    response = {'code': code, 'result': 'Error', 'message': msg}
    if data is not None:
        response['data'] = data
    return json.dumps(response, ensure_ascii=False)


def to_str(text):
    """Python 3では全てstrなので、単純に文字列に変換"""
    return str(text)


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


def deep_merge(base, override):
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class EnvTag:
    yaml_tag = '!env'

    def __init__(self, value):
        self.value = value

    @classmethod
    def from_yaml(cls, loader, node):
        return cls(loader.construct_scalar(node))

    def resolve(self):
        return os.getenv(self.value, '')


class FormatTag:
    yaml_tag = '!format'

    def __init__(self, values):
        self.template = values[0]
        self.args = values[1:]

    @classmethod
    def from_yaml(cls, loader, node):
        return cls(loader.construct_sequence(node))

    def resolve(self):
        resolved_args = [arg.resolve() if hasattr(arg, 'resolve') else arg for arg in self.args]
        return self.template.format(*resolved_args)


def resolve_tags(value):
    if hasattr(value, 'resolve'):
        return value.resolve()
    elif isinstance(value, dict):
        return {k: resolve_tags(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_tags(v) for v in value]
    return value


def load_yaml(path, custom_tags=None):
    class CustomLoader(yaml.SafeLoader):
        pass

    if custom_tags:
        for tag, cls in custom_tags.items():
            CustomLoader.add_constructor(tag, cls.from_yaml)

    with open(path) as f:
        data = yaml.load(f, Loader=CustomLoader)

    return resolve_tags(data)

def load_settings_yaml(path):
    return load_yaml(path, custom_tags={EnvTag.yaml_tag: EnvTag, FormatTag.yaml_tag: FormatTag})


def deep_dump(obj, indent=0, visited=None):
    if visited is None:
        visited = set()
    # 循環参照のチェック
    if id(obj) in visited:
        print(" " * indent + f"(already visited {type(obj).__name__})")
        return
    visited.add(id(obj))

    prefix = " " * indent
    print(f"{prefix}{type(obj).__name__}: {repr(obj)}")

    if isinstance(obj, dict):
        for key, value in obj.items():
            print(f"{prefix}  Key {type(key).__name__} {repr(key)} ->")
            deep_dump(value, indent + 4, visited)
    elif isinstance(obj, (list, tuple, set)):
        for index, item in enumerate(obj):
            print(f"{prefix}  [{index}] ->")
            deep_dump(item, indent + 4, visited)
    elif hasattr(obj, '__dict__'):
        for attr, value in obj.__dict__.items():
            print(f"{prefix}  Attribute '{attr}' ->")
            deep_dump(value, indent + 4, visited)
