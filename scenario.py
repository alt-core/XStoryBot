import re
import logging
import string
from unicodedata import normalize
import hashlib
import pickle
import random
import requests
import json

import utility
import convert_image
from models import ImageFileStatDB, MediaFileStatDB
import hub
import commands
from cloud_backend import create_object_store
from cloud_backend.contracts import InvalidObjectReferenceError
from condition_expr import ConditionExpression
from expression import Expression
import expression


from common_commands import OR_CMDS, IF_CMDS, ELSE_CMDS, END_CMDS, SEQ_CMDS, LOOP_CMDS, RANDOM_CMDS, IMAGE_CMDS, CALL_CMDS, RETURN_CMDS, DEFER_CMDS

INCLUDE_REGION_CMDS = ('@include', '@読込')
INCLUDEIF_REGION_CMDS = ('@include_if', '@条件付読込')
FALLBACK_REGION_CMDS = ('@fallback', '@フォールバック')
TEMPLATE_REGION_CMDS = ('@template', '@テンプレート')
FILTER_REGION_CMDS = ('@filter', '@フィルタ')

BACK_JUMPS = ('back', '戻る') # v2 までの互換性のため


BEFORE_LINE_0 = -1

CONDITION_KIND_STRING = 1
CONDITION_KIND_REGEXP = 2
CONDITION_KIND_EXPR = 3
CONDITION_KIND_COMMAND = 100

CONDITION_OPTION_REGEXP_NORMALIZE = 1
CONDITION_OPTION_REGEXP_LOWER_CASE = 2

CALL_ARGS_MAX = 15 # @call で渡せる引数の最大数

# ObjectStoreの初期化は、従来と同じくscenarioのimport時に行う。
object_store = create_object_store()


class ScenarioSyntaxError(Exception):
    def __str__(self):
        return ','.join(str(arg) for arg in self.args)


# version 1 用の Guard
class Guard_V1:
    def __init__(self):
        self.terms = []

    @classmethod
    def from_str(cls, guard):
        self = cls()
        guard = normalize('NFKC', guard)
        terms = [x.strip() for x in guard.split(',')]
        for term in terms:
            m = re.match(r'^([^=!\s]+)\s*(==|!=)\s*([\S]+)\s*$', term)
            if not m:
                logging.error("invalid guard syntax:" + guard)
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
            if lhs.startswith('$'):
                lhs = env.get(lhs, '')
            if rhs.startswith('$'):
                rhs = env.get(rhs, '')
            if op == '==':
                if not lhs == rhs:
                    return False
            elif op == '!=':
                if not lhs != rhs:
                    return False
        return True


class Guard_V2:
    def __init__(self):
        self.expr = None

    @classmethod
    def from_str(cls, s):
        self = cls()
        self.expr = Expression.from_str(s)
        self._s = s
        return self

    def eval(self, env, matches=[]):
        return self.expr.eval(env, matches)

    def __repr__(self):
        return f'Guard({self._s})'


class Condition:
    def __init__(self, kind, value, guards=None, options=None):
        self.kind = kind
        self.value = value
        self.guards = guards
        self.options = options

    def check(self, action, env):
        if self.guards:
            for guard in self.guards:
                if not guard.eval(env):
                    return None

        if self.kind == CONDITION_KIND_STRING:
            return (action,) if self.value == action else None
        elif self.kind == CONDITION_KIND_COMMAND:
            return (action,)
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
        return self.kind == CONDITION_KIND_STRING and self.value.startswith('#')

    def __repr__(self):
        return f'Condition({self.kind}, {self.value}, {self.guards})'


class Region:
    def __init__(self, tab_name, sub_name=''):
        self.tab_name = tab_name
        self.sub_name = sub_name
        self.blocks = []

    def get_fullpath(self):
        return f'{self.tab_name}/{self.sub_name}'

    def __repr__(self):
        block_repr = '\n'.join([f'  {repr(block)}' for block in self.blocks])
        return f'Region({self.tab_name}/{self.sub_name})\n{block_repr}'


class Scene:
    def __init__(self, tab_name, sub_name='', line_no=0):
        self.tab_name = tab_name
        self.sub_name = sub_name
        self.line_no = line_no
        self.base_region = None
        self.regions = []
        self._default_fallback_scene_name = None
        self._template_name = None

    def __str__(self):
        return self.get_fullpath()

    def get_fullpath(self):
        return f'{self.tab_name}/{self.sub_name}'

    def get_relative_position_desc(self, node):
        if self.tab_name == node.tab_name:
            return f"{self.get_fullpath()}_L{node.line_no - self.line_no}"
        else:
            return f"{self.get_fullpath()}__{node.tab_name}_L{node.line_no}"

    def get_entrypoint_label(self):
        return f'###_{self.get_fullpath()}_entrypoint'


class SyntaxTree:
    def __init__(self, tab_name, line, term):
        self.tab_name = tab_name
        self.line_no = line
        self.term = term
        self.children = []

    def get_factor(self, i):
        if i < len(self.term):
            return self.term[i]
        else:
            return ''

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
        s = '  '*level + str(self) + "\n"
        if self.children:
            for child in self.children:
                s += child.dump(level+1)
        return s

    def __str__(self):
        msg = ', '.join([str(x) for x in self.term])
        msg += f' ＠{self.tab_name}!{self.line_no}行目'
        return msg


class Command:
    base_name = None
    counter = {}

    @classmethod
    def generate_command_id(cls):
        # シナリオが変更されてもできるだけIDを維持できるように
        # ベースネームからの差分で管理している
        ret = f'{cls.base_name}__{cls.counter[cls.base_name]}'
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

    def __repr__(self):
        return f'Command({self.msg}, {self.options}, {self.sender}, {self.children})'


class Scenario:
    def __init__(self, version=1):
        self.scenes = {}
        self.startup_scene_title = None
        self.version = version
        self.constants = {}

    @classmethod
    def load_from_uri(cls, uri):
        try:
            data = object_store.load_scenario(uri)
            self = pickle.loads(data)
            return self
        except InvalidObjectReferenceError as e:
            raise ScenarioSyntaxError(str(e))
        except Exception as e:
            raise ScenarioSyntaxError(f'シナリオのロードに失敗しました: {uri}, {str(e)}')

    def save_to_storage(self):
        try:
            scenario_data = pickle.dumps(self)
            file_digest = hashlib.md5(scenario_data).hexdigest()
            blob_name = f'scenario/{file_digest}'
            logging.info(f'save scenario file: {blob_name}')
            return object_store.store_scenario(blob_name, scenario_data)
        except Exception as e:
            raise ScenarioSyntaxError(f'シナリオの保存に失敗しました: {str(e)}')


class ScenarioBuilder:
    def __init__(self, options, version=1):
        self.scenario = Scenario(version)
        self.first_top_scene = None
        self.image_file_read_cache = {}
        self.image_file_write_cache = {}
        self.version = version
        expression.set_version(self.version)

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

        self.filters = []

        self.control_flow_labels = {}
        self.control_flow_stack = []
        self.auto_labels = {}
        self.include_check_list = []
        self.fallback_backpatching = {}

    @classmethod
    def build_from_table(cls, table, constants=None, options=None, version=1):
        if constants is None:
            constants = {}
        self = cls(options, version)
        self._build_from_table('default', table)
        if self.version >= 3:
            self._fixup_build_v3()
        else:
            self._fixup_build_v2()
        if not self.scenario.scenes:
            self.raise_error('シナリオには1つ以上のシーンを含んでいる必要があります')
        self.scenario.constants = constants
        return self.scenario

    @classmethod
    def build_from_tables(cls, tables, constants=None, options=None, version=1):
        if constants is None:
            constants = {}
        self = cls(options, version)
        for tab_name, table in tables:
            # シート名は正規化する
            tab_name = normalize('NFKC', tab_name)
            self._build_from_table(tab_name, table)
        if self.version >= 3:
            self._fixup_build_v3()
        else:
            self._fixup_build_v2()
        if not self.scenario.scenes:
            self.raise_error('シナリオには1つ以上のシーンを含んでいる必要があります')
        self.scenario.constants = constants
        return self.scenario

    def _parse_sub_tree(self, node, table, tab_name, line_no, level, column_as_node_rule=False):
        first_line_no = line_no
        while line_no < len(table):
            row = table[line_no]

            # None を空白に置換（None が渡ってくることがあるかは不明）
            row = [cell if cell is not None else '' for cell in row]
            # unicode 以外の物を unicode に変換（数値が直接渡ってくる場合がある）
            row = [cell if isinstance(cell, str) else str(cell) for cell in row]
            # 各セル内の末尾の空白文字を除去
            row = [cell.rstrip() for cell in row]
            # #@*%;で始まっていたら先頭の空白も取り除いた上で半角化（正規化）する
            row = [normalize('NFKC', cell.strip()) if re.match(r'^\s*[#＃@＠*＊%％;；]', cell) else cell for cell in row]

            # 空行・コメント行はスキップ
            if not row or row[0] == '#' or row[0] == ';':
                line_no += 1
                continue

            # TODO: 時間があるときに @fallback のブロック問題に対処する
            # if row[0].startswith('@') and level > 0:
            #     # リージョンオペレータは行単位で引数に持って行かれるので level 1 以降はスキップ
            #     line_no += 1
            #     continue

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
                    self.raise_error('テーブルの空白セルがおかしいです。', *row)
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

    def _build_from_table(self, tab_name, table):
        logging.info(f'build scenario from table: {tab_name}')
        root = SyntaxTree(tab_name, 0, ('**'+tab_name,))
        # ファイルの先頭に特殊な親ノードを貼る
        root.children.append(SyntaxTree(tab_name, 0, ('*',)))
        self._parse_sub_tree(root, table, tab_name, 0, 0, column_as_node_rule=True)

        self.top_scene = None
        self.region = None
        for self.node in root.children:
            cond_str = self.node.get_factor(0)
            if not cond_str:
                self.raise_error('internal parser error')

            cond = None
            if cond_str.startswith('*'):
                # 新しいリージョンに入る
                cond = self._enter_new_region(tab_name, sub_name=cond_str[1:])
            elif cond_str.startswith('@'):
                # リージョンオペレータの処理 (@include, @fallback など)
                cond = self._process_region_operator(cond_str)
                if cond is None:
                    # TODO: @fallback では cond が None で返ってくるが、
                    # ここで continue してしまうと @fallback のあとの行に処理が続かない
                    continue
            else:
                # 通常の条件セル
                cond = self._process_block_condition(cond_str)

            # TODO: @fallback では cond が None で返ってくるが、
            # その場合、直前の設定で処理が継続するようにしたい
            self._process_block_body(cond)

        region = self.region
        self._leave_region()

        #print(root.dump())

    def _enter_new_region(self, tab_name, sub_name):
        self._leave_region()
        self.control_flow_labels = {}
        self.control_flow_stack = []
        self.auto_labels = {}

        # 新しいシーンを開始する
        if '/' in sub_name:
            self.raise_error('シーン名に/を含むことはできません')
        logging.info(f'new scene: {tab_name}/{sub_name}')
        self.scene = Scene(tab_name, sub_name, self.node.line_no)
        self.scenario.scenes[self.scene.get_fullpath()] = self.scene
        # 最初に定義されたシーンがスタートアップシーンとなる
        if not self.scenario.startup_scene_title:
            self.scenario.startup_scene_title = self.scene.get_fullpath()

        self.region = Region(tab_name, sub_name)
        self.scene.base_region = self.region
        self.filters = [] # リージョンが変わるとフィルタもリセット
        if self.top_scene is None:
            # table で先頭のリージョン
            if sub_name != '':
                self.raise_error('inernal parse error')
            self.top_scene = self.scene
            self.scene._template_name = None
            if self.first_top_scene is None:
                # 最初に設定した top_scene が first_top_scene
                self.first_top_scene = self.top_scene
                # ついでに first_top_scene のみのシーンをデフォルトシーンに設定
                self.scenario.scenes['*default'] = self.scene
        else:
            if self.version >= 3:
                # トップシーン以外のテンプレートは各シートのトップシーン
                self.scene._template_name = self.top_scene.get_fullpath()
            else:
                self.scene._default_fallback_scene_name = self.top_scene.get_fullpath()

        Command.set_base_name(self.scene.get_fullpath())
        return Condition(CONDITION_KIND_STRING, self.scene.get_entrypoint_label())

    def _leave_region(self):
        #print(self.region)
        if self.region is None:
            return
        # # /end されていないコントロールフローがあれば、閉じる
        # while self.control_flow_stack:
        #     self.add_new_control_flow_block()
        #     self.control_flow_stack.pop()
        if self.control_flow_stack:
            self.raise_error('/end の数が合っていません')
        self._backpatch_relative_label()
        self.region = None

    def _process_region_operator(self, cond_str):
        # コマンド＠条件セル
        if cond_str in TEMPLATE_REGION_CMDS:
            if self.version < 3:
                self.raise_error('@template は version 3 以降でのみ使用できます')
            if self.first_top_scene == self.scene:
                self.raise_error('first top scene には @template は指定できません')
            msg = self.node.get_factor(1)
            if not msg or msg == "":
                self.scene._template_name = None
                return None
            if not msg.startswith('*'):
                self.raise_error('@template のあとにはシーンラベルを指定してください')
            scene_fullpath = msg[1:]
            if '/' not in scene_fullpath:
                # フルパスに
                scene_fullpath = self.scene.tab_name + '/' + scene_fullpath
            self.scene._template_name = scene_fullpath
            return None
        elif cond_str in INCLUDE_REGION_CMDS:
            msg = self.node.get_factor(1)
            if not msg or not msg.startswith('*'):
                self.raise_error('@include のあとにはシーンラベルを指定してください')
            scene_fullpath = msg[1:]
            if '/' not in scene_fullpath:
                # フルパスに
                scene_fullpath = self.scene.tab_name + '/' + scene_fullpath
            self.include_check_list.append((scene_fullpath, str(self.node)))
            return Condition(CONDITION_KIND_COMMAND, cond_str, options=[f'*{scene_fullpath}'], guards=self.filters)
        elif cond_str in INCLUDEIF_REGION_CMDS:
            expr_str = self.node.get_factor(1)
            label = self.node.get_factor(2)
            try:
                expr = Expression.from_str(expr_str)
            except Exception as e:
                self.raise_error('式が不正です: {} {}'.format(expr_str, str(e)))
            if not label or not label.startswith('*'):
                self.raise_error('@includeif のあとには 条件, シーンラベル を指定してください')
            scene_fullpath = label[1:]
            if '/' not in scene_fullpath:
                # フルパスに
                scene_fullpath = self.scene.tab_name + '/' + scene_fullpath
            elif scene_fullpath.startswith('./'):
                # 相対パスの解決
                scene_fullpath = f'{self.scene.tab_name}/{scene_fullpath[2:]}'
            self.include_check_list.append((scene_fullpath, str(self.node)))
            return Condition(CONDITION_KIND_COMMAND, cond_str, options=[expr, f'*{scene_fullpath}'], guards=self.filters)
        elif cond_str in FALLBACK_REGION_CMDS:
            if self.version < 3:
                self.raise_error('@fallback は version 3 以降でのみ使用できます')
            names = []
            for label in self.node.get_factors(1):
                if not label or not label.startswith('*'):
                    self.raise_error('@fallback のあとにはシーンラベルを指定してください')
                scene_fullpath = label[1:]
                if '/' not in scene_fullpath:
                    # フルパスに
                    scene_fullpath = self.scene.tab_name + '/' + scene_fullpath
                elif scene_fullpath.startswith('./'):
                    # 相対パスの解決
                    scene_fullpath = f'{self.scene.tab_name}/{scene_fullpath[2:]}'
                names.append(scene_fullpath)
            if self.scene not in self.fallback_backpatching:
                self.fallback_backpatching[self.scene] = []
            self.fallback_backpatching[self.scene].append((names, str(self.node)))
            # @fallback を指定されたら、暗黙のデフォルトフォールバックは無効に
            self.scene._default_fallback_scene_name = None
            return None
        elif cond_str in FILTER_REGION_CMDS:
            if self.version < 3:
                self.raise_error('@filter は version 3 以降でのみ使用できます')
            guard_expr = self.node.get_factor(1)
            if guard_expr is not None and guard_expr != '':
                try:
                    guard = Guard_V2.from_str(guard_expr)
                except Exception as e:
                    self.raise_error('条件指定が正しくありません: {} {}'.format(guard_expr, str(e)))
                self.filters = [guard] # filter 条件は上書き
            else:
                self.filters = []
            return None
        else:
            self.raise_error('不正なコマンドです')

    def _extract_guard_expr(self, s):
        s = s.lstrip()
        if not s or s[0] not in ('[', '［'):
            return None, s
        depth = 1
        i = 1  # 開いているところからスタート
        result = []
        while i < len(s):
            c = s[i]
            # エスケープ対応
            if c == '\\' and i + 1 < len(s):
                result.append(s[i:i+2])
                i += 2
                continue
            if c in ('[', '［'):
                depth += 1
                result.append(c)
            elif c in (']', '］'):
                depth -= 1
                if depth == 0:
                    i += 1
                    break
                else:
                    result.append(c)
            else:
                result.append(c)
            i += 1
        if depth != 0:
            self.raise_error('条件指定で [] の対応が合っていません')
        guard_expr = ''.join(result)
        remaining = s[i:].lstrip(' \n')
        return guard_expr, remaining

    def _process_block_condition(self, cond_str):
        if cond_str == '##':
            # 無名インデックス
            return self.create_anonymous_block_cond()

        # 通常の条件セル

        # ガード部の解釈
        guards = self.filters.copy()
        if cond_str.lstrip().startswith(('[', '［')):
            try:
                guard_expr, cond_str = self._extract_guard_expr(cond_str)
            except Exception as e:
                self.raise_error(str(e))
            if self.version >= 2:
                try:
                    guard = Guard_V2.from_str(guard_expr)
                except Exception as e:
                    self.raise_error('条件指定が正しくありません: {} {}'.format(guard_expr, str(e)))
            else:
                guard = Guard_V1.from_str(guard_expr)
                if guard is None:
                    self.raise_error('条件指定が正しくありません')
            guards.append(guard)

        # 条件部の解釈
        if cond_str.startswith('#'):
            # ラベル条件
            cond = Condition(CONDITION_KIND_STRING, cond_str, guards=guards)
        elif self.version >= 2:
            # version 2 以降は expr 対応
            try:
                expr = ConditionExpression.from_str(cond_str)
            except Exception as e:
                self.raise_error('条件指定が正しくありません: {} {}'.format(cond_str, str(e)))
            cond = Condition(CONDITION_KIND_EXPR, expr, guards=guards)
        else:
            m = re.match(r'^/(.*)/([iLN]*)?', cond_str)
            if m:
                # 正規表現条件
                option_str = m.group(2)
                regex_string = m.group(1)
                regex_option = 0
                condition_option = []
                if option_str and 'i' in option_str:
                    regex_option = re.IGNORECASE
                if option_str and 'L' in option_str:
                    condition_option.append(CONDITION_OPTION_REGEXP_LOWER_CASE)
                if option_str and 'N' in option_str:
                    condition_option.append(CONDITION_OPTION_REGEXP_NORMALIZE)
                regex = re.compile(regex_string, regex_option)
                cond = Condition(CONDITION_KIND_REGEXP, regex, guards=guards, options=condition_option)
            else:
                # 一般条件
                cond = Condition(CONDITION_KIND_STRING, cond_str, guards=guards)
        return cond

    def _process_block_body(self, cond=None):
        # cond が None の場合は直前のブロックの処理を継続する
        if cond is not None:
            self.lines = []
            block = (cond, self.lines)
            self.region.blocks.append(block)
            # 新しい条件に来たので、メッセージ数カウンタを初期化する
            hub.invoke_all_builder_methods('callback_new_block', self, cond)

        self.parent_node = self.node
        for self.i_node, self.node in enumerate(self.parent_node.children):
            self._process_command()

    def _process_command(self):
        command_entry, sender, msg, options, children, grandchildren = commands.parse_command(self, self.node)
        if command_entry:
            if commands.invoke_builder(self, command_entry, sender, msg, options, children, grandchildren):
                pass
            else:
                self.raise_error('間違ったコマンドです')

        else:
            # 何の装飾もないテキスト

            # =IMAGE() の処理
            # TODO: build_plain_text でやる
            orig_url = self.parse_imageurl(msg, parse_plain_url=False)
            if orig_url:
                image_url, _ = self.build_image_for_image_command(orig_url)
                self.add_command(sender, IMAGE_CMDS[0], [image_url,], None)

            else:
                # プラグインでまず処理を試みる
                msg = hub.filter_all_builder_methods('filter_plain_text', self, msg, options, sender)
                if msg:
                    if hub.invoke_all_builder_methods('build_plain_text', self, sender, msg, options):
                        pass
                    else:
                        # 互換性のために残っているが、各プラグインの build_plain_text 内で add_command されるのが正しい
                        self.add_command(sender, msg, options, None)

        hub.invoke_all_builder_methods('callback_after_each_line', self)

    def _get_relative_label(self, region, i_label, num):
        # ##__ で始まる num 個先のラベルを返す
        # num が 0 の場合はすぐ次の物を返す
        try:
            index = i_label + 1
            while True:
                cond, _ = region.blocks[index]
                if cond.is_label() and cond.value.startswith('##__'):
                    num -= 1
                if num <= 0:
                    if not cond.is_label():
                        self.raise_error('相対指定された先がラベルではありません')
                        return None
                    return cond.value
                index += 1
        except IndexError:
            self.raise_error('相対指定された先が存在していません')
            return None

    def _backpatch_relative_label_sub(self, label, region, i_label):
        match = re.match(r'^##(\d+)?$', label)
        if match:
            num = int(match.group(1) or 1)
            new_label = self._get_relative_label(region, i_label, num)
            if new_label is not None:
               return True, new_label
        match = re.match(r'^#\+__(.*)_-_(.+)$', label)
        if match:
            base_name = match.group(1)
            param = match.group(2)
            if param == '*':
                # 最後はフローから脱出するラベルなので返さない
                new_label = self.control_flow_labels.get(base_name, [None])[0:-1]
            else:
                num = int(match.group(2))
                try:
                    new_label = self.control_flow_labels.get(base_name, [None])[num]
                except IndexError:
                    self.raise_error('相対指定された先が存在していません\n/else, /end を確認してください')
            if new_label is not None:
               return True, new_label
        return False, label

    def _backpatch_relative_label_iter(self, cur_list, region, i_label):
        for index in range(len(cur_list)):
            if isinstance(cur_list[index], str):
                result, label = self._backpatch_relative_label_sub(cur_list[index], region, i_label)
                if result:
                    #print('overwrite label: ' + cur_list[index] + ' -> ' + label)
                    if isinstance(label, list):
                        # cur_list を index の位置から label で置き換える
                        cur_list[index:index+1] = label
                    else:
                        cur_list[index] = label
            elif isinstance(cur_list[index], list):
                self._backpatch_relative_label_iter(cur_list[index], region, i_label)
            elif isinstance(cur_list[index], Expression):
                pass
            else:
                self.raise_error('内部エラーが発生しました' + str(cur_list[index]))

    def _backpatch_relative_label(self):
        # 相対表記のラベルを正しい物に置き直す
        # TODO: 現在は全ての項目で ##n と ##+, ##* を探しているので、きちんと構文を解釈するようにする
        region = self.region
        for i_label in range(len(region.blocks)):
            cond, lines = region.blocks[i_label]
            for command in lines:
                result, label = self._backpatch_relative_label_sub(command.msg, region, i_label)
                if result:
                    if isinstance(label, list):
                        self.raise_error('内部エラーが発生しました')
                    command.msg = label
                if command.options:
                    self._backpatch_relative_label_iter(command.options, region, i_label)
                if command.children:
                    self._backpatch_relative_label_iter(command.children, region, i_label)

    def add_command(self, sender, msg, options, children):
        self.lines.append(Command(sender, msg, options, children))

    def add_new_string_block(self, label):
        # ユニークなラベルを作る際に使用されるものなので、self.filters はガードに指定しない
        cond = Condition(CONDITION_KIND_STRING, label)
        self.lines = []
        self.region.blocks.append((cond, self.lines))
        hub.invoke_all_builder_methods('callback_new_block', self, cond)

    def make_internal_label(self, param='label'):
        return f'###__{self.scene.get_relative_position_desc(self.node)}__{param}'

    def make_unique_auto_label(self, auto_label):
        if auto_label in self.auto_labels:
            self.auto_labels[auto_label] += 1
            return f'{auto_label}_-_{self.auto_labels[auto_label]}'
        else:
            self.auto_labels[auto_label] = 1
            return auto_label

    def create_anonymous_block_cond(self):
        label = self.make_unique_auto_label(f'##__{self.scene.get_relative_position_desc(self.node)}')
        return Condition(CONDITION_KIND_STRING, label)

    def add_new_anonymous_block(self):
        cond = self.create_anonymous_block_cond()
        self.lines = []
        self.region.blocks.append((cond, self.lines))
        hub.invoke_all_builder_methods('callback_new_block', self, cond)

    def start_control_flow(self, kind):
        base_name = self.scene.get_relative_position_desc(self.node)
        self.control_flow_stack.append((base_name, kind))
        self.control_flow_labels[base_name] = []
        return base_name

    def end_control_flow(self):
        self.control_flow_stack.pop()

    def get_current_control_flow_base_name(self):
        if len(self.control_flow_stack) == 0:
            return None
        return self.control_flow_stack[-1][0]

    def get_current_control_flow_kind(self):
        if len(self.control_flow_stack) == 0:
            return None
        return self.control_flow_stack[-1][1]

    def get_current_control_flow_index(self):
        if len(self.control_flow_stack) == 0:
            return None
        base_name = self.get_current_control_flow_base_name()
        return len(self.control_flow_labels[base_name])

    def make_control_flow_refernce_label(self, param='*'):
        # param='*' は解決時に全てのラベルのリストが挿入される
        if len(self.control_flow_stack) == 0:
            self.raise_error('/else, /elif, /end の数が合っていません')
        base_name = self.get_current_control_flow_base_name()
        return f'#+__{base_name}_-_{param}'

    def create_control_flow_block_cond(self):
        label = self.make_unique_auto_label(f'#-__{self.scene.get_relative_position_desc(self.node)}')
        return Condition(CONDITION_KIND_STRING, label)

    def add_new_control_flow_block(self):
        base_name = self.get_current_control_flow_base_name()
        cond = self.create_control_flow_block_cond()
        self.lines = []
        self.region.blocks.append((cond, self.lines))
        self.control_flow_labels[base_name].append(cond.value)
        hub.invoke_all_builder_methods('callback_new_block', self, cond)

    def _fixup_build_v2(self):
        # include されたラベルのチェック
        for scene_label, node_str in self.include_check_list:
            if scene_label not in self.scenario.scenes:
                raise ScenarioSyntaxError(f'include されたシーンが存在しません: {scene_label}\n{node_str}')
        for scene in self.scenario.scenes.values():
            # v2 では、主たるシーン、シートのトップシーン、スタートアップシーンの順にリージョンを設定する
            visited_scenes = set([scene])
            regions = [scene.base_region]
            if scene._default_fallback_scene_name is not None:
                default_fallback_scene = self.scenario.scenes[scene._default_fallback_scene_name]
                visited_scenes.add(default_fallback_scene)
                regions.append(default_fallback_scene.base_region)
            if self.first_top_scene is not None and self.first_top_scene not in visited_scenes:
                regions.append(self.first_top_scene.base_region)
            scene.regions = regions
            #print(f'regions: {scene.get_fullpath()} => {", ".join(region.get_fullpath() for region in regions)}')

    def _fixup_build_v3(self):
        # include されたラベルのチェック
        for scene_label, node_str in self.include_check_list:
            if scene_label not in self.scenario.scenes:
                raise ScenarioSyntaxError(f'include されたシーンが存在しません: {scene_label}\n{node_str}')
        # fallback されたラベルのチェック
        for scene, value in self.fallback_backpatching.items():
            for fallback_scene_names, node_str in value:
                for fallback_scene_name in fallback_scene_names:
                    if fallback_scene_name not in self.scenario.scenes:
                        self.raise_error(f'fallback されたラベルが存在しません: {fallback_scene_name}\n{node_str}')
        # 各シーンのリージョンを解決する
        for scene in self.scenario.scenes.values():
            visited_scenes = set()
            all_scenes_in_chain = []

            # template の解決
            current_scene = scene
            template_name = current_scene._template_name
            while template_name is not None:
                if template_name not in self.scenario.scenes:
                    raise ScenarioSyntaxError(f'template 先のシーンが存在しません: {template_name}\n{current_scene.get_fullpath()}')
                template = self.scenario.scenes[template_name]
                if template in visited_scenes:
                    break
                visited_scenes.add(template)
                all_scenes_in_chain.insert(0, template)
                current_scene = template
                template_name = current_scene._template_name

            # シーン自身のリージョンを追加
            if scene not in visited_scenes:
                all_scenes_in_chain.append(scene)

            regions = []
            for s_in_chain in all_scenes_in_chain:
                regions.append(s_in_chain.base_region)
            for s_in_chain in reversed(all_scenes_in_chain):
                self._backpatch_fallback_iter(regions, s_in_chain, visited_scenes)

            scene.regions = regions
            #print(f'regions: {scene.get_fullpath()} => {", ".join(region.get_fullpath() for region in regions)}')

    def _backpatch_fallback_iter(self, regions, scene, visited_scenes):
        fallback_scenes = self.fallback_backpatching.get(scene, None)
        if fallback_scenes is None:
            return
        for fallback_scene_names, _ in reversed(fallback_scenes):
            for fallback_scene_name in fallback_scene_names:
                fallback_scene = self.scenario.scenes[fallback_scene_name]
                if fallback_scene not in visited_scenes:
                    visited_scenes.add(fallback_scene)
                    regions.append(fallback_scene.base_region)
                    self._backpatch_fallback_iter(regions, fallback_scene, visited_scenes)

    def _make_imagemap_filepath(self, file_digest):
        file_format, digest = file_digest.split('_', 1)
        filepath = f'imagemap/{digest}.{convert_image.get_ext_from_format(file_format)}'
        return filepath

    def _make_image_filepath(self, file_digest, resize_to):
        file_format, digest = file_digest.split('_', 1)
        filepath = f'image/{digest}_{resize_to}.{convert_image.get_ext_from_format(file_format)}'
        return filepath

    def _make_video_filepath(self, file_digest):
        filepath = f'video/{file_digest}.mp4'
        return filepath

    def _make_url_from_filepath(self, filepath):
        return object_store.public_url(filepath)

    def build_image_for_imagemap_command(self, image_url):
        return self.build_image(image_url, 'imagemap')

    def build_image_for_image_command(self, image_url):
        return self.build_image(image_url, 'image')

    def build_video(self, video_url):
        key = f'video|{video_url}'
        if key in self.image_file_read_cache:
            return self.image_file_read_cache[key]

        stat = MediaFileStatDB.get_cached_media_file_stat('video', video_url)
        if self.option_skip_image:
            # skip image オプションが有効の場合、過去に変換したことのある URL は
            # 更新確認をせずにスキップする
            if stat:
                file_type, file_size, file_digest, attributes = stat
                result = self._make_url_from_filepath(self._make_video_filepath(file_digest))
                self.image_file_read_cache[key] = result
                return result

        try:
            # TODO: 動画を全部メモリに読み込むのではなく、ストリーミングで処理するようにする
            response = requests.get(video_url)
            response.raise_for_status()
            content = response.content
        except Exception as e:
            self.raise_error(f'動画ファイルの読み込みに失敗しました: {video_url} {str(e)}')

        file_digest = hashlib.md5(content).hexdigest()

        if not self.option_force and stat is not None and file_digest == stat[2]:
            # ダイジェストが一致しているので保存を省略する
            result = self._make_url_from_filepath(self._make_video_filepath(file_digest))
            self.image_file_read_cache[key] = result
            return result

        url = self.build_video_with_rawdata(content, file_digest, logging_context=str(video_url))
        MediaFileStatDB.put_cached_media_file_stat('video', video_url, 'video', len(content), file_digest)
        result = url
        self.image_file_read_cache[key] = result
        return result

    def build_video_with_rawdata(self, data, file_digest, logging_context=''):
        filepath = self._make_video_filepath(file_digest)
        result = self._save_video_data(data, filepath)
        if result is None:
            self.raise_error('動画ファイルが保存できませんでした: {}'.format(logging_context))
        return result

    def _save_video_data(self, data, filepath):
        if filepath in self.image_file_write_cache:
            logging.debug(f'skip save video (write cache): {filepath}')
            return self.image_file_write_cache[filepath]

        try:
            logging.info(f'save video file: {filepath}')
            result = object_store.store_public(
                filepath,
                data,
                content_type='video/mp4',
            )

        except Exception as e:
            logging.error(f'ファイルの書き込みに失敗しました: {str(e)}')
            return None

        self.image_file_write_cache[filepath] = result
        return result

    def build_image(self, image_url, kind):
        key = f'{kind}|{image_url}'
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

        try:
            response = requests.get(image_url)
            response.raise_for_status()
            content = response.content
        except Exception as e:
            self.raise_error(f'画像ファイルの読み込みに失敗しました: {image_url} {str(e)}')
        image_format = convert_image.get_image_format(content)
        file_digest = f'{image_format}_{hashlib.md5(content).hexdigest()}'

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
            url, size = self.build_image_for_imagemap_command_with_rawdata(content, file_digest=file_digest, logging_context=str(image_url))
        else:
            url, size = self.build_image_for_image_command_with_rawdata(content, file_digest=file_digest, logging_context=str(image_url))

        ImageFileStatDB.put_cached_image_file_stat(kind, image_url, file_digest, size)
        result = url, size
        self.image_file_read_cache[key] = result
        return result

    def build_image_for_imagemap_command_with_rawdata(self, orig_data, file_digest, logging_context=''):
        filepath = self._make_imagemap_filepath(file_digest)
        size = None
        for resize_to in [240, 300, 460, 700, 1040]:
            result, size = self._resize_and_save_image_data(orig_data, resize_to, f'{filepath}/{resize_to}', force_fit_width=True)
            if result is None:
                self.raise_error('画像ファイルが変換できませんでした: {}'.format(logging_context))
        # size は最後に変換した 1040 のものを返す
        url = self._make_url_from_filepath(filepath)
        return url, size

    def build_image_for_image_command_with_rawdata(self, orig_data, file_digest, logging_context=''):
        result = None
        size = None
        for resize_to in [240, 1024]:
            filepath = self._make_image_filepath(file_digest, resize_to)
            result, size = self._resize_and_save_image_data(orig_data, resize_to, filepath, never_stretch=True)
            if result is None:
                self.raise_error('画像ファイルが変換できませんでした: {}'.format(logging_context))
        # result, size は最後に変換した 1024 のものを返す
        return result, size

    def _resize_and_save_image_data(self, orig_data, resize_to, filepath, force_fit_width=False, never_stretch=False):
        if filepath in self.image_file_write_cache:
            logging.debug(f'skip save image (write cache): {filepath}, {resize_to}')
            return self.image_file_write_cache[filepath]

        image_data, image_format, size = convert_image.resize_image(orig_data, resize_to, force_fit_width=force_fit_width, never_stretch=never_stretch)
        if image_data is None:
            return None, None

        try:
            logging.info(f'save image file: {filepath}')
            image_url = object_store.store_public(
                filepath,
                image_data,
                content_type=convert_image.get_content_type_from_format(image_format),
            )

        except Exception as e:
            logging.error(f'ファイルの書き込みに失敗しました: {str(e)}')
            return None, None

        result = (image_url, size)
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
            error_msg += '\n' + str(arg)
        if self.node:
            error_msg += '\n' + str(self.node)
        raise ScenarioSyntaxError(error_msg)

    def assert_strlen(self, msg, maxlen, error_msg = None):
        if error_msg is None:
            error_msg = '文字数制限（{}文字）'
        if len(msg) > maxlen:
            self.raise_error(error_msg.format(maxlen), msg)

    def assert_strlen_from_array(self, options, index, maxlen, error_msg = None):
        if len(options) > index:
            self.assert_strlen(options[index], maxlen, error_msg)
            return True
        return False

    def assert_imageurl(self, url, error_msg = None):
        if error_msg is None:
            error_msg = '画像を指定すべきセルに違うものが指定されています'
        if not self.parse_imageurl(url):
            self.raise_error(error_msg, url)

    def assert_imageurl_from_array(self, options, index, error_msg = None):
        if len(options) > index and options[index] != '':
            self.assert_imageurl(options[index], error_msg)
            return True
        return False

    def _build_and_replace_imageurl(self, options, index):
        if len(options) > index and options[index] != '':
            orig_url = self.parse_imageurl(options[index])
            if orig_url is not None:
                image_url, _ = self.build_image_for_image_command(orig_url)
                options[index] = image_url
            return True
        return False

    def parse_imageurl(self, cell, parse_plain_url=True):
        m = commands.RE_IMAGE.match(cell)
        if m:
            return m.group(1)
        elif parse_plain_url and utility.is_valid_web_url(cell):
            return cell
        else:
            return None


class StringFormatter(string.Formatter):
    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            # field_name を正規化
            key = normalize('NFKC', key).lower()
            return super(StringFormatter, self).get_value(key, args, kwargs)
        else:
            return super(StringFormatter, self).get_value(key, args, kwargs)

    def convert_field(self, value, conversion):
        if conversion == 'j':
            return json.dumps(value)
        return super().convert_field(value, conversion)

class Director:
    def __init__(self, scenario, context):
        self.scenario = scenario
        self.version = scenario.version
        expression.set_version(self.version)
        self.base_scene = None
        self.context = context
        self.vformat = StringFormatter().vformat
        self.flag_label_error = False
        self.deferred_actions = []

    def _get_scene(self, scene_title):
        if scene_title in self.scenario.scenes:
            return self.scenario.scenes[scene_title]
        return None

    def _get_scene_or_default(self, scene_title):
        if scene_title in self.scenario.scenes:
            return self.scenario.scenes[scene_title]
        else:
            # 指定されたシーンが無かった場合、同じタブのデフォルトシーンがあれば採用する
            scene = self.scenario.scenes.get(scene_title.split('/')[0] + '/', None)
            if not scene:
                # タブ名すら見つからない場合はデフォルトシーンが用いられる
                scene = self.scenario.scenes['*default']
            return scene

    def search_block(self, scene, action, dry_run=False):
        if scene is None or action is None:
            return None, None, None, None

        if action.startswith('*'):
            # シーンジャンプのアクションである
            m = re.match(r'^\*([^#]+)(#.*)?$', action)
            if not m:
                raise ValueError('cannot parse scene name: ' + action)
            scene_fullpath = m.group(1)
            action_tag = m.group(2)
            if self.version <= 2 and scene_fullpath[0] == '*' and scene_fullpath[1:] in BACK_JUMPS:
                # 呼び出し元に戻る特殊なジャンプ
                scene_fullpath = self.jump_back_scene()
                if scene_fullpath is None:
                    logging.error('cannot jump back')
                    return None, None, None, None
                next_scene = self._get_scene(scene_fullpath)
                if next_scene is None:
                    if not dry_run:
                        self.flag_label_error = True
                    return None, None, None, None
                if not dry_run:
                    self.base_scene = next_scene
            else:
                # 通常のシーンジャンプ
                if '/' not in scene_fullpath:
                    # フルパスにするために現在のシーンの tab_name を補完する
                    scene_fullpath = self.base_scene.tab_name + '/' + scene_fullpath
                next_scene = self._get_scene(scene_fullpath)
                if next_scene is None:
                    if not dry_run:
                        self.flag_label_error = True
                    return None, None, None, None
                if not dry_run:
                    self.base_scene = next_scene
                    self.enter_new_scene(scene_fullpath)

            # このまま scene と action を読み替えて検索開始
            scene = self.base_scene
            if action_tag is not None:
                action = action_tag
            else:
                # '#' はシーンの先頭を表す特殊なインデックス
                action = scene.get_entrypoint_label()

        visited_regions = set()
        if self.version >= 3:
            merged_regions = [self.startup_scene.regions[0]] + scene.regions
            if len(self.startup_scene.regions) > 0:
                merged_regions += self.startup_scene.regions[1:]
        else:
            merged_regions = scene.regions
        for region in merged_regions:
            if region in visited_regions:
                continue
            visited_regions.add(region)
            result = self._search_block_sub(scene, region, action, set())
            if result[2] is not None:
                return result[0], result[1], result[2], result[3]

        if action.startswith('#'):
            # tag 指定の呼び出しだったのに見つからなかった
            self.flag_label_error = True

        return None, None, None, None

    def _search_block_sub(self, scene, region, action, visited_scenes):
        visited_scenes.add(scene.get_fullpath())
        #print(f'> {action}, {scene.get_fullpath()}, ({",".join(visited_scenes)})')
        #print(f"  REGION: {region.tab_name}/{region.sub_name}")
        for n_lines, tup in enumerate(region.blocks):
            cond, lines = tup
            #print(f"{region.tab_name}/{region.sub_name}", cond, lines)
            if cond.is_command():
                match = cond.check(action, self.context.env)
                if not match:
                    continue
                if cond.value in (INCLUDE_REGION_CMDS + INCLUDEIF_REGION_CMDS):
                    # @include 処理
                    scene_fullpath = None
                    if cond.value in INCLUDEIF_REGION_CMDS:
                        expr = cond.options[0]
                        if not expr.eval(self.context.env, self.context.env.matches):
                            # 条件を満たさなかったのでスキップ
                            continue
                        scene_fullpath = cond.options[1][1:]
                    elif cond.value in INCLUDE_REGION_CMDS:
                        scene_fullpath = cond.options[0][1:]

                    if scene_fullpath not in self.scenario.scenes:
                        logging.error('invalid @include scene name: ' + scene_fullpath)
                        return None, None, None, None
                    elif scene_fullpath in visited_scenes:
                        # 同じシーンを2回以上includeしようとしたらスキップ
                        pass
                    else:
                        # include 処理
                        next_scene = self.scenario.scenes[scene_fullpath]
                        if self.version >= 3:
                            # include 先の region チェーンをたどる
                            for next_region in next_scene.regions:
                                result = self._search_block_sub(next_scene, next_region, action, visited_scenes)
                                if result[2] is not None:
                                    return result[0], result[1], result[2], result[3]
                            continue
                        else: # version 2 まで
                            # include 先の region チェーンはたどらない
                            next_region = next_scene.regions[0]
                            result = self._search_block_sub(next_scene, next_region, action, visited_scenes)
                            if result[2] is None:
                                # include 内では該当する処理がなかったので、続きへ
                                continue
                            else:
                                return result[0], result[1], result[2], result[3]
            else:
                match = cond.check(action, self.context.env)
                if match:
                    return scene, region, n_lines, match
        return None, None, None, None

    def format_value(self, value):
        try:
            result = self.vformat(value, self.context.env.matches, self.context.env)
        except (KeyError, IndexError, UnicodeEncodeError) as e:
            logging.error('format error: ' + str(e))
            result = value
        return result

    def format_values(self, arr):
        try:
            result = [self.vformat(cell, self.context.env.matches, self.context.env) if isinstance(cell, str) else cell for cell in arr]
        except (KeyError, IndexError, UnicodeEncodeError) as e:
            logging.error('format error: ' + str(e))
            result = arr
        return result

    def enter_new_scene(self, scene_title):
        if scene_title not in self.scenario.scenes:
            logging.info('指定されたシーン名が存在していません:' + scene_title)

        if self.version <= 2:
            # version 3 以降は call したときのみ
            self.context.status.push_scene_history(self.context.status.scene)

        self.context.status.scene = scene_title
        self.context.status.renew_action_token()
        # TODO: action_token もスタックに積んでおいた方がいいのか考える
        #print u','.join(self.context.status.scene_history)

    # v2 までの仕様
    def jump_back_scene(self):
        scene_title = self.context.status.pop_scene_history()
        if not scene_title:
            return None
        self.context.status.scene = scene_title
        self.context.status.renew_action_token()
        #print u','.join(self.context.status.scene_history)
        return scene_title

    def _plan_reaction_sub(self, scene, region, n_lines, match):
        if n_lines is None:
            return None

        cond, lines = region.blocks[n_lines]
        for command in lines:
            if len(self.context.reactions) > 100:
                logging.error("reaction 処理内で無限ループを検出しました")
                break
            sender = command.sender
            msg = self.format_value(command.msg)
            options = command.options
            if options is None:
                options = ()
            options = self.format_values(options)
            if sender is None:
                sender_name = self.context.status.get('$$name', None)
                if sender_name is not None and sender_name != '':
                    sender = sender_name
            if sender == '': # 意図的に空文字で設定してあるときは $$name で上書きしない
                sender = None
            row = [sender, msg]
            if options:
                row.extend(options)
            if self.version >= 3:
                children = None
                if command.children is not None:
                    children = [self.format_values(child_row) for child_row in command.children]
            else:
                children = command.children
            if msg.startswith('@'):
                result = commands.invoke_runtime_run_command(self.context, sender, msg, options, children)
                if result == True:
                    # True はコマンド側で処理済み
                    continue
                elif isinstance(result, str):
                    # 文字列が返ってきたら、そこへのジャンプを行う
                    return self.search_block(self.base_scene, result)

                if msg in OR_CMDS:
                    if len(region.blocks) > n_lines+1:
                        return (scene, region, n_lines+1, match)
                    else:
                        return None
                elif msg in IF_CMDS:
                    if self.version >= 2:
                        expr = options[0]
                    else:
                        expr = Guard_V1.from_str(options[0])
                        if expr is None:
                            logging.error('条件指定が正しくありません: {}'.format(options[0]))
                            return None
                    if expr.eval(self.context.env, self.context.env.matches):
                        next_label = options[1]
                    else:
                        next_label = options[2]
                    return self.search_block(self.base_scene, next_label)
                # TODO: いずれは @seq もプラグインに
                elif msg in SEQ_CMDS or msg in LOOP_CMDS:
                    is_loop = msg in LOOP_CMDS
                    node_seq = self.context.status.get('$$node.seq.' + scene.tab_name, {})
                    command_id = command.command_id
                    index = 0
                    if command_id in node_seq:
                        index = int(node_seq[command_id])
                    if index >= len(options):
                        if is_loop:
                            index = 0
                        else:
                            index = len(options) - 1
                    node_seq[command_id] = str(index + 1)
                    self.context.status['$$node.seq.' + scene.tab_name] = node_seq
                    return self.search_block(self.base_scene, options[index])
                elif msg in RANDOM_CMDS:
                    node_seq = self.context.status.get('$$node.seq.' + scene.tab_name, {})
                    command_id = command.command_id
                    index = 0
                    if command_id in node_seq:
                        if len(options) >= 2:
                            index = random.randint(0, len(options)-2)
                            last_index = int(node_seq[command_id])
                            if index >= last_index:
                                index += 1
                    else:
                        index = random.randint(0, len(options)-1)
                    node_seq[command_id] = str(index)
                    self.context.status['$$node.seq.' + scene.tab_name] = node_seq
                    return self.search_block(self.base_scene, options[index])
                elif msg in CALL_CMDS:
                    jump_label = options[0]
                    return_label = options[1]
                    # 引数の評価
                    for i in range(CALL_ARGS_MAX):
                        if f'$${i+1}' in self.context.status:
                            del self.context.status[f'$${i+1}']
                    for i, value in enumerate(options[2:]):
                        if isinstance(value, Expression):
                            value = expr.eval(self.context.env, self.context.env.matches)
                        self.context.status[f'$${i+1}'] = value
                    frame = json.dumps({
                        'return': return_label,
                        'scene': self.base_scene.get_fullpath(),
                    })
                    self.context.status.push_scene_history(frame)
                    return self.search_block(self.base_scene, jump_label)
                elif msg in RETURN_CMDS:
                    frame = self.context.status.pop_scene_history()
                    if not frame:
                        logging.error('コールスタックが空でリターンできません')
                        return None, None, None, None
                    if len(options) > 0:
                        value = options[0]
                        if isinstance(value, Expression):
                            value = value.eval(self.context.env, self.context.env.matches)
                        self.context.status['$$result'] = value
                    try:
                        frame = json.loads(frame)
                    except:
                        logging.error('コールスタックが壊れています')
                        return None, None, None, None
                    self.context.status.scene = frame['scene']
                    self.context.status.renew_action_token()
                    self.base_scene = self._get_scene(frame['scene'])
                    return self.search_block(self.base_scene, frame['return'], True)
                elif msg in DEFER_CMDS:
                    self.deferred_actions.append(options[0])
                    continue
            elif msg.startswith('*') or msg.startswith('#'):
                # ジャンプ
                return self.search_block(self.base_scene, msg)

            self.context.reactions.append((row, children))

        return None

    def plan_reactions(self):
        # シーン決定
        scene_title = self.context.status.scene
        if scene_title is None:
            # 初回アクセス
            scene_title = self.scenario.startup_scene_title
            self.enter_new_scene(scene_title)

        if self.version >= 3:
            # startup scene は常に最初に評価される
            self.startup_scene = self._get_scene(self.scenario.startup_scene_title)

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
                logging.info('action_token is not matched: {} != {}'.format(action_token, self.context.status.action_token))
                return

        # 実行行の取得
        scene, region, n_lines, match = self.search_block(self.base_scene, action)

        flag_error = False
        counter = 0
        while True:
            if scene is None and not flag_error:
                # 実行すべきブロックの発見に失敗した
                if self.flag_label_error:
                    # ラベル指定があったのに見つけられなかった
                    logging.warning("ラベルを見つけられませんでした: {} @ {}".format(action, self.base_scene))
                    # ##error_invalid_label という特殊な action を発行する
                    self.context.add_env({
                        '$$invalid_label': action
                    })
                    scene, region, n_lines, match = self.search_block(self.base_scene, "##error_invalid_label")
                else:
                    # 通常の文字指定で対応するブロックがなかった
                    # ##error_unhandled_action という特殊な action を発行する
                    self.context.add_env({
                        '$$unhandled_action': action
                    })
                    scene, region, n_lines, match = self.search_block(self.base_scene, "##error_unhandled_action")
                flag_error = True

            self.context.env.set_matches(match)
            new_context = self._plan_reaction_sub(scene, region, n_lines, match)
            self.context.env.clear_matches()
            counter += 1
            if counter > 10000:
                logging.error("無限ループを検出しました")
                return
            while new_context is None:
                # reaction に結果が入っている場合と、何もすることが見つけられなかった場合がある
                if self.deferred_actions:
                    # あとで実行するように登録されていたアクションを実行する
                    action = self.deferred_actions.pop(0)
                    self.context.current_action = action
                    new_context = self.search_block(self.base_scene, action)
                    counter += 1
                    if counter > 10000:
                        logging.error("無限ループを検出しました")
                        return
                else:
                    break
            if new_context is None:
                break
            scene, region, n_lines, match = new_context

        return
