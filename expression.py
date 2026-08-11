import re
import logging
from unicodedata import normalize
from arpeggio import ParserPython, PTNodeVisitor, visit_parse_tree, Optional, ZeroOrMore, OneOrMore, And, EOF
from arpeggio import RegExMatch as _
from syntax_tree import SyntaxNode, SyntaxTreeEvaluator
from utility import safe_list_get

DEBUG = False

def token_and():    return '&&'
def token_or():     return '||'
def token_lparen(): return '('
def token_rparen(): return ')'
def token_op_regex(): return '=~'
def op_eq():        return '=='
def op_neq():       return '!='
def op_geq():       return '>='
def op_leq():       return '<='
def op_gt():        return '>'
def op_lt():        return '<'
def null_value():   return _(r'null', ignore_case=True)
def boolean_value(): return _(r'true|on|false|off', ignore_case=True)
def number_value(): return _(r'[-+]?\d+(\.\d+)?')
def string_value(): return _(r'"(\\.|[^"])*"')
def variable():     return _(r'[\$＄]{1,2}[^\s+\-*/\\"=~<>,\[\]@;:`{}!#$%&()^|?＋−＊／”＝〜＜＞，［］＠；：｀｛｝！＃＄％＆（）＾｜？]+')
def unary_op():     return _(r'[-+!]'), value
def sub_expression(): return token_lparen, expression, token_rparen

def token_lbrace(): return '{'
def token_rbrace(): return '}'
def token_comma():  return ','
def token_colon():  return ':'
def token_lbracket(): return '['
def token_rbracket(): return ']'
def token_for():    return "for"
def token_in():     return "in"
def token_if():     return "if"
def token_question(): return '?'
def get_item():     return token_lbracket, expression, token_rbracket

def list_comprehension(): return token_for, variable, token_in, expression, Optional((token_if, expression))
def list_literal_items(): return OneOrMore(expression, sep=token_comma)
def list_body(): return [(expression, And(token_for), list_comprehension), list_literal_items]
def list_value():   return token_lbracket, Optional(list_body), token_rbracket
def dict_comprehension(): return token_for, variable, token_in, expression, Optional((token_if, expression))
def dict_entry():   return expression, token_colon, expression
def dict_literal_entries(): return OneOrMore(dict_entry, sep=token_comma)
def dict_body(): return [(dict_entry, And(token_for), dict_comprehension), dict_literal_entries]
def dict_value():   return token_lbrace, Optional(dict_body), token_rbrace


def primary_value():
    return [unary_op, variable, string_value, number_value, boolean_value, null_value,
            sub_expression, dict_value, list_value]
def value():        return primary_value, ZeroOrMore(get_item)
def prod_op():      return OneOrMore(value, sep=_(r'[*/%]'))
def sum_op():       return OneOrMore(prod_op, sep=_(r'[-+]'))
def regex_match():  return _(r'/(\\/|[^/])*/[iLN]*')
def regex_apply():  return sum_op, token_op_regex, regex_match
def comparison():   return sum_op, [op_eq, op_neq, op_geq, op_leq, op_gt, op_lt], sum_op
def factor():       return [regex_apply, comparison, sum_op]
def and_op():       return OneOrMore(factor, sep=token_and)
def or_op():        return OneOrMore(and_op, sep=token_or)

def condition(): return or_op
def conditional_expression(): return or_op, Optional(conditional_operation)
def conditional_operation(): return token_question, expression, token_colon, expression

def expression():   return conditional_expression
def top():          return expression, EOF

regex_match_regex = re.compile(r'/((?:(?:\/)|[^/])*)/([iLN]*)?')
unescape_sub_regex = re.compile(r'\\(.)')
OPTION_REGEXP_NORMALIZE = 1
OPTION_REGEXP_LOWER_CASE = 2

expression_parser = ParserPython(top, ws='\t\n\r 　', debug=DEBUG, memoization=True)

TRUE_VALUE = True
FALSE_VALUE = False
NONE_VALUE = None
EXPRESSION_VERSION = 3

def bool_to_value(b):
    return TRUE_VALUE if b else FALSE_VALUE

def set_version(version):
    global TRUE_VALUE, FALSE_VALUE, NONE_VALUE, EXPRESSION_VERSION
    EXPRESSION_VERSION = version
    if version <= 2:
        TRUE_VALUE = 1
        FALSE_VALUE = 0
        NONE_VALUE = 0
    else:
        TRUE_VALUE = True
        FALSE_VALUE = False
        NONE_VALUE = None

def value_to_str(value):
    if value is True:
        return 'true'
    elif value is False:
        return 'false'
    elif value is None:
        return 'null'
    else:
        return str(value)

def value_to_num(value):
    if value is True:
        return 1
    elif value is False:
        return 0
    elif value is None:
        return 0
    else:
        int_value = int(value)
        float_value = float(value)
        return int_value if int_value == float_value else float_value

def value_to_bool(value):
    return bool(value)

class ExpressionConverter(PTNodeVisitor):
    def node(self, node, children):
        children_list = tuple(children)
        is_terminal = len(children_list) == 0
        value = node.value if is_terminal else children_list
        if DEBUG:
            if is_terminal:
                print('Leaf<{}>({})'.format(node.rule_name, value))
            else:
                print('Node<{}>{}'.format(node.rule_name, value))
        return SyntaxNode(node.rule_name, is_terminal, value)

    def suppress(self, node, children):
        if len(children) == 0:
            return None
        elif len(children) == 1:
            return children[0]
        else:
            return self.node(node, children)

    def __getattr__(self, name):
        # 未定義のルールはデフォルト処理
        if name.startswith('visit_token_'):
            return self.suppress
        elif name.startswith('visit_'):
            return self.node
        else:
            raise AttributeError

    def visit_string_value(self, node, children):
        value = node.value[1:-1]
        value = unescape_sub_regex.sub(r'\1', value)
        node.value = value
        return self.node(node, children)

    def visit_number_value(self, node, children):
        if '.' in node.value:
            node.value = float(node.value)
        else:
            node.value = int(node.value)
        return self.node(node, children)

    def visit_boolean_value(self, node, children):
        is_true = (re.match(r'^(true|on)$', node.value, re.IGNORECASE) is not None)
        node.value = bool_to_value(is_true)
        return self.node(node, children)

    def visit_variable(self, node, children):
        value = normalize('NFKC', node.value).lower().strip()
        if re.match(r'^\$\d+$', value):
            value = int(value[1:])
        node.value = value
        return self.node(node, children)

    def visit_null_value(self, node, children):
        node.value = None
        return self.node(node, children)

    def visit_regex_match(self, node, children):
        m = regex_match_regex.match(node.value)
        option_str = m.group(2)
        regex_string = m.group(1)
        regex_option = re.DOTALL
        condition_option = []
        if option_str and 'i' in option_str:
            regex_option |= re.IGNORECASE
        if option_str and 'L' in option_str:
            condition_option.append(OPTION_REGEXP_LOWER_CASE)
        if option_str and 'N' in option_str:
            condition_option.append(OPTION_REGEXP_NORMALIZE)
        regex = re.compile(regex_string, regex_option)
        node.value = (regex, condition_option)
        return self.node(node, children)

    def visit_value(self, node, children):
        return self.suppress(node, children)

    def visit_sub_expression(self, node, children):
        return self.suppress(node, children)

    def visit_prod_op(self, node, children):
        return self.suppress(node, children)

    def visit_sum_op(self, node, children):
        return self.suppress(node, children)

    def visit_factor(self, node, children):
        return self.suppress(node, children)

    def visit_and_op(self, node, children):
        return self.suppress(node, children)

    def visit_or_op(self, node, children):
        return self.suppress(node, children)

    def visit_EOF(self, node, children):
        return self.suppress(node, children)

    def visit_top(self, node, children):
        return self.suppress(node, children)

    def visit_dict_entry(self, node, children):
        key = children[0]
        val = children[1]
        return (key, val)

    def visit_dict_value(self, node, children):
        if not children:
            return {}
        child = children[0] if isinstance(children, list) and len(children) == 1 else children
        if isinstance(child, SyntaxNode) and child.name == 'dict_body':
            body_children = child.children
            if not body_children:
                return {}
            first = body_children[0]
            if isinstance(first, SyntaxNode) and first.name == 'dict_literal_entries':
                # dict_literal_entries は (key,value) タプル列が children に積まれるので辞書へ再構成する
                result = {}
                for entry in first.children:
                    if isinstance(entry, tuple) and len(entry) == 2:
                        key_node, value_node = entry
                        result[key_node] = value_node
                return result
            if isinstance(first, tuple):
                # comprehension の場合、先頭が (key,value)、続く要素が comprehension ノードになる
                if len(body_children) > 1:
                    comp = body_children[1]
                    if isinstance(comp, SyntaxNode) and comp.name == 'dict_comprehension':
                        key_node, value_node = first
                        return SyntaxNode('dict_comprehension', False, [key_node, value_node, *comp.children])
        if isinstance(child, tuple) and len(child) == 2:
            # 単要素辞書（{expr: expr}）は reduce_tree 無しでも (key,value) が直接返ることがある
            key_node, value_node = child
            return {key_node: value_node}
        raise ValueError("dict_value: invalid children")

    def visit_list_value(self, node, children):
        if not children:
            return []
        child = children[0] if isinstance(children, list) and len(children) == 1 else children
        if isinstance(child, SyntaxNode) and child.name == 'list_body':
            body_children = child.children
            if not body_children:
                return []
            first = body_children[0]
            if isinstance(first, SyntaxNode) and first.name == 'list_literal_items':
                # list_literal_items はリスト要素が子ノードとして並ぶのでそのまま返す
                return list(first.children)
            if isinstance(first, SyntaxNode) and first.name == 'list_comprehension_clause':
                head_expr = first.children[0]
                comp = first.children[1]
                return SyntaxNode('list_comprehension', False, [head_expr, *comp.children])
            if len(body_children) > 1:
                head_expr = first
                comp = body_children[1]
                if isinstance(comp, SyntaxNode) and comp.name == 'list_comprehension':
                    return SyntaxNode('list_comprehension', False, [head_expr, *comp.children])
        return children

    def visit_list_comprehension(self, node, children):
        return self.node(node, children)

    def visit_dict_comprehension(self, node, children):
        return self.node(node, children)

    def visit_conditional_expression(self, node, children):
        return self.node(node, children)

    def visit_conditional_operator(self, node, children):
        return self.node(node, children)

    def visit_condition(self, node, children):
        return self.suppress(node, children)

    def visit_expression(self, node, children):
        return self.suppress(node, children)

class Expression:
    def __init__(self):
        self.expr = None

    @classmethod
    def from_str(cls, s):
        self = cls()
        expr = expression_parser.parse(s)
        self.expr = visit_parse_tree(expr, ExpressionConverter())
        self._s = s
        return self

    def eval(self, env, matches=[], set_var = None, set_list = None, set_dict = None):
        if set_var is None:
            set_var = lambda k, v: None
        if set_list is None:
            set_list = lambda l, i, v: None
        if set_dict is None:
            set_dict = lambda d, k, v: None
        try:
            return ExpressionEvaluator(env, matches, set_var, set_list, set_dict, debug=DEBUG).eval(self.expr)
        except Exception as e:
            logging.error(f'Error evaluating expression: {self._s}\n{e}')
            return None

    def eval_assignment(self, new_value, env, matches, set_var, set_list, set_dict):
        try:
            return ExpressionEvaluator(env, matches, set_var, set_list, set_dict, debug=DEBUG).eval_assignment(self.expr, new_value)
        except Exception as e:
            logging.error(f'Error evaluating assignment: {self._s}\n{e}')
            return None

    def __repr__(self):
        return f'"{self._s}"'


class ExpressionEvaluator(SyntaxTreeEvaluator):
    def __init__(self, env, matches, set_var, set_list, set_dict, debug=False, **kwargs):
        self.local_env = {}
        self.env = env
        self.matches = matches
        self.set_var = set_var
        self.set_list = set_list
        self.set_dict = set_dict
        self.debug = debug
        super(ExpressionEvaluator, self).__init__(**kwargs)

    def eval(self, node):
        if isinstance(node, dict):
            return { self.eval(k) if isinstance(k, SyntaxNode) else k:
                     self.eval(v) for k, v in node.items() }
        elif isinstance(node, list):
            return [self.eval(item) for item in node]
        elif isinstance(node, SyntaxNode):
            return super(ExpressionEvaluator, self).eval(node)
        else:
            return node

    def visit_top(self, node):
        children = self.eval_children(node)
        if len(children) > 0:
            #print("top: {}".format(children[0]))
            return children[0]
        raise NotImplementedError

    def visit_or_op(self, node):
        if len(node.children) == 1:
            return self.eval(node.children[0])
        # OR
        for child in node.children:
            if value_to_bool(self.eval(child)):
                return TRUE_VALUE
        return FALSE_VALUE

    def visit_and_op(self, node):
        if len(node.children) == 1:
            return self.eval(node.children[0])
        # AND
        for child in node.children:
            if not value_to_bool(self.eval(child)):
                return FALSE_VALUE
        return TRUE_VALUE

    def visit_regex_apply(self, node):
        children = self.eval_children(node)
        target_string = children[0]
        regex, options = children[1]
        if OPTION_REGEXP_NORMALIZE in options:
            target_string = normalize('NFKC', target_string)
        if OPTION_REGEXP_LOWER_CASE in options:
            target_string = target_string.lower()
        m = regex.search(target_string)
        if m:
            return TRUE_VALUE
        else:
            return FALSE_VALUE

    def visit_comparison(self, node):
        lhs, op, rhs = self.eval_children(node)
        #print('compare {} {} {}'.format(lhs, op, rhs))
        if lhs is None or rhs is None:
            if op == '==':
                return bool_to_value(lhs is None and rhs is None)
            elif op == '!=':
                return bool_to_value(not (lhs is None and rhs is None))
            else:
                return FALSE_VALUE
        # もしも lhs と rhs の型が違ったら、両方とも数値であれば float に変換し、そうでなければ str 型に両者を揃える
        if type(lhs) != type(rhs):
            if isinstance(lhs, (int, float)) and isinstance(rhs, (int, float)):
                lhs, rhs = value_to_num(lhs), value_to_num(rhs)
            else:
                lhs, rhs = value_to_str(lhs), value_to_str(rhs)
        if op == '==':
            return bool_to_value(lhs == rhs)
        elif op == '!=':
            return bool_to_value(lhs != rhs)
        elif op == '<':
            return bool_to_value(lhs < rhs)
        elif op == '>':
            return bool_to_value(lhs > rhs)
        elif op == '>=':
            return bool_to_value(lhs >= rhs)
        elif op == '<=':
            return bool_to_value(lhs <= rhs)
        else:
            raise NotImplementedError("unknown op: {}".format(op))

    def visit_prod_op(self, node):
        # */%
        result = self.eval(node.children[0])
        index = 1
        while index + 1 < len(node.children):
            op = self.eval(node.children[index])
            value = self.eval(node.children[index+1])
            if op == '*':
                result = value_to_num(result) * value_to_num(value)
            elif op == '/':
                if EXPRESSION_VERSION <= 2:
                    result = value_to_num(result) // value_to_num(value)
                else:
                    result = value_to_num(result) / value_to_num(value)
            elif op == '%':
                result = value_to_num(result) % value_to_num(value)
            else:
                raise NotImplementedError("unknown op: {}".format(op))
            index += 2
        return result

    def visit_sum_op(self, node):
        # +-
        result = self.eval(node.children[0])
        index = 1
        while index + 1 < len(node.children):
            op = self.eval(node.children[index])
            value = self.eval(node.children[index+1])
            if op == '+':
                if result is None or isinstance(result, (int, float, bool)):
                    result = value_to_num(result) + value_to_num(value)
                elif isinstance(result, list):
                    # リスト結合
                    if not isinstance(value, list):
                        value = [value]
                    result = result + value
                elif isinstance(result, dict):
                    # 辞書結合
                    if isinstance(value, dict):
                        result = result.copy()
                        for k, v in value.items():
                            result[k] = v
                    else:
                        result = value_to_str(result) + value_to_str(value)
                else:
                    result = value_to_str(result) + value_to_str(value)
            elif op == '-':
                if result is None or isinstance(result, (int, float, bool)):
                    result = value_to_num(result) - value_to_num(value)
                elif isinstance(result, list):
                    # リストから要素を削除
                    result = result.copy()
                    if isinstance(value, list):
                        for v in value:
                            result.remove(v)
                    else:
                        result.remove(value)
                elif isinstance(result, dict):
                    result = result.copy()
                    # 辞書から要素を削除
                    if isinstance(value, dict):
                        for k in value.keys():
                            result.pop(value_to_str(k), None)
                    else:
                        result.pop(value_to_str(value), None)
                else:
                    # 文字列から部分文字列を削除
                    result = value_to_str(result).replace(value_to_str(value), '')
            else:
                raise NotImplementedError("unknown op: {}".format(op))
            index += 2
        return result

    def visit_unary_op(self, node):
        op, value = self.eval_children(node)
        if op == '+':
            return value_to_num(value)
        elif op == '-':
            return -value_to_num(value)
        elif op == '!':
            return bool_to_value(not value_to_bool(value))
        else:
            raise NotImplementedError("unknown op: {}".format(op))

    def visit_variable(self, node):
        if isinstance(node.value, int):
            value = safe_list_get(self.matches, node.value, NONE_VALUE)
        elif node.value in self.local_env:
            value = self.local_env[node.value]
        else:
            value = self.env.get(node.value, NONE_VALUE)
        if self.debug:
            print('variable {} is {}'.format(node.value, value))
        return value

    def visit_value(self, node):
        # node.children[0] は primary_value の評価結果、
        # その後の子要素は get_item 演算（各 get_item ノードの children[0] がキー）
        result = self.eval(node.children[0])
        for get_item_node in node.children[1:]:
            key = self.eval(get_item_node.children[0])
            if isinstance(result, dict):
                # 辞書の key は必ず文字列化する
                key = value_to_str(key)
                result = result.get(key, NONE_VALUE)
            elif isinstance(result, list):
                key = value_to_num(key)
                try:
                    result = result[key]
                except IndexError:
                    result = NONE_VALUE
            else:
                result = NONE_VALUE
        return result

    def visit_list_comprehension(self, node):
        # [expr, variable, collection_expr]
        expr = node.children[0]
        var_node = node.children[1]
        collection = self.eval(node.children[2])
        filter_expr = node.children[3] if len(node.children) > 3 else None
        result = []
        var_name = var_node.value
        for item in collection:
            self.local_env[var_name] = item
            if filter_expr is None or value_to_bool(self.eval(filter_expr)):
                result.append(self.eval(expr))
        if var_name in self.local_env:
            del self.local_env[var_name]
        return result

    def visit_dict_comprehension(self, node):
        # [key_expr, value_expr, variable, collection_expr]
        key_expr = node.children[0]
        value_expr = node.children[1]
        var_node = node.children[2]
        collection = self.eval(node.children[3])
        filter_expr = node.children[4] if len(node.children) > 4 else None
        result = {}
        var_name = var_node.value
        for item in collection:
            self.local_env[var_name] = item
            key_val = self.eval(key_expr)
            value_val = self.eval(value_expr)
            if filter_expr is None or value_to_bool(self.eval(filter_expr)):
                result[key_val] = value_val
        if var_name in self.local_env:
            del self.local_env[var_name]
        return result

    def visit_expression(self, node):
        if len(node.children) == 2 and node.children[1].name == 'conditional_operation':
            # 三項演算子
            condition = self.eval(node.children[0])
            true_expr = self.eval(node.children[1].children[0])
            false_expr = self.eval(node.children[1].children[1])
            return true_expr if value_to_bool(condition) else false_expr
        else:
            # 通常の式
            return self.eval(node.children[0])

    #TODO: システム変数を上書きされないように
    def eval_assignment(self, lhs_node, value):
        if len(lhs_node.children) == 1:
            primary = lhs_node.children[0]
            if primary.name == 'primary_value':
                if len(primary.children) != 1:
                    raise ValueError("代入先は変数である必要があります")
                primary = primary.children[0]
            if primary.name == 'variable':
                var_name = primary.value
                if self.debug:
                    print(f"var[{var_name}] = {value}")
                self.set_var(var_name, value)
                return value
            else:
                raise ValueError("代入先は変数である必要があります" + str(primary))

        container = self.eval(lhs_node.children[0])
        for get_item_node in lhs_node.children[1:-1]:
            key = self.eval(get_item_node.children[0])
            if isinstance(container, dict):
                key = value_to_str(key) # 辞書のキーは文字列化する
                if key not in container:
                    # 存在しないキーの場合は dict に追加
                    self.set_dict(container, key, {})
                container = container[key]
            elif isinstance(container, list):
                key = value_to_num(key) # リストのインデックスは整数化する
                try:
                    container = container[key]
                except IndexError:
                    raise ValueError("リストのインデックスが範囲外です: index={}".format(key))
            else:
                raise ValueError("代入できない型です")

        last_get_item = lhs_node.children[-1]
        key = self.eval(last_get_item.children[0])
        if isinstance(container, dict):
            if self.debug:
                print(f"dict[{key}] = {value}")
            key = value_to_str(key) # 辞書のキーは文字列化する
            self.set_dict(container, key, value)
            return value
        elif isinstance(container, list):
            key = value_to_num(key) # リストのインデックスは整数化する
            if self.debug:
                print(f"list[{key}] = {value}")
            self.set_list(container, key, value)
            return value
        else:
            raise ValueError("代入できない型です")
