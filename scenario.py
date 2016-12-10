# coding: utf-8
import re
import logging
import random
import string
from unicodedata import normalize

BUTTON_CMDS = (u'@button', u'@ボタン')
CONFIRM_CMDS = (u'@confirm', u'@確認')
PANEL_CMDS = (u'@carousel', u'@カルーセル', u'@panel', u'@パネル')
IMAGEMAP_CMDS = (u'@imagemap', u'@イメージマップ')
ALL_TEMPLATE_CMDS = BUTTON_CMDS + CONFIRM_CMDS + PANEL_CMDS + IMAGEMAP_CMDS

MORE_CMDS = (u'@more', u'@続きを読む')
OR_CMDS = (u'@or', u'@または')
SCENE_CMDS = (u'@scene', u'@シーン')
RESET_CMDS = (u'@reset', u'@リセット')
SET_CMDS = (u'@set', u'@セット')
AI_CMDS = (u'@ai', u'@AI')
AI_RESET_CMDS = (u'@aireset', u'@AIリセット')

INCLUDE_COND_CMDS = (u'@include', u'@読込')

BACK_JUMPS = (u'back', u'戻る')

PRIORITY_AI_CMDS = (u'@priority', u'@優先度')
PERCENT_AI_CMDS = (u'@percent', u'@確率')
ALWAYS_AI_CMDS = (u'@always', u'@常時')
MEMORY_AI_CMDS = (u'@memory', u'@記憶')
DEFINE_AI_CMDS = (u'@define', u'@定義')

SEQUENCE_AI_CMDS = (u'@seq', u'@順々')

MAX_HISTORY = 16
MAX_MEMORY = 5

class ScenarioSyntaxError(Exception):
    def __str__(self):
        return (u','.join(self.args)).encode('utf-8')
    def __unicode__(self):
        return u','.join(self.args)

BEFORE_LINE_0 = -1

CONDITION_KIND_STRING = 1
CONDITION_KIND_REGEXP = 2
CONDITION_KIND_COMMAND = 100

PRIORITY_MAP = {
    u'超': 100,
    u'高': 80,
    u'中': 50,
    u'低': 20,
    u'稀': 1,
}
DEFAULT_PRIORITY = (u'高', u'低')
DUPLICATE_PRIORITY = u'稀'


class Guard(object):
    def __init__(self):
        self.terms = []

    @classmethod
    def from_str(cls, guard):
        self = cls()
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

    def eval(self, status):
        # 各項を and でつないだ条件
        # OR は @or でひとまずは何とか・・・
        # 項が１つもなければ True
        for op, lhs, rhs in self.terms:
            if lhs.startswith(u'$'):
                lhs = status.get(lhs, u'')
            if rhs.startswith(u'$'):
                rhs = status.get(rhs, u'')
            if op == u'==':
                if not lhs == rhs:
                    return False
            elif op == u'!=':
                if not lhs != rhs:
                    return False
        return True


class Condition(object):
    def __init__(self, kind, value, guard=None, options=None):
        self.kind = kind
        self.value = value
        self.guard = guard
        self.options = options

    def check(self, action, status):
        if self.guard:
            if not self.guard.eval(status):
                return None

        if self.kind == CONDITION_KIND_STRING:
            return (action,) if self.value == action else None
        elif self.kind == CONDITION_KIND_REGEXP:
            m = self.value.match(action)
            return (m.group(0),) + m.groups() if m else None
        else:
            raise ValueError("invalid Condition: " + self.value)

    def is_command(self):
        return self.kind == CONDITION_KIND_COMMAND

    def is_condition(self):
        return self.kind == CONDITION_KIND_STRING or self.kind == CONDITION_KIND_REGEXP


class Block(object):
    def __init__(self, tab_name, sub_name=u''):
        self.tab_name = tab_name
        self.sub_name = sub_name
        self.indices = []


class Scene(object):
    def __init__(self, tab_name, sub_name=u''):
        self.tab_name = tab_name
        self.sub_name = sub_name
        self.blocks = []

    def get_fullpath(self):
        return self.tab_name + u'/' + self.sub_name


class AICondition(object):
    def __init__(self):
        pass


class AIClauseCondition(AICondition):
    def __init__(self, clause):
        super(AIClauseCondition, self).__init__()
        self.clause = clause

    def check(self, action, named_entities, context, _):
        matched_word = None
        for word in self.clause:
            if word.startswith(u'%'):
                # グループ定義
                for entity_word in named_entities[word]:
                    if entity_word in action:
                        matched_word = entity_word
                        break
                if matched_word: break
            elif word in action:
                matched_word = word
                break

        if not matched_word:
            # 発言内に必要な単語が無かったので、文脈から探す
            memory = context.get(u'memory', [])
            for word in self.clause:
                if word.startswith(u'%'):
                    # グループ定義
                    for entity_word in named_entities[word]:
                        if entity_word in memory:
                            matched_word = entity_word
                            break
                    if matched_word: break
                elif word in memory:
                    matched_word = word
                    break

        return matched_word is not None, matched_word


class AIAlwaysCondition(AICondition):
    def check(self, _1, _2, _3, _4):
        return True, None


class AIMemoryCondition(AICondition):
    def check(self, _1, _2, _3, status):
        memory = status.get("memory", None)
        if memory:
            return True, random.choice(memory)
        else:
            return False, None


class AIPercentCondition(AICondition):
    def __init__(self, percent):
        super(AIPercentCondition, self).__init__()
        self.percent = percent

    def check(self, _1, _2, _3, _4):
        r = random.randint(0, 99)
        if r < self.percent:
            return True, None
        else:
            return False, None


class AIDic(object):
    def __init__(self, tab_name):
        self.tab_name = tab_name
        self.named_entities = {}
        self.conditions = []


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
        self.term = tuple(new_term)

    def normalize(self, *args):
        new_term = []
        for i, v in enumerate(self.term):
            if i in args:
                new_term.append(normalize('NFKC', v))
            else:
                new_term.append(v)
        self.term = tuple(new_term)

    def normalize_all(self):
        self.term = tuple([normalize('NFKC', v) for v in self.term])

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


class Scenario(object):
    def __init__(self):
        self.scenes = {}
        self.ai_dic = {}
        self.first_top_block = None
        self.startup_scene_title = None
        self.tab_name = None

    @classmethod
    def from_table(cls, table):
        self = cls()
        self._read_table(u'default', table)
        if not self.scenes:
            self.raise_error(u'シナリオには1つ以上のシーンを含んでいる必要があります')
        return self

    @classmethod
    def from_tables(cls, tables):
        self = cls()
        for tab_name, table in tables:
            # シート名は正規化する
            tab_name = normalize('NFKC', tab_name)
            if tab_name.startswith(u'AI'):
                self._read_ai_table(tab_name, table)
            else:
                self._read_table(tab_name, table)
        if not self.scenes:
            self.raise_error(u'シナリオには1つ以上のシーンを含んでいる必要があります')
        return self

    def _parse_sub_tree(self, node, table, tab_name, line_no, level, column_as_node_rule=False):
        first_line_no = line_no
        while line_no < len(table):
            row = table[line_no]

            # 各セル内の空白文字を除去
            row = [cell.strip() if isinstance(cell, unicode) else cell for cell in row]

            # 空行・コメント行はスキップ
            if not row or row[0] == u'#' or row[0] == u'＃':
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
            while row:
                if row[-1]: break
                row.pop()

            # #@[*%で始まっていたら半角化（正規化）する
            row = [normalize('NFKC', cell) if re.match(u'^[#＃@＠\[［*＊%％]', cell) else cell for cell in row]

            child_node = SyntaxTree(tab_name, line_no, tuple(row[level:]))
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

    def _read_table(self, tab_name, table):
        self.tab_name = tab_name

        root = SyntaxTree(tab_name, 0, (u'**'+tab_name,))
        # ファイルの先頭に特殊な親ノードを貼る
        root.children.append(SyntaxTree(tab_name, 0, (u'*',)))
        self._parse_sub_tree(root, table, tab_name, 0, 0, column_as_node_rule=True)

        top_block = None
        scene_count = 0
        for node in root.children:
            cond_str = node.get_factor(0)

            if not cond_str:
                self.raise_error(u'internal parser error')

            if cond_str.startswith(u'@'):
                # コマンド＠条件セル
                if cond_str in INCLUDE_COND_CMDS:
                    msg = node.get_factor(1)
                    if not msg or not msg.startswith(u'*'):
                        # TODO: 正しいシーン名か validation する
                        self.raise_error(u'@include のあとにはシーンラベルを指定してください', node)
                else:
                    self.raise_error(u'不正なコマンドです', node)
                cond = Condition(CONDITION_KIND_COMMAND, cond_str, options=node.get_factors(1))
            elif cond_str.startswith(u'*'):
                # 新しいシーンを開始する
                sub_name = cond_str[1:]
                if u'/' in sub_name:
                    self.raise_error(u'シーン名に/を含むことはできません', node)
                scene = Scene(tab_name, sub_name)
                self.scenes[scene.get_fullpath()] = scene
                # 最初に定義されたシーンがスタートアップシーンとなる
                if not self.startup_scene_title:
                    self.startup_scene_title = scene.get_fullpath()

                block = Block(tab_name, sub_name)
                # トップブロックと最初のタブのトップブロックが常に include される
                scene.blocks.append(block)
                if top_block is None:
                    # table で先頭のブロック
                    if sub_name != u'':
                        self.raise_error(u'inernal parse error', node)
                    top_block = block
                    if self.first_top_block is None:
                        # 最初に設定した top_block が first_top_block
                        self.first_top_block = top_block
                else:
                    scene.blocks.append(top_block)
                if self.first_top_block != top_block:
                    scene.blocks.append(self.first_top_block)
                scene_count += 1
                cond = Condition(CONDITION_KIND_STRING, u'#')
            else:
                # 通常の条件セル
                guard = None
                if cond_str.startswith(u'['):
                    m = re.match(r'^\[([^\]]*)\][\s\n]*(.+)$', cond_str)
                    if m:
                        guard = Guard.from_str(m.group(1))
                        if guard is None:
                            self.raise_error(u'条件指定が正しくありません', node)
                        cond_str = m.group(2)
                    else:
                        self.raise_error(u'条件指定が正しくありません', node)
                if cond_str[0] == u'/' and cond_str[-1] == u'/':
                    cond = Condition(CONDITION_KIND_REGEXP, re.compile(cond_str[1:-1]), guard=guard)
                else:
                    cond = Condition(CONDITION_KIND_STRING, cond_str, guard=guard)
            lines = []
            block.indices.append((cond, lines))
            # 新しい条件に来たので、メッセージ数カウンタを初期化する
            msg_count = 0

            for node in node.children:
                msg = node.get_factor(0)
                options = node.get_factors(1)

                if msg in CONFIRM_CMDS or msg in BUTTON_CMDS or msg in IMAGEMAP_CMDS:
                    self.lint_command(msg, options)
                    choices = []
                    for choice in node.children:
                        self.lint_choice(msg, choice.term)
                        choices.append(choice.term)
                    if (msg in CONFIRM_CMDS or msg in BUTTON_CMDS) and len(choices) == 0:
                        self.raise_error(u'選択肢が0個です', node)
                    if msg in CONFIRM_CMDS and len(choices) > 2:
                        self.raise_error(u'「＠確認」の選択肢は最大で2個です', node)
                    if msg in BUTTON_CMDS and len(choices) > 4:
                        self.raise_error(u'「＠ボタン」の選択肢は最大で4個です', node)
                    if msg in IMAGEMAP_CMDS and len(choices) > 49:
                        self.raise_error(u'「＠イメージマップ」の選択肢は最大で49個です', node)
                    lines.append((node.term, choices))
                    msg_count += 1

                elif msg in PANEL_CMDS:
                    self.lint_command(msg, options)
                    panels = []
                    num_choices = -1
                    flag_title = None
                    flag_image = None
                    for panel in node.children:
                        self.lint_panel(panel.term)
                        choices = []
                        for choice in panel.children:
                            self.lint_choice(msg, choice.term)
                            choices.append(choice.term)
                        if len(choices) == 0:
                            self.raise_error(u'選択肢が0個です', panel)
                        if len(choices) > 3:
                            self.raise_error(u'パネルの選択肢は最大3個です', panel)
                        if num_choices != -1 and num_choices != len(choices):
                            self.raise_error(u'各パネルの選択肢数がばらばらです', node)
                        num_choices = len(choices)
                        if (flag_title is not None) and ((panel.get_factor(1) != u'') != flag_title):
                            self.raise_error(u'各パネルのタイトルの有無がばらばらです', node)
                        flag_title = (panel.get_factor(1) != u'')
                        if (flag_image is not None) and ((panel.get_factor(2) != u'') != flag_image):
                            self.raise_error(u'各パネルの画像の有無がばらばらです', node)
                        flag_image = (panel.get_factor(2) != u'')
                        panels.append((panel.term, choices))
                    if len(panels) == 0:
                        self.raise_error(u'パネルが0個です', node)
                    if len(panels) > 5:
                        self.raise_error(u'パネルは最大で5個です', node)
                    lines.append((node.term, panels))
                    msg_count += 1

                elif msg.startswith('@'):
                    if msg in OR_CMDS:
                        pass
                    elif msg in RESET_CMDS:
                        pass
                    elif msg in SCENE_CMDS:
                        if len(options) < 1:
                            self.raise_error(u'コマンドの引数が足りません', node)
                        # 引数を正規化
                        node.normalize(1)
                        #if row[2] not in self.scenes:
                        #    self.raise_error(u'コマンドで指定したシーンが存在しません', *row)
                    elif msg in SET_CMDS:
                        if len(options) < 2:
                            self.raise_error(u'コマンドの引数が足りません', node)
                        # 引数を正規化
                        node.normalize(1)
                        node.normalize(2)
                        if not node.get_factor(1).startswith(u'$'):
                            self.raise_error(u'＠セットの第一引数は $*** である必要があります', node)
                    elif msg in AI_CMDS:
                        if len(options) < 1:
                            self.raise_error(u'コマンドの引数が足りません', node)
                        # 引数を正規化
                        node.normalize(1)
                    elif msg in AI_RESET_CMDS:
                        if len(options) < 1:
                            self.raise_error(u'コマンドの引数が足りません', node)
                        # 引数を正規化
                        node.normalize(1)
                    else:
                        self.raise_error(u'間違ったコマンドです', node)
                    lines.append((node.term, None))

                else:
                    # 仕様書に記述がないが、おそらく300文字が上限
                    self.assert_strlen(msg, 300)
                    lines.append((node.term, None))
                    msg_count += 1

                if msg_count > 5:
                    self.raise_error(u'6つ以上のメッセージを同時に送ろうとしました', node)
        #print root.dump().encode('utf-8')

    def _read_ai_table(self, tab_name, table):
        self.tab_name = tab_name

        root = SyntaxTree(tab_name, 0, (u'**'+tab_name,))
        self._parse_sub_tree(root, table, tab_name, 0, 0)

        ai_dic = AIDic(tab_name)
        self.ai_dic[tab_name] = ai_dic

        msg_list = None
        conj_cond = None
        priority = None

        for node in root.children:
            # 条件行 or 優先度指定
            if conj_cond is None:
                conj_cond = []
                priority = DEFAULT_PRIORITY

            node.compaction()
            node.normalize_all()
            words = node.term

            cond = None
            first = words[0]
            if first.startswith(u'@'):
                options = words[1:]
                if first in DEFINE_AI_CMDS:
                    if len(options) < 1:
                        self.raise_error(u'引数が必要です。', node)
                    name = options[0]
                    if not name.startswith(u'%'):
                        self.raise_error(u'グループ名は % で始める必要があります。', node)
                    word_list = []
                    for word in options[1:]:
                        if word.startswith(u'%'):
                            if word not in ai_dic.named_entities:
                                self.raise_error(u'未定義のグループ名です。', word, node)
                            word_list.extend(ai_dic.named_entities[word])
                        else:
                            word_list.append(word)
                    if name in ai_dic.named_entities:
                        # 既に定義されていたら追加する
                        ai_dic.named_entities[name].extend(word_list)
                    else:
                        ai_dic.named_entities[name] = word_list
                elif first in PRIORITY_AI_CMDS:
                    if len(options) < 2:
                        self.raise_error(u'未読優先度と既読優先度の2つの指定が必要です。', node)
                    if options[0].isdigit() or options[0] in PRIORITY_MAP:
                        priority1 = options[0]
                    else:
                        self.raise_error(u'優先度指定が間違っています。', node)
                    if options[1].isdigit() or options[1] in PRIORITY_MAP:
                        priority2 = options[1]
                    else:
                        self.raise_error(u'優先度指定が間違っています。', node)
                    priority = (priority1, priority2)
                elif first in ALWAYS_AI_CMDS:
                    cond = AIAlwaysCondition()
                elif first in PERCENT_AI_CMDS:
                    if len(options) < 1 or not options[0].isdigit():
                        self.raise_error(u'引数が必要です。', node)
                    cond = AIPercentCondition(int(options[0]))
                elif first in MEMORY_AI_CMDS:
                    cond = AIMemoryCondition()
                else:
                    self.raise_error(u'不正なコマンドです。', node)
            else:
                clause = []
                for word in words:
                    if word.startswith(u'%'):
                        if word not in ai_dic.named_entities:
                            self.raise_error(u'未定義のグループ名です。', word, node)
                    clause.append(word)
                cond = AIClauseCondition(clause)
            if cond:
                conj_cond.append(cond)

            # リアクション行を持っていたら処理する
            if node.children:
                msg_list = []
                ai_dic.conditions.append((conj_cond, priority, msg_list))
                conj_cond = None
                priority = None

                for node in node.children:
                    if msg_list is None:
                        # 単語指定がなく、いきなりメッセージが書かれていた
                        self.raise_error(u'AI辞書が正しい書式ではありません。', node)

                    first_line_no = node.line_no
                    msgs = []
                    args = None

                    for msg in node.term:
                        # 仕様書に記述がないが、おそらく300文字が上限
                        self.assert_strlen(msg, 300)
                        msgs.append(msg)

                    if len(msgs) > 5:
                        self.raise_error(u'6つ以上のメッセージを同時に送ろうとしました', node)

                    if msgs[0] in SEQUENCE_AI_CMDS:
                        args = []
                        for sub_node in node.children:
                            args.append(sub_node.term)
                        if not args:
                            self.raise_error(u'@seq は1つ以上のメッセージを内包している必要があります。', node)

                    msg_list.append((u'L{}'.format(first_line_no), msgs, args))


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

    def parse_url(self, cell):
        m = re.match(r'^(https?://|tel:)', cell)
        if m:
            return m.group(0)
        else:
            return None

    def parse_imageurl(self, cell):
        m = re.match(r'^=IMAGE\("([^"]+)"\)', cell)
        if m:
            return m.group(1)
        else:
            return None

    def lint_command(self, msg, options):
        if msg in CONFIRM_CMDS:
            self.assert_strlen_from_array(options, 0, 240)
        elif msg in BUTTON_CMDS:
            self.assert_strlen_from_array(options, 1, 40)
            self.assert_imageurl_from_array(options, 2)
            if (len(options) > 1 and options[1] != u'') or (len(options) > 2 and options[2] != u''):
                self.assert_strlen_from_array(options, 0, 60, u'タイトルか画像を指定した場合の文字数制限（{}文字）')
            else:
                self.assert_strlen_from_array(options, 0, 160, u'タイトルも画像も指定しない場合の文字数制限（{}文字）')
        elif msg in PANEL_CMDS:
            pass
        elif msg in IMAGEMAP_CMDS:
            self.assert_imageurl_from_array(options, 0)
            try:
                if len(options) > 1 and (float(options[1]) < 0.1 or float(options[1]) > 2.0):
                    self.raise_error(u'アスペクト比は 0.1 から 2.0 までの小数である必要があります', msg, *options)
            except ValueError:
                self.raise_error(u'アスペクト比に数値以外のものが指定されています', msg, *options)
        return True

    def lint_choice(self, msg, choice):
        action_label = choice[0]
        action_value = u''
        action_data = u''
        if len(choice) <= 1 or not choice[1]:
            action_type = 'message'
            action_value = action_label
        else:
            if self.parse_url(choice[1]):
                action_type = 'url'
                action_value = choice[1]
            elif re.match(u'^[#＃*＊]', choice[1]):
                action_type = 'postback'
                action_data = choice[1]
            else:
                action_type = 'message'
                action_value = choice[1]
        if len(choice) > 2 and choice[2]:
            if re.match(u'^[#＃*＊]', choice[2]):
                if action_type == 'url':
                    self.raise_error(u'アクションラベル指定時は URL を開かせることはできません', *choice)
                action_type = 'postback'
                action_data = choice[2]
            else:
                self.raise_error(u'アクションラベルは # か * で始まらないといけません', *choice)

        if msg in IMAGEMAP_CMDS:
            if action_type == 'postback':
                self.raise_error(u'イメージマップではアクションラベルは指定できません', *choice)
            try:
                x, y, w, h = [int(x) for x in action_label.split(u',')]
                if x < 0 or 1040 <= x or y < 0 or 1040 <= y or w <= 0 or 1040 < w or h <= 0 or 1040 < h:
                    raise ValueError
            except (ValueError, IndexError):
                self.raise_error(u'イメージマップアクションの指定が不正です', action_label)
        else:
            self.assert_strlen(action_label, 20)
        if action_type in ('message', 'postback'):
            self.assert_strlen(action_value, 300)
        self.assert_strlen(action_data, 300)
        return True

    def lint_panel(self, panel):
        self.assert_strlen_from_array(panel, 1, 40)
        self.assert_imageurl_from_array(panel, 2)
        if (len(panel) > 1 and panel[1] != u'') or (len(panel) > 2 and panel[2] != u''):
            self.assert_strlen_from_array(panel, 0, 60, u'タイトルか画像を指定した場合の文字数制限（{}文字）')
        else:
            self.assert_strlen_from_array(panel, 0, 120, u'タイトルも画像も指定しない場合の文字数制限（{}文字）')
        return True

    def __str__(self):
        s = ''
        for _, table in self.tables:
            for row, choices in table.lines:
                s += (row.join("\t") + "\n")
        return s


class Director(object):
    def __init__(self, scenario, status = None):
        self.scenario = scenario
        self.status = status or {}
        self.base_scene = None

    def search_index(self, scene, action):
        if action.startswith(u'*'):
            # シーンジャンプのアクションである
            m = re.match(r'^\*([^#]+)(#.*)?$', action)
            if not m:
                raise ValueError(u'cannot parse scene name: ' + action)
            scene_fullpath = m.group(1)
            action_tag = m.group(2)
            if scene_fullpath[0] == u'*' and scene_fullpath[1:] in BACK_JUMPS:
                scene_fullpath = self.jump_back_scene()
                if scene_fullpath is None:
                    raise ValueError(u'cannot jump back')
                self.base_scene = self.scenario.scenes[scene_fullpath]
            else:
                if u'/' not in scene_fullpath:
                    # フルパスにするために現在のシーンの tab_name を補完する
                    scene_fullpath = self.base_scene.tab_name + u'/' + scene_fullpath
                if scene_fullpath not in self.scenario.scenes:
                    raise ValueError(u'invalid scene name: ' + scene_fullpath)
                self.base_scene = self.scenario.scenes[scene_fullpath]
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
                match = cond.check(action, self.status)
                if match:
                    return scene, block, n_lines, match
        return None, None, None, None

    def format_cells(self, arr, match):
        try:
            result = [cell.format(*match) for cell in arr]
        except KeyError, e:
            logging.error('KeyError: ' + e.message)
            result = arr
        return result

    def enter_new_scene(self, scene_title):
        if scene_title not in self.scenario.scenes:
            logging.error(u'指定されたシーン名が存在していません:' + scene_title)
            return None

        if self.status.scene is not None:
            scene_history = self.status.scene_history
            scene_history.append(self.status.scene)
            self.status.scene_history = scene_history[-MAX_HISTORY:]

        self.status.scene = scene_title
        self.status.visit_id = \
            u''.join([random.choice(string.ascii_letters) for _ in range(8)])
        # TODO: visit_id もスタックに積んでおいた方がいいのか考える
        #print u','.join(self.status.scene_history)

    def jump_back_scene(self):
        scene_history = self.status.scene_history
        if not scene_history:
            return None
        scene_title = scene_history.pop()
        self.status.scene = scene_title
        self.status.visit_id = \
            u''.join([random.choice(string.ascii_letters) for _ in range(8)])
        self.status.scene_history = scene_history[-MAX_HISTORY:]
        #print u','.join(self.status.scene_history)
        return scene_title

    def _get_reaction_sub(self, action, scene, block, n_lines, match):
        if n_lines is None:
            return None, None

        cond, lines = block.indices[n_lines]
        reaction = []
        for row, args in lines:
            if len(reaction) >= 5:
                break
            msg = row[0]
            options = row[1:]
            if msg.startswith(u'@'):
                if msg in ALL_TEMPLATE_CMDS:
                    reaction.append((self.format_cells(row, match), args))
                elif msg in OR_CMDS:
                    if len(block.indices) > n_lines+1:
                        return reaction, (scene, block, n_lines+1, match)
                    else:
                        return reaction, None
                elif msg in SCENE_CMDS:
                    scene_fullpath = options[0]
                    if u'/' not in scene_fullpath:
                        scene_fullpath = scene.tab_name + u'/' + scene_fullpath
                    self.enter_new_scene(scene_fullpath)
                elif msg in RESET_CMDS:
                    self.status.reset()
                elif msg in SET_CMDS:
                    self.status[options[0]] = options[1]
                elif msg in AI_CMDS:
                    res = self.respond_with_ai(options, action, match)
                    for res_msg in res:
                        if res_msg.startswith(u'*') or res_msg.startswith(u'#'):
                            # AIの返答にジャンプが混ざっていたらジャンプ
                            return reaction, self.search_index(self.base_scene, res_msg)
                        else:
                            reaction.append((self.format_cells([res_msg], match), args))
                elif msg in AI_RESET_CMDS:
                    self.status[u'ai.read.' + options[0]] = {}
                    self.status[u'ai.seq.' + options[0]] = {}
                    self.status[u'ai.prev'] = ['', 0, '']
                else:
                    raise ValueError(u'unexpected cmd:' + msg)
            elif msg.startswith(u'*') or msg.startswith(u'#'):
                # ジャンプ
                return reaction, self.search_index(self.base_scene, msg)
            else:
                reaction.append((self.format_cells(row, match), args))
        return reaction, None

    def get_reaction(self, action):
        # シーン決定
        scene_title = self.status.scene
        if scene_title is None:
            # 初回アクセス
            scene_title = self.scenario.startup_scene_title
            self.enter_new_scene(scene_title)
        if scene_title not in self.scenario.scenes:
            logging.error(u'指定されたシーン名が存在していません:' + scene_title)
            return None

        self.base_scene = self.scenario.scenes[scene_title]

        # 実行行の取得
        scene, block, n_lines, match = self.search_index(self.base_scene, action)

        reactions = None
        while True:
            cur_reactions, new_context = self._get_reaction_sub(action, scene, block, n_lines, match)
            if cur_reactions:
                if reactions is None:
                    reactions = cur_reactions
                else:
                    reactions.extend(cur_reactions)
            if new_context is None:
                # reaction に結果が入っている場合と、何もすることが見つけられなかった場合がある
                break
            scene, block, n_lines, match = new_context

        return reactions

    def respond_with_ai(self, dic_name_list, action, match):
        ai_prev = self.status.get(u"ai.prev", ['', 0, ''])
        ai_context = self.status.get(u"ai.context", {})

        response_list = []
        for dic_name in dic_name_list:
            ai_dic = self.scenario.ai_dic.get(dic_name, None)
            if ai_dic is None:
                raise ValueError(u'ai dictionary is not exists:' + dic_name)

            # %%キーワード の言葉が含まれていたら記憶する
            memory = ai_context.get(u'memory', [])
            memory.append(u'') # 1発言ごとに過去を忘れるために空白を追加
            for word in ai_dic.named_entities.get(u'%%キーワード', []):
                if word in action:
                    # 記憶の末尾に追加
                    if word in memory:
                        memory.remove(word)
                    memory.append(word)
            ai_context[u'memory'] = memory[-MAX_MEMORY:]

            ai_read = self.status.get(u"ai.read." + dic_name, {})

            for conj_cond, priority, msg_list in ai_dic.conditions:
                flag = True
                matched_word = {}
                for ai_cond in conj_cond:
                    result, m = ai_cond.check(action, ai_dic.named_entities, ai_context, self.status)
                    if result:
                        if m is not None:
                            matched_word[u'w{}'.format(len(matched_word)+1)] = m
                    else:
                        flag = False
                        break
                if flag:
                    for line_no, msgs, args in msg_list:
                        msgs = [msg.format(*match, **matched_word) for msg in msgs]
                        if args:
                            args = [[msg.format(*match, **matched_word) for msg in arg] for arg in args]
                        prio = priority[0]
                        if line_no in ai_read and ai_read[line_no] == msgs[0][0:5]:
                            # 既読の場合
                            # msgs[0][0:5]との比較は、シナリオ書換により行番号がずれたときの保険
                            prio = priority[1]
                        if ai_prev[0] == dic_name and ai_prev[1] == line_no and ai_prev[2] == msgs[0][0:5]:
                            # 直前のメッセージとの重複であった
                            prio = DUPLICATE_PRIORITY
                        if prio.isdigit():
                            prio = int(prio)
                        elif prio in PRIORITY_MAP:
                            prio = PRIORITY_MAP[prio]
                        weight = 100
                        response_list.append((prio*1000+len(matched_word), weight, dic_name, line_no, msgs, args, matched_word))

        if response_list:
            # 優先度が最大のものだけでフィルタする
            priority_max = max(response_list, key=lambda entry: entry[0])[0]
            filtered_response_list = [entry for entry in response_list if entry[0] == priority_max]

            # フィルタ結果の中から、weight を考慮したランダム抽出を行う
            weight_sum = sum([entry[1] for entry in filtered_response_list])
            point = random.randint(0, weight_sum-1)
            for prio, weight, dic_name, line_no, msgs, args, matched_word in filtered_response_list:
                point -= weight
                if point < 0:
                    # このメッセージが抽選で選ばれた
                    if msgs[0] in SEQUENCE_AI_CMDS:
                        # 連続コマンド
                        ai_seq = self.status.get(u'ai.seq.' + dic_name, {})
                        index = 0
                        if line_no in ai_seq:
                            index = ai_seq[line_no]
                        if index >= len(args)-1:
                            # シーケンスの最後まで到達したら既読にする
                            ai_seq[line_no] = 0
                            ai_read = self.status.get(u"ai.read." + dic_name, {})
                            ai_read[line_no] = msgs[0][0:5]
                            self.status[u"ai.read." + dic_name] = ai_read
                        else:
                            ai_seq[line_no] = index+1
                        self.status[u'ai.seq.' + dic_name] = ai_seq
                        self.status[u'ai.prev'] = ['', 0, '']
                        final_response = args[index]
                    else:
                        # 既読フラグ
                        ai_read = self.status.get(u"ai.read." + dic_name, {})
                        ai_read[line_no] = msgs[0][0:5]
                        self.status[u"ai.read." + dic_name] = ai_read
                        # 直前履歴保存
                        self.status[u'ai.prev'] = [dic_name, line_no, msgs[0][0:5]]
                        final_response = msgs

                    # 採用された単語が記憶の中に含まれていたら記憶を新たにする
                    memory = ai_context.get(u'memory', [])
                    for word in matched_word:
                        # 今回出て来た言葉は記憶の末尾に追加
                        if word in memory:
                            memory.remove(word)
                            memory.append(word)
                    ai_context[u'memory'] = memory[-MAX_MEMORY:]
                    self.status[u'ai.context'] = ai_context
                    return final_response
        return None
