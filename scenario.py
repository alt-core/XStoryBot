# coding: utf-8
import re
import logging
import string
from unicodedata import normalize
import hashlib
import pickle
import httplib2

from google.appengine.api import app_identity
from google.appengine.api import memcache
import cloudstorage

import utility
import convert_image
from models import ImageFileStatDB
import hub
import commands
from condition_expr import ConditionExpression
from expression import Expression


from common_commands import OR_CMDS, IF_CMDS, SEQ_CMDS, IMAGE_CMDS

INCLUDE_COND_CMDS = (u'@include', u'@読込')

BACK_JUMPS = (u'back', u'戻る')


BEFORE_LINE_0 = -1

CONDITION_KIND_STRING = 1
CONDITION_KIND_REGEXP = 2
CONDITION_KIND_EXPR = 3
CONDITION_KIND_COMMAND = 100

CONDITION_OPTION_REGEXP_NORMALIZE = 1
CONDITION_OPTION_REGEXP_LOWER_CASE = 2

http = httplib2.Http()
#http = httplib2.Http(cache=memcache) # memcache を使うと1MB制限が課される


class ScenarioSyntaxError(Exception):
    def __str__(self):
        return (u','.join(self.args)).encode('utf-8')

    def __unicode__(self):
        return u','.join(self.args)


# version 1 用の Guard
class Guard_V1(object):
    def __init__(self):
        self.terms = []

    @classmethod
    def from_str(cls, guard):
        self = cls()
        guard = normalize('NFKC', guard)
        terms = [x.strip() for x in guard.split(u',')]
        for term in terms:
            m = re.match(r'^([^=!\s]+)\s*(==|!=)\s*([\S]+)\s*$', term)
            if not m:
                logging.error(u"invalid guard syntax:" + guard)
                return None
            lhs = m.group(1)
            op = m.group(2)
            rhs = m.group(3)
            self.terms.append((op, lhs, rhs))
        return self

    def eval(self, env, match=[]):
        # 各項を and でつないだ条件
        # OR は @or でひとまずは何とか・・・
        # 項が１つもなければ True
        for op, lhs, rhs in self.terms:
            if lhs.startswith(u'$'):
                lhs = env.get(lhs, u'')
            if rhs.startswith(u'$'):
                rhs = env.get(rhs, u'')
            if op == u'==':
                if not lhs == rhs:
                    return False
            elif op == u'!=':
                if not lhs != rhs:
                    return False
        return True


class Guard_V2(object):
    def __init__(self):
        self.expr = None

    @classmethod
    def from_str(cls, s):
        self = cls()
        self.expr = Expression.from_str(s)
        return self

    def eval(self, env, matches=[]):
        return self.expr.eval(env, matches)


class Condition(object):
    def __init__(self, kind, value, guard=None, options=None):
        self.kind = kind
        self.value = value
        self.guard = guard
        self.options = options

    def check(self, action, env):
        if self.guard:
            if not self.guard.eval(env):
                return None

        if self.kind == CONDITION_KIND_STRING:
            return (action,) if self.value == action else None
        else:
            if utility.is_special_action(action):
                # postback は REGEXP にマッチさせない
                return None
            if self.kind == CONDITION_KIND_REGEXP:
                target_string = action
                if self.options and CONDITION_OPTION_REGEXP_NORMALIZE in self.options:
                    target_string = normalize('NFKC', target_string)
                if self.options and CONDITION_OPTION_REGEXP_LOWER_CASE in self.options:
                    target_string = target_string.lower()
                m = self.value.search(target_string)
                return (m.group(0),) + m.groups() if m else None
            elif self.kind == CONDITION_KIND_EXPR:
                return self.value.check(action)
            else:
                raise ValueError("invalid Condition: " + self.value)

    def is_command(self):
        return self.kind == CONDITION_KIND_COMMAND

    def is_condition(self):
        return self.kind == CONDITION_KIND_STRING or self.kind == CONDITION_KIND_REGEXP

    def is_label(self):
        return self.kind == CONDITION_KIND_STRING and self.value.startswith(u'#') and self.guard is None


class Block(object):
    def __init__(self, tab_name, sub_name=u''):
        self.tab_name = tab_name
        self.sub_name = sub_name
        self.indices = []


class Scene(object):
    def __init__(self, tab_name, sub_name=u'', line_no=0):
        self.tab_name = tab_name
        self.sub_name = sub_name
        self.line_no = line_no
        self.blocks = []

    def __str__(self):
        return self.get_fullpath().encode('utf-8')

    def __unicode__(self):
        return self.get_fullpath()

    def get_fullpath(self):
        return self.tab_name + u'/' + self.sub_name

    def get_relative_position_desc(self, node):
        if self.tab_name == node.tab_name:
            return u"{}_L{}".format(self.get_fullpath(), node.line_no - self.line_no)
        else:
            return u"{}__{}_L{}".format(self.get_fullpath(), node.tab_name, node.line_no)


class SyntaxTree(object):
    def __init__(self, tab_name, line, term):
        self.tab_name = tab_name
        self.line_no = line
        self.term = term
        self.children = []

    def get_factor(self, i):
        if i < len(self.term):
            return self.term[i]
        else:
            return u''

    def get_factors(self, i):
        return self.term[i:]

    def compaction(self):
        new_term = [factor for factor in self.term if factor]
        self.term = list(new_term)

    def normalize(self, *args):
        new_term = []
        for i, v in enumerate(self.term):
            if i in args:
                new_term.append(normalize('NFKC', v))
            else:
                new_term.append(v)
        self.term = list(new_term)

    def normalize_all(self):
        self.term = list([normalize('NFKC', v) for v in self.term])

    def dump(self, level=0):
        str = u'  '*level + unicode(self) + u"\n"
        if self.children:
            for child in self.children:
                str += child.dump(level+1)
        return str

    def __unicode__(self):
        msg = u', '.join([unicode(x) for x in self.term])
        msg += u' ＠{}!{}行目'.format(self.tab_name, self.line_no)
        return msg


class Command(object):
    base_name = None
    counter = {}

    @classmethod
    def generate_command_id(cls):
        # シナリオが変更されてもできるだけIDを維持できるように
        # ベースネームからの差分で管理している
        ret = u'{}__{}'.format(cls.base_name, cls.counter[cls.base_name])
        cls.counter[cls.base_name] += 1
        return ret

    @classmethod
    def set_base_name(cls, base_name):
        cls.base_name = base_name
        if base_name not in cls.counter:
            cls.counter[base_name] = 0

    def __init__(self, sender, msg, options, children, command_id = None):
        self.sender = sender
        self.msg = msg
        self.options = options
        self.children = children
        if command_id is None:
            command_id = Command.generate_command_id()
        self.command_id = command_id

    def is_normal_message(self):
        if re.match(r'^[@＠*＊#＃]', self.msg):
            return False
        return True


class Scenario(object):
    def __init__(self, version=1):
        self.scenes = {}
        self.startup_scene_title = None
        self.version = version

    @classmethod
    def load_from_uri(cls, uri):
        m = re.match(r'^https://storage.googleapis.com(/.+)$', uri)
        if m:
            filepath = m.group(1)
            try:
                logging.info(u'load scenario file: {}'.format(filepath))
                pickled_file = cloudstorage.open(filepath, 'r')
                self = pickle.load(pickled_file)
                pickled_file.close()
                return self
            except (cloudstorage.Error, pickle.PickleError) as e:
                raise ScenarioSyntaxError(u'シナリオのロードに失敗しました: {}, {}'.format(uri, unicode(e)))
        else:
            raise ScenarioSyntaxError(u'CloudStorage のファイルではありません')

    def save_to_storage(self):
        try:
            scenario_data = pickle.dumps(self)
            bucket_name = app_identity.get_default_gcs_bucket_name()
            file_digest = hashlib.md5(scenario_data).hexdigest()
            filepath = '/{}/scenario/{}'.format(bucket_name, file_digest)
            logging.info(u'save scenario file: {}'.format(filepath))
            scenario_file = cloudstorage.open(filepath, 'w', content_type='application/octet-stream', options={'x-goog-acl': 'public-read'})
            scenario_file.write(scenario_data)
            scenario_file.close()
            uri = u'https://storage.googleapis.com{}'.format(filepath)
            return uri
        except (cloudstorage.Error, pickle.PickleError):
            return None


class ScenarioBuilder(object):
    def __init__(self, options, version=1):
        self.scenario = Scenario(version)
        self.first_top_block = None
        self.image_file_read_cache = {}
        self.image_file_write_cache = {}
        self.bucket_name = app_identity.get_default_gcs_bucket_name()
        self.version = version

        self.options = options or {}
        if self.options.get('force') == True:
            self.option_force = True
            self.option_skip_image = False
        else:
            self.option_force = False
            self.option_skip_image = (self.options.get('skip_image') == True)

        self.node = None
        self.i_node = 0
        self.parent_node = None
        self.scene = None
        self.lines = None

    @classmethod
    def build_from_table(cls, table, options=None, version=1):
        self = cls(options, version)
        self._build_from_table(u'default', table)
        if not self.scenario.scenes:
            self.raise_error(u'シナリオには1つ以上のシーンを含んでいる必要があります')
        return self.scenario

    @classmethod
    def build_from_tables(cls, tables, options=None, version=1):
        self = cls(options, version)
        for tab_name, table in tables:
            # シート名は正規化する
            tab_name = normalize('NFKC', tab_name)
            self._build_from_table(tab_name, table)
        if not self.scenario.scenes:
            self.raise_error(u'シナリオには1つ以上のシーンを含んでいる必要があります')
        return self.scenario

    def _parse_sub_tree(self, node, table, tab_name, line_no, level, column_as_node_rule=False):
        first_line_no = line_no
        while line_no < len(table):
            row = table[line_no]

            # None を空白に置換（None が渡ってくることがあるかは不明）
            row = [cell if cell is not None else u'' for cell in row]
            # unicode 以外の物を unicode に変換（数値が直接渡ってくる場合がある）
            row = [cell if isinstance(cell, unicode) else unicode(cell) for cell in row]
            # 各セル内の末尾の空白文字を除去
            row = [cell.rstrip() for cell in row]
            # #@*%で始まっていたら先頭の空白も取り除いた上で半角化（正規化）する
            row = [normalize('NFKC', cell.strip()) if re.match(ur'^\s*[#＃@＠*＊%％]', cell) else cell for cell in row]

            # 空行・コメント行はスキップ
            if not row or row[0] == u'#':
                line_no += 1
                continue

            row_level = self.get_indent_level(row)
            if row_level < level:
                if first_line_no == line_no:
                    # column_as_node_rule == True が呼び出した _parse_sub_tree での1行目であり得る
                    if not row[level:]:
                        # 2行目以降が空欄の場合はスキップ
                        line_no += 1
                        continue
                    else:
                        pass
                else:
                    # サブツリーの終わりに達したので戻る
                    return line_no
            elif row_level > level:
                # さらに深いインデント
                if not node.children:
                    # インデントがおかしい
                    self.raise_error(u'テーブルの空白セルがおかしいです。', *row)
                # 子要素なのでサブツリーをパース
                line_no = self._parse_sub_tree(node.children[-1], table, tab_name, line_no, row_level)
                continue

            # 通常の子要素

            # 空のセルは右端から順に消す
            utility.remove_tail_empty_cells(row)

            child_node = SyntaxTree(tab_name, line_no, list(row[level:]))
            node.children.append(child_node)
            if column_as_node_rule:
                # 1列が1ノードレベルという扱い
                if len(row) > level + 1:
                    # 同じ行で次の列を子要素として評価
                    line_no = self._parse_sub_tree(child_node, table, tab_name, line_no, level+1)
                else:
                    line_no += 1
            else:
                line_no += 1

        # テーブルの最後まで読み込んだ
        return line_no


    def _get_relative_label(self, block, i_label, num):
        # ##__ で始まる num 個先のラベルを返す
        # num が 0 の場合はすぐ次の物を返す
        try:
            index = i_label + 1
            while True:
                cond, _ = block.indices[index]
                if cond.is_label() and cond.value.startswith(u'##__'):
                    num -= 1
                if num <= 0:
                    if not cond.is_label():
                        self.raise_error(u'相対指定された先がラベルではありません')
                        return None
                    return cond.value
                index += 1
        except IndexError:
            self.raise_error(u'相対指定された先が存在していません')
            return None

    def _fix_relative_label_sub(self, label, block, i_label):
        match = re.match(r'^##(\d+)?$', label)
        if match:
            num = int(match.group(1) or 1)
            new_label = self._get_relative_label(block, i_label, num)
            if new_label is not None:
               return True, new_label
        return False, label

    def _fix_relative_label_iter(self, cur_list, block, i_label):
        for index in range(len(cur_list)):
            if isinstance(cur_list[index], unicode):
                result, label = self._fix_relative_label_sub(cur_list[index], block, i_label)
                if result:
                    #print 'overwrite label: ' + cur_list[index] + ' -> ' + label
                    cur_list[index] = label
            elif isinstance(cur_list[index], list):
                self._fix_relative_label_iter(cur_list[index], block, i_label)
            elif isinstance(cur_list[index], Expression):
                pass
            else:
                self.raise_error(u'内部エラーが発生しました' + unicode(cur_list[index]))

    def _fix_relative_label(self):
        # 相対表記のラベルを正しい物に置き直す
        # TODO: 現在は全ての項目で ##n を探しているので、きちんと構文を解釈するようにする
        for scene in self.scenario.scenes.values():
            for block in scene.blocks:
                for i_label in range(len(block.indices)):
                    cond, lines = block.indices[i_label]
                    for command in lines:
                        result, label = self._fix_relative_label_sub(command.msg, block, i_label)
                        if result:
                            command.msg = label
                        if command.options:
                            self._fix_relative_label_iter(command.options, block, i_label)
                        if command.children:
                            self._fix_relative_label_iter(command.children, block, i_label)

    def add_command(self, sender, msg, options, children):
        self.lines.append(Command(sender, msg, options, children))

    def add_new_string_index(self, label):
        cond = Condition(CONDITION_KIND_STRING, label)
        self.lines = []
        self.block.indices.append((cond, self.lines))
        hub.invoke_all_builder_methods('callback_new_block', self, cond)

    def _build_from_table(self, tab_name, table):
        root = SyntaxTree(tab_name, 0, (u'**'+tab_name,))
        # ファイルの先頭に特殊な親ノードを貼る
        root.children.append(SyntaxTree(tab_name, 0, (u'*',)))
        self._parse_sub_tree(root, table, tab_name, 0, 0, column_as_node_rule=True)

        top_block = None
        scene_count = 0
        for self.node in root.children:
            cond_str = self.node.get_factor(0)

            if not cond_str:
                self.raise_error(u'internal parser error')

            if cond_str.startswith(u'@'):
                # コマンド＠条件セル
                if cond_str in INCLUDE_COND_CMDS:
                    msg = self.node.get_factor(1)
                    if not msg or not msg.startswith(u'*'):
                        # TODO: 正しいシーン名か validation する
                        self.raise_error(u'@include のあとにはシーンラベルを指定してください')
                else:
                    self.raise_error(u'不正なコマンドです')
                cond = Condition(CONDITION_KIND_COMMAND, cond_str, options=self.node.get_factors(1))
            elif cond_str.startswith(u'*'):
                # 新しいシーンを開始する
                sub_name = cond_str[1:]
                if u'/' in sub_name:
                    self.raise_error(u'シーン名に/を含むことはできません')
                self.scene = Scene(tab_name, sub_name, self.node.line_no)
                self.scenario.scenes[self.scene.get_fullpath()] = self.scene
                # 最初に定義されたシーンがスタートアップシーンとなる
                if not self.scenario.startup_scene_title:
                    self.scenario.startup_scene_title = self.scene.get_fullpath()

                self.block = Block(tab_name, sub_name)
                # トップブロックと最初のタブのトップブロックが常に include される
                self.scene.blocks.append(self.block)
                if top_block is None:
                    # table で先頭のブロック
                    if sub_name != u'':
                        self.raise_error(u'inernal parse error')
                    top_block = self.block
                    if self.first_top_block is None:
                        # 最初に設定した top_block が first_top_block
                        self.first_top_block = top_block
                        # ついでに first_top_block のみのシーンをデフォルトシーンに設定
                        self.scenario.scenes[u'*default'] = self.scene
                else:
                    self.scene.blocks.append(top_block)
                if self.first_top_block != top_block:
                    self.scene.blocks.append(self.first_top_block)
                scene_count += 1
                Command.set_base_name(self.scene.get_fullpath())
                cond = Condition(CONDITION_KIND_STRING, u'#')
            elif cond_str == u'##':
                # 無名インデックス
                cond = Condition(CONDITION_KIND_STRING, u'##__{}'.format(self.scene.get_relative_position_desc(self.node)))
            else:
                # 通常の条件セル
                guard = None
                if re.match(ur'^\s*(\[|［)', cond_str):
                    m = re.match(ur'^\s*(?:\[|［)((?:\\\]|\\］|[^\]］])*)(?:\]|］)[\s\n]*(.+)$', cond_str)
                    if m:
                        if self.version >= 2:
                            try:
                                guard = Guard_V2.from_str(m.group(1))
                            except Exception as e:
                                self.raise_error(u'条件指定が正しくありません: {} {}'.format(m.group(1), unicode(e)))
                        else:
                            guard = Guard_V1.from_str(m.group(1))
                            if guard is None:
                                self.raise_error(u'条件指定が正しくありません')
                        cond_str = m.group(2)
                    else:
                        self.raise_error(u'条件指定が正しくありません')
                if cond_str.startswith(u'#'):
                    # ラベル条件
                    cond = Condition(CONDITION_KIND_STRING, cond_str, guard=guard)
                elif self.version >= 2:
                    # version 2 以降は expr 対応
                    try:
                        expr = ConditionExpression.from_str(cond_str)
                    except Exception as e:
                        self.raise_error(u'条件指定が正しくありません: {} {}'.format(cond_str, unicode(e)))
                    cond = Condition(CONDITION_KIND_EXPR, expr, guard=guard)
                else:
                    m = re.match(r'^/(.*)/([iLN]*)?', cond_str)
                    if m:
                        # 正規表現条件
                        option_str = m.group(2)
                        regex_string = m.group(1)
                        regex_option = 0
                        condition_option = []
                        if option_str and u'i' in option_str:
                            regex_option = re.IGNORECASE
                        if option_str and u'L' in option_str:
                            condition_option.append(CONDITION_OPTION_REGEXP_LOWER_CASE)
                        if option_str and u'N' in option_str:
                            condition_option.append(CONDITION_OPTION_REGEXP_NORMALIZE)
                        regex = re.compile(regex_string, regex_option)
                        cond = Condition(CONDITION_KIND_REGEXP, regex, guard=guard, options=condition_option)
                    else:
                        # 一般条件
                        cond = Condition(CONDITION_KIND_STRING, cond_str, guard=guard)
            self.lines = []
            self.block.indices.append((cond, self.lines))
            # 新しい条件に来たので、メッセージ数カウンタを初期化する
            hub.invoke_all_builder_methods('callback_new_block', self, cond)

            self.parent_node = self.node
            for self.i_node in range(len(self.parent_node.children)):
                self.node = self.parent_node.children[self.i_node]
                if commands.invoke_builder(self, self.node):
                    pass
                else:
                    sender, msg = utility.parse_sender(self.node.get_factor(0))
                    options = self.node.get_factors(1)

                    if msg in IMAGE_CMDS or self.parse_imageurl(msg):
                        # 画像
                        if msg in IMAGE_CMDS:
                            if len(options) < 1:
                                self.raise_error(u'@imageには引数が1つ必要です')
                            s = options[0]
                        else:
                            s = msg
                        orig_url = self.parse_imageurl(s)
                        if orig_url is None:
                            orig_url = s
                        if orig_url is None or not orig_url.startswith(u'http'):
                            self.raise_error(u'@imageの第一引数は画像のURLである必要があります')
                        image_url, _ = self.build_image_for_image_command(orig_url)
                        self.add_command(sender, IMAGE_CMDS[0], [image_url,], None)

                    elif msg.startswith('@'):
                        self.raise_error(u'間違ったコマンドです')

                    else:
                        # 何の装飾もないテキスト
                        # プラグインでまず処理を試みる
                        msg = hub.filter_all_builder_methods('filter_plain_text', self, msg, options, sender)
                        if msg:
                            if hub.invoke_all_builder_methods('build_plain_text', self, sender, msg, options):
                                pass
                            else:
                                # 互換性のために残っているが、各プラグインの build_plain_text 内で add_command されるのが正しい
                                self.add_command(sender, msg, options, None)

                hub.invoke_all_builder_methods('callback_after_each_line', self)

        self.node = None # raise_error で古い node が表示されないようにする
        self._fix_relative_label()
        #print root.dump().encode('utf-8')


    def _make_imagemap_filepath(self, file_digest):
        file_format, digest = file_digest.split('_', 1)
        filepath = '/{}/imagemap/{}.{}'.format(self.bucket_name, digest, convert_image.get_ext_from_format(file_format))
        return filepath

    def _make_image_filepath(self, file_digest, resize_to):
        file_format, digest = file_digest.split('_', 1)
        filepath = '/{}/image/{}_{}.{}'.format(self.bucket_name, digest, str(resize_to), convert_image.get_ext_from_format(file_format))
        return filepath

    def _make_url_from_filepath(self, filepath):
        return u'https://storage.googleapis.com{}'.format(filepath)

    def build_image_for_imagemap_command(self, image_url):
        return self.build_image(image_url, 'imagemap')

    def build_image_for_image_command(self, image_url):
        return self.build_image(image_url, 'image')

    def build_image(self, image_url, kind):
        key = u'{}|{}'.format(kind, image_url)
        if key in self.image_file_read_cache:
            #logging.debug(u'skip load image (read cache): {}, {}'.format(image_url, kind))
            return self.image_file_read_cache[key]

        stat = ImageFileStatDB.get_cached_image_file_stat(kind, image_url)
        if self.option_skip_image:
            # skip image オプションが有効の場合、過去に変換したことのある URL は
            # 更新確認をせずにスキップする
            if stat:
                file_digest, size = stat
                if kind == 'imagemap':
                    #logging.debug(u'ImageFileStatDB has {}, so skip imagemap conversion: {}, {}'.format(image_url, file_digest, size))
                    result = self._make_url_from_filepath(self._make_imagemap_filepath(file_digest)), size
                else:
                    #logging.debug(u'ImageFileStatDB has {}, so skip image conversion: {}, {}'.format(image_url, file_digest, size))
                    result = self._make_url_from_filepath(self._make_image_filepath(file_digest, 1024)), size
                self.image_file_read_cache[key] = result
                return result
            #else:
            #    logging.debug(u'ImageFileStatDB does not have {}: {}'.format(image_url, stat))

        resp, content = None, None
        try:
            resp, content = http.request(image_url)
        except ValueError as e:
            self.raise_error(u'画像ファイルの読み込みに失敗しました。ファイルサイズなどをご確認ください。: {} {}'.format(image_url, str(e)))
        if resp.status != 200:
            self.raise_error(u'画像ファイルが読み込めません: {}'.format(image_url))
        image_format = convert_image.get_image_format(content)
        file_digest = '{}_{}'.format(image_format, hashlib.md5(content).hexdigest())

        if not self.option_force and stat is not None and file_digest == stat[0]:
            # ダイジェストが一致しているので保存を省略する
            size = stat[1]
            if kind == 'imagemap':
                #logging.debug(u'ImageFileStatDB has {}, and file_digest are same. so skip imagemap conversion: {}, {}'.format(image_url, file_digest, size))
                result = self._make_url_from_filepath(self._make_imagemap_filepath(file_digest)), size
            else:
                #logging.debug(u'ImageFileStatDB has {}, and file_digest are same. so skip image conversion: {}, {}'.format(image_url, file_digest, size))
                result = self._make_url_from_filepath(self._make_image_filepath(file_digest, 1024)), size
            self.image_file_read_cache[key] = result
            return result

        if kind == 'imagemap':
            url, size = self.build_image_for_imagemap_command_with_rawdata(content, file_digest=file_digest, logging_context=unicode(image_url))
        else:
            url, size = self.build_image_for_image_command_with_rawdata(content, file_digest=file_digest, logging_context=unicode(image_url))

        ImageFileStatDB.put_cached_image_file_stat(kind, image_url, file_digest, size)
        result = url, size
        self.image_file_read_cache[key] = result
        return result

    def build_image_for_imagemap_command_with_rawdata(self, orig_data, file_digest, logging_context=u''):
        filepath = self._make_imagemap_filepath(file_digest)
        size = None
        for resize_to in [240, 300, 460, 700, 1040]:
            result, size = self._resize_and_save_image_data(orig_data, resize_to, '{}/{}'.format(filepath, str(resize_to)), force_fit_width=True)
            if result is None:
                self.raise_error(u'画像ファイルが変換できませんでした: {}'.format(logging_context))
        # size は最後に変換した 1040 のものを返す
        url = self._make_url_from_filepath(filepath)
        return url, size

    def build_image_for_image_command_with_rawdata(self, orig_data, file_digest, logging_context=u''):
        result = None
        size = None
        for resize_to in [240, 1024]:
            filepath = self._make_image_filepath(file_digest, resize_to)
            result, size = self._resize_and_save_image_data(orig_data, resize_to, filepath, never_stretch=True)
            if result is None:
                self.raise_error(u'画像ファイルが変換できませんでした: {}'.format(logging_context))
        # result, size は最後に変換した 1024 のものを返す
        return result, size

    def _resize_and_save_image_data(self, orig_data, resize_to, filepath, force_fit_width=False, never_stretch=False):
        if filepath in self.image_file_write_cache:
            logging.debug(u'skip save image (write cache): {}, {}'.format(filepath, resize_to))
            return self.image_file_write_cache[filepath]

        image_data, image_format, size = convert_image.resize_image(orig_data, resize_to, force_fit_width=force_fit_width, never_stretch=never_stretch)
        if image_data is None:
            return None, None

        try:
            logging.info(u'save image file: {}'.format(filepath))
            image_file = cloudstorage.open(filepath, 'w', content_type=convert_image.get_content_type_from_format(image_format), options={'x-goog-acl': 'public-read'})
            image_file.write(image_data)
            image_file.close()
        except (IOError, cloudstorage.Error) as e:
            logging.error(u'ファイルの書き込みに失敗しました: {}'.format(unicode(e)))
            return None, None
        result = (self._make_url_from_filepath(filepath), size)
        self.image_file_write_cache[filepath] = result
        return result

    @staticmethod
    def get_indent_level(row):
        level = 0
        while level < len(row):
            if row[level]: break
            level += 1
        return level

    def raise_error(self, msg, *args):
        error_msg = msg
        for arg in args:
            error_msg += u'\n' + unicode(arg)
        if self.node:
            error_msg += u'\n' + unicode(self.node)
        raise ScenarioSyntaxError(error_msg)

    def assert_strlen(self, msg, maxlen, error_msg = None):
        if error_msg is None:
            error_msg = u'文字数制限（{}文字）'
        if len(msg) > maxlen:
            self.raise_error(error_msg.format(maxlen), msg)

    def assert_strlen_from_array(self, options, index, maxlen, error_msg = None):
        if len(options) > index:
            self.assert_strlen(options[index], maxlen, error_msg)
            return True
        return False

    def assert_imageurl(self, url, error_msg = None):
        if error_msg is None:
            error_msg = u'画像を指定すべきセルに違うものが指定されています'
        if not self.parse_imageurl(url):
            self.raise_error(error_msg, url)

    def assert_imageurl_from_array(self, options, index, error_msg = None):
        if len(options) > index and options[index] != u'':
            self.assert_imageurl(options[index], error_msg)
            return True
        return False

    def _build_and_replace_imageurl(self, options, index):
        if len(options) > index and options[index] != u'':
            orig_url = self.parse_imageurl(options[index])
            if orig_url is not None:
                image_url, _ = self.build_image_for_image_command(orig_url)
                options[index] = image_url
            return True
        return False

    def parse_imageurl(self, cell):
        m = re.match(r'^=IMAGE\("([^"]+)"\)', cell)
        if m:
            return m.group(1)
        else:
            return None


class Director(object):
    def __init__(self, scenario, context):
        self.scenario = scenario
        self.base_scene = None
        self.context = context
        self.vformat = string.Formatter().vformat
        self.flag_label_error = False

    def _get_scene(self, scene_title):
        if scene_title in self.scenario.scenes:
            return self.scenario.scenes[scene_title]
        return None

    def _get_scene_or_default(self, scene_title):
        if scene_title in self.scenario.scenes:
            return self.scenario.scenes[scene_title]
        else:
            # 指定されたシーンが無かった場合、同じタブのデフォルトシーンがあれば採用する
            scene = self.scenario.scenes.get(scene_title.split(u'/')[0] + u'/', None)
            if not scene:
                # タブ名すら見つからない場合はデフォルトシーンが用いられる
                scene = self.scenario.scenes[u'*default']
            return scene

    def search_index(self, scene, action):
        if scene is None or action is None:
            return None, None, None, None

        if action.startswith(u'*'):
            # シーンジャンプのアクションである
            m = re.match(r'^\*([^#]+)(#.*)?$', action)
            if not m:
                raise ValueError(u'cannot parse scene name: ' + action)
            scene_fullpath = m.group(1)
            action_tag = m.group(2)
            if scene_fullpath[0] == u'*' and scene_fullpath[1:] in BACK_JUMPS:
                # 呼び出し元に戻る特殊なジャンプ
                scene_fullpath = self.jump_back_scene()
                if scene_fullpath is None:
                    logging.error(u'cannot jump back')
                    return None, None, None, None
                next_scene = self._get_scene(scene_fullpath)
                if next_scene is None:
                    self.flag_label_error = True
                    return None, None, None, None
                self.base_scene = next_scene
            else:
                # 通常のシーンジャンプ
                if u'/' not in scene_fullpath:
                    # フルパスにするために現在のシーンの tab_name を補完する
                    scene_fullpath = self.base_scene.tab_name + u'/' + scene_fullpath
                next_scene = self._get_scene(scene_fullpath)
                if next_scene is None:
                    self.flag_label_error = True
                    return None, None, None, None
                self.base_scene = next_scene
                self.enter_new_scene(scene_fullpath)

            # このまま scene と action を読み替えて検索開始
            scene = self.base_scene
            if action_tag is not None:
                action = action_tag
            else:
                # '#' はシーンの先頭を表す特殊なインデックス
                action = u'#'

        for block in scene.blocks:
            result = self._search_index_sub(scene, block, action, [])
            if result[2] is not None:
                return result[0], result[1], result[2], result[3]

        if action.startswith(u'#'):
            # tag 指定の呼び出しだったのに見つからなかった
            self.flag_label_error = True

        return None, None, None, None

    def _search_index_sub(self, scene, block, action, visited_scene):
        #print action, scene.get_fullpath(), u",".join(visited_scene)
        #print "  BLOCK", block.tab_name, block.sub_name
        for n_lines, tup in enumerate(block.indices):
            cond, lines = tup
            #print scene.tab_name, cond, lines
            if cond.is_command() and cond.value in INCLUDE_COND_CMDS:
                # @include 処理
                if len(cond.options) < 1 or not cond.options[0].startswith(u'*'):
                    logging.error(u'invalid @include scene name')
                    return None, None, None, None
                scene_fullpath = cond.options[0][1:]
                if u'/' not in scene_fullpath:
                    # フルパスに
                    scene_fullpath = scene.tab_name + u'/' + scene_fullpath
                if scene_fullpath not in self.scenario.scenes:
                    logging.error(u'invalid @include scene name: ' + scene_fullpath)
                    return None, None, None, None
                elif scene_fullpath in visited_scene:
                    # 同じシーンを2回以上includeしようとしたらスキップ
                    pass
                else:
                    # include 処理
                    next_scene = self.scenario.scenes[scene_fullpath]
                    # include 先の block チェーンはたどらない
                    next_block = next_scene.blocks[0]
                    visited_scene.append(scene.get_fullpath())
                    result = self._search_index_sub(next_scene, next_block, action, visited_scene)
                    if result[2] is None:
                        # include 内では該当する処理がなかったので、続きへ
                        continue
                    else:
                        return result[0], result[1], result[2], result[3]
            else:
                match = cond.check(action, self.context.env)
                if match:
                    return scene, block, n_lines, match
        return None, None, None, None

    def format_value(self, value):
        try:
            result = self.vformat(value, self.context.env.matches, self.context.env)
        except (KeyError, IndexError) as e:
            logging.error('format error: ' + str(e))
            result = value
        return result

    def format_values(self, arr):
        try:
            result = [self.vformat(cell, self.context.env.matches, self.context.env) if isinstance(cell, unicode) else cell for cell in arr]
        except (KeyError, IndexError) as e:
            logging.error('format error: ' + str(e))
            result = arr
        return result

    def enter_new_scene(self, scene_title):
        if scene_title not in self.scenario.scenes:
            logging.info(u'指定されたシーン名が存在していません:' + scene_title)

        self.context.status.push_scene_history(self.context.status.scene)

        self.context.status.scene = scene_title
        self.context.status.renew_action_token()
        # TODO: action_token もスタックに積んでおいた方がいいのか考える
        #print u','.join(self.context.status.scene_history)

    def jump_back_scene(self):
        scene_title = self.context.status.pop_scene_history()
        if not scene_title:
            return None
        self.context.status.scene = scene_title
        self.context.status.renew_action_token()
        #print u','.join(self.context.status.scene_history)
        return scene_title

    def _plan_reaction_sub(self, scene, block, n_lines, match):
        if n_lines is None:
            return None

        cond, lines = block.indices[n_lines]
        for command in lines:
            if len(self.context.reactions) > 100:
                logging.error(u"reaction 処理内で無限ループを検出しました")
                break
            sender = command.sender
            msg = self.format_value(command.msg)
            options = command.options
            if options is None:
                options = ()
            options = self.format_values(options)
            if sender is None:
                sender_name = self.context.status.get(u'$$name', None)
                if sender_name is not None and sender_name != u'':
                    sender = sender_name
            row = [sender, msg]
            if options:
                row.extend(options)
            children = command.children
            if msg.startswith(u'@'):
                flag_handled = commands.invoke_runtime_run_command(self.context, sender, msg, options, children)
                if flag_handled:
                    continue

                if msg in OR_CMDS:
                    if len(block.indices) > n_lines+1:
                        return (scene, block, n_lines+1, match)
                    else:
                        return None
                elif msg in IF_CMDS:
                    if self.scenario.version >= 2:
                        expr = options[0]
                    else:
                        expr = Guard_V1.from_str(options[0])
                        if expr is None:
                            logging.error(u'条件指定が正しくありません: {}'.format(options[0]))
                            return None
                    if expr.eval(self.context.env, self.context.env.matches):
                        next_label = options[1]
                    else:
                        next_label = options[2]
                    return self.search_index(self.base_scene, next_label)
                # TODO: いずれは @seq もプラグインに
                elif msg in SEQ_CMDS:
                    node_seq = self.context.status.get(u'node.seq.' + scene.tab_name, {})
                    command_id = command.command_id
                    index = 0
                    if command_id in node_seq:
                        index = int(node_seq[command_id])
                    if index >= len(options):
                        index = len(options) - 1
                    node_seq[command_id] = unicode(index + 1)
                    self.context.status[u'node.seq.' + scene.tab_name] = node_seq
                    return self.search_index(self.base_scene, options[index])
            elif msg.startswith(u'*') or msg.startswith(u'#'):
                # ジャンプ
                return self.search_index(self.base_scene, msg)

            self.context.reactions.append((row, children))

        return None

    def plan_reactions(self):
        # シーン決定
        scene_title = self.context.status.scene
        if scene_title is None:
            # 初回アクセス
            scene_title = self.scenario.startup_scene_title
            self.enter_new_scene(scene_title)

        self.base_scene = self._get_scene_or_default(scene_title)

        # action の割り込み読み替え
        action = hub.filter_all_runtime_methods('modify_incoming_action', self.context, self.context.action)
        self.context.current_action = action

        if action is None:
            # 読み替えの結果 None になった
            return

        if 'action_token' in self.context.attrs:
            action_token = self.context.attrs['action_token']
            del self.context.attrs['action_token']
            if action_token != self.context.status.action_token:
                logging.info(u'action_token is not matched: {} != {}'.format(action_token, self.context.status.action_token))
                return

        # 実行行の取得
        scene, block, n_lines, match = self.search_index(self.base_scene, action)

        flag_error = False
        while True:
            if scene is None and not flag_error:
                # 実行すべきブロックの発見に失敗した
                if self.flag_label_error:
                    # ラベル指定があったのに見つけられなかった
                    logging.warning(u"ラベルを見つけられませんでした: {} @ {}".format(action, self.base_scene))
                    # ##error_invalid_label という特殊な action を発行する
                    scene, block, n_lines, match = self.search_index(self.base_scene, u"##error_invalid_label")
                else:
                    # 通常の文字指定で対応するブロックがなかった
                    # ##error_unhandled_action という特殊な action を発行する
                    scene, block, n_lines, match = self.search_index(self.base_scene, u"##error_unhandled_action")
                flag_error = True

            self.context.env.set_matches(match)
            new_context = self._plan_reaction_sub(scene, block, n_lines, match)
            self.context.env.clear_matches()
            if new_context is None:
                # reaction に結果が入っている場合と、何もすることが見つけられなかった場合がある
                break
            scene, block, n_lines, match = new_context

        return
