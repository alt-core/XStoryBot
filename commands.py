# coding: utf-8

import re

import utility
from expression import Expression

catalog = []
catalog_map = {}

object_catalog = []
object_catalog_map = {}


def clear():
    del catalog[:]
    catalog_map.clear()
    del object_catalog[:]
    object_catalog_map.clear()


RE_LABEL = re.compile(r'^[*＊#＃].*')
RE_VARIABLE = re.compile(r'^[$＄].*')
RE_IMAGE = re.compile(r'^=IMAGE\("([^"]+)"\)')
RE_NUMBER = re.compile(ur'^[\-−]?[0-9０-９]+([.．][0-9０-９]*)?$')


class Default_Builder(object):
    def build_from_command(self, builder, sender, msg, options, children=None):
        if children:
            builder.add_command(sender, msg, options, children)
        else:
            builder.add_command(sender, msg, options, None)
        return True


def _convert_format_string(s):
    result = []
    for cell_format in (s or '').split():
        required = True
        max_len = 0
        if cell_format.startswith('[') and cell_format.endswith(']'):
            required = False
            cell_format = cell_format[1:-1]
        m = re.match(ur'^([^(]*)(?:\((\d+)\))?$', cell_format)
        if m:
            cell_format = m.group(1)
            max_len = int(m.group(2) or 0)
        result.append((cell_format.split('|'), required, max_len))
    return result


class CommandEntry(object):
    def __init__(self, names, options=None, child=None, grandchild=None, builder=None, runtime=None, service='*', specs=None, min_version=1):
        """
        :param names: [u'@コマンド名', u'@Command']
        :param options: 'label|text|expr|image|raw(MAX_LEN) [label|text|expr|image|raw(MAX_LEN)] ...'
        :param child: 'label|text|expr|raw(MAX_LEN) [label|text|expr|raw(MAX_LEN)] ...'
        :param grandchild: 'label|text|expr|raw(MAX_LEN) [label|text|expr|raw(MAX_LEN)] ...'
        :param builder: SomePlugin_Builder() or None
        :param runtime: SomePlugin_Runtime() or None
        :param service: 'line'
        :param specs: {'children_min': 1, 'children_max': 4}
        :param min_version: scenario version
        """
        self.command = names
        self.options = _convert_format_string(options)
        self.child = _convert_format_string(child)
        self.grandchild = _convert_format_string(grandchild)
        self.builder = builder
        self.runtime = runtime
        self.service = service
        self.specs = specs or {}
        self.min_version = min_version


def register_command(entry):
    """コマンドカタログにコマンドを登録する。

    :param entry: 追加するコマンド
    :type entry: CommandEntry
    """
    catalog.append(entry)
    command = entry.command
    for word in command:
        l = catalog_map.get(word, [])
        l.append(entry)
        catalog_map[word] = l


def register_commands(entries):
    """コマンドカタログに複数のコマンドを登録する。

    :param entries: 追加するコマンド群
    :type entries: CommandEntry[]
    """
    for entry in entries:
        register_command(entry)


def get_command(msg, version, service='*'):
    """コマンドカタログからコマンド情報を取得する。

    :param msg: 取得するコマンド
    :param service: フィルタするサービス, '*' でフィルタを行わない"""
    l = catalog_map.get(msg, [])
    for entry in l:
        if (service == '*' or entry.service == '*' or entry.service == service) and version >= entry.min_version:
            return entry
    return None


def check_format_and_normalize(builder, cell, cell_format):
    format_type, required, max_len = cell_format

    if cell == u'':
        if required:
            builder.raise_error(u'コマンドの引数が足りません')
        else:
            return cell

    if max_len != 0 and len(cell) > max_len:
        builder.raise_error(u'コマンドの引数の文字数が長すぎます（最大{}文字）'.format(max_len), cell)

    hankaku = False
    lower = False
    if 'expr' in format_type:
        try:
            expr = Expression.from_str(cell)
        except Exception as e:
            builder.raise_error(u'式が不正です: {} {}'.format(cell, unicode(e)), cell)
        return expr

    if 'variable' in format_type:
        hankaku = True
        lower = True
    if 'normalize' in format_type:
        hankaku = True
        lower = True
    if 'hankaku' in format_type:
        hankaku = True
    if 'label' in format_type:
        # label の可能性がある場合は # や * で始まっていたら半角化
        if RE_LABEL.match(cell):
            hankaku = True
    if 'number' in format_type:
        # number の可能性がある場合は正しい数字だったら半角化
        if RE_NUMBER.match(cell):
            hankaku = True
    if hankaku:
        cell = utility.to_hankaku(cell)
    if lower:
        cell = cell.lower()

    passed = False
    if 'raw' in format_type or 'text' in format_type or 'expr' in format_type or 'hankaku' in format_type:
        passed = True
    elif 'label' in format_type and RE_LABEL.match(cell):
        passed = True
    elif 'variable' in format_type and RE_VARIABLE.match(cell):
        passed = True
    elif 'image' in format_type and RE_IMAGE.match(cell):
        passed = True
    elif 'number' in format_type and RE_NUMBER.match(cell):
        passed = True

    if not passed:
        msgs = []
        if 'label' in format_type:
            msgs.append(u'ラベル')
        if 'number' in format_type:
            msgs.append(u'数字')
        if 'image' in format_type:
            msgs.append(u'画像')
        if 'variable' in format_type:
            msgs.append(u'$ で始まるフラグ名')
        builder.raise_error(u'{}を指定する必要があります'.format(u'か'.join(msgs)), cell)

    if 'label' not in format_type and RE_LABEL.match(cell):
        builder.raise_error(u'ラベルが指定できない引数にラベルを指定しています', cell)

    return cell


def invoke_builder(builder, node):
    row = node.term
    # コマンド判定は半角化して行う
    sender, msg = utility.parse_sender(utility.to_hankaku(row[0]))
    if not msg.startswith(u'@'):
        return False

    entry = get_command(msg, builder.version)
    if entry is None:
        return False
    if entry.builder is None:
        builder.raise_error(u'{} は builder が指定されていません'.format(msg))

    options = []
    children = []
    grandchildren = []

    # options のフォーマット確認と事前処理
    for i in range(len(entry.options)):
        if len(row) > 1+i:
            cell = row[1+i]
        else:
            cell = u''
        options.append(check_format_and_normalize(builder, cell, entry.options[i]))
        utility.remove_tail_empty_cells(options)

    # children のフォーマット確認と事前処理
    n_children = len(node.children)
    if n_children > 0 and entry.child is None:
        builder.raise_error(u'このコマンドに子要素を指定することはできません')
    if 'children_min' in entry.specs and n_children < entry.specs['children_min']:
        builder.raise_error(u'子要素は {} 個以上必要です'.format(entry.specs['children_min']))
    if 'children_max' in entry.specs and n_children > entry.specs['children_max']:
        builder.raise_error(u'子要素は {} 個以下でなくてはいけません'.format(entry.specs['children_max']))
    for i in range(n_children):
        child = []
        child_node = node.children[i]
        for j in range(len(entry.child)):
            if len(child_node.term) > j:
                cell = child_node.term[j]
            else:
                cell = u''
            child.append(check_format_and_normalize(builder, cell, entry.child[j]))
        utility.remove_tail_empty_cells(child)
        children.append(child)

        # 孫要素の確認
        n_grandchildren = len(child_node.children)
        if n_grandchildren > 0 and entry.grandchild is None:
            builder.raise_error(u'このコマンドに孫要素を指定することはできません')
        child_grandchild_list = []
        for j in range(n_grandchildren):
            grandchild = []
            grandchild_node = child_node.children[j]
            for k in range(len(entry.grandchild)):
                if len(grandchild_node.term) > k:
                    cell = grandchild_node.term[k]
                else:
                    cell = u''
                grandchild.append(check_format_and_normalize(builder, cell, entry.grandchild[k]))
            utility.remove_tail_empty_cells(grandchild)
            child_grandchild_list.append(grandchild)
        grandchildren.append(child_grandchild_list)

    if entry.grandchild:
        return entry.builder.build_from_command(builder, sender, msg, options, children, grandchildren)
    elif entry.child:
        return entry.builder.build_from_command(builder, sender, msg, options, children)
    else:
        return entry.builder.build_from_command(builder, sender, msg, options)


def invoke_runtime_run_command(context, sender, msg, options, children):
    if not msg.startswith(u'@'):
        return False
    entry = get_command(msg, version=context.version, service=context.service_name)
    if entry is None:
        return False
    if not hasattr(entry.runtime, 'run_command'):
        return False
    if entry.child:
        return entry.runtime.run_command(context, sender, msg, options, children)
    else:
        return entry.runtime.run_command(context, sender, msg, options)


def invoke_runtime_construct_response(context, sender, msg, options, children):
    if not msg.startswith(u'@'):
        return False
    entry = get_command(msg, version=context.version, service=context.service_name)
    if entry is None:
        return False
    if not hasattr(entry.runtime, 'construct_response'):
        return False
    if entry.child:
        return entry.runtime.construct_response(context, sender, msg, options, children)
    else:
        return entry.runtime.construct_response(context, sender, msg, options)


class ObjectEntry(object):
    def __init__(self, names, runtime=None, service='*'):
        """
        :param names: [u'ObjectName']
        :param runtime: SomePlugin_Runtime() or None
        :param service: 'line'
        """
        self.names = names
        self.runtime = runtime
        self.service = service


def register_object(entry):
    """オブジェクトカタログにオブジェクトを登録する。

    :param entry: 追加するオブジェクト
    :type entry: ObjectEntry
    """
    object_catalog.append(entry)
    for word in entry.names:
        l = object_catalog_map.get(word, [])
        l.append(entry)
        object_catalog_map[word] = l


def register_objects(entries):
    """オブジェクトカタログに複数のオブジェクトを登録する。

    :param entries: 追加するオブジェクト群
    :type entries: ObjectEntry[]
    """
    for entry in entries:
        register_object(entry)


def get_runtime_object_dictionary(service='*', context=None):
    """オブジェクトカタログから特定のサービス向けのオブジェクト表を生成する。

    :param service: フィルタするサービス, '*' でフィルタを行わない
    :param context: コンテキスト情報"""
    m = {}

    for entry in object_catalog:
        if service == '*' or entry.service == '*' or entry.service == service:
            for word in entry.names:
                m[word] = entry.runtime.get_runtime_object(word, context)
    return m


