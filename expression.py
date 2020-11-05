# coding: utf-8
import re
import string
from unicodedata import normalize

from arpeggio import ParserPython, PTNodeVisitor, visit_parse_tree, Optional, ZeroOrMore, OneOrMore, EOF
from arpeggio import RegExMatch as _

from syntax_tree import SyntaxNode, SyntaxTreeEvaluator
from utility import safe_list_get


DEBUG = False

def token_and():  return u'&&'
def token_or():  return u'||'
def token_lparen():  return u'('
def token_rparen(): return u')'
def token_op_regex(): return u'=~'
def op_eq():        return u'=='
def op_neq():       return u'!='
def boolean_value(): return _(ur'true|on|false|off', ignore_case=True)
def number_value(): return _(ur'[-+]?\d+')
def string_value(): return _(ur'"(\\.|[^"])*"')
def variable():     return _(ur'[\$＄]{1,2}[^\s+\-*/\\"=~<>,\.\[\]@;:`{}!#$%&()^|?＋−＊／”＝〜＜＞，．［］＠；：｀｛｝！＃＄％＆（）＾｜？]+')
def unary_op():     return _(ur'[-+!]'), value
def sub_expression(): return token_lparen, or_op, token_rparen
def value():       return [unary_op, variable, string_value, number_value, boolean_value, sub_expression]
def prod_op():      return OneOrMore(value, sep=_(ur'[*/]'))
def sum_op():       return OneOrMore(prod_op, sep=_(ur'[-+]'))
def regex_match():  return _(ur'/(\\/|[^/])*/[iLN]*')
def regex_apply():  return sum_op, token_op_regex, regex_match
def comparison():   return sum_op, [op_eq, op_neq], sum_op
def factor():       return [regex_apply, comparison, sum_op]
def and_op():       return OneOrMore(factor, sep=token_and)
def or_op():        return OneOrMore(and_op, sep=token_or)
def top():          return or_op, EOF

regex_match_regex = re.compile(ur'/((?:(?:\/)|[^/])*)/([iLN]*)?')
unescape_sub_regex = re.compile(ur'\\(.)')
OPTION_REGEXP_NORMALIZE = 1
OPTION_REGEXP_LOWER_CASE = 2

expression_parser = ParserPython(top, ws=u'\t\n\r 　', debug=DEBUG)

INT_TRUE = 1
INT_FALSE = 0

def bool_to_int(b):
    return INT_TRUE if b else INT_FALSE

class ExpressionConverter(PTNodeVisitor):
    def node(self, node, children):
        children_list = tuple(children)
        is_terminal = len(children_list) == 0
        value = node.value if is_terminal else children_list
        if DEBUG:
            if is_terminal:
                print(u'Leaf<{}>({})'.format(node.rule_name, value))
            else:
                print(u'Node<{}>{}'.format(node.rule_name, value))
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
        node.value = int(node.value)
        return self.node(node, children)

    def visit_boolean_value(self, node, children):
        is_true = (re.match(ur'^(true|on)$', node.value, re.IGNORECASE) is not None)
        node.value = bool_to_int(is_true)
        return self.node(node, children)

    def visit_variable(self, node, children):
        value = normalize('NFKC', node.value).lower().strip()
        if re.match(ur'^\$\d+$', value):
            value = int(value[1:])
        node.value = value
        return self.node(node, children)

    def visit_regex_match(self, node, children):
        m = regex_match_regex.match(node.value)
        option_str = m.group(2)
        regex_string = m.group(1)
        regex_option = 0
        condition_option = []
        if option_str and u'i' in option_str:
            regex_option = re.IGNORECASE
        if option_str and u'L' in option_str:
            condition_option.append(OPTION_REGEXP_LOWER_CASE)
        if option_str and u'N' in option_str:
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
        if DEBUG:
            return self.node(node, children)
        else:
            return self.suppress(node, children)


class Expression(object):
    def __init__(self):
        self.expr = None

    @classmethod
    def from_str(cls, s):
        self = cls()
        expr = expression_parser.parse(s)
        self.expr = visit_parse_tree(expr, ExpressionConverter())
        return self

    def eval(self, env, matches=[]):
        return ExpressionEvaluator(env, matches, debug=DEBUG).eval(self.expr)


class ExpressionEvaluator(SyntaxTreeEvaluator):
    def __init__(self, env, matches, debug=False, **kwargs):
        self.env = env
        self.matches = matches
        self.debug = debug
        super(ExpressionEvaluator, self).__init__(**kwargs)

    def visit_top(self, node):
        children = self.eval_children(node)
        if len(children) > 0:
            #print(u"top: {}".format(children[0]))
            return children[0]
        raise NotImplementedError

    def visit_or_op(self, node):
        if len(node.children) == 1:
            return self.eval(node.children[0])
        # OR
        for child in node.children:
            result = self.eval(child)
            if result:
                return INT_TRUE
        return INT_FALSE

    def visit_and_op(self, node):
        if len(node.children) == 1:
            return self.eval(node.children[0])
        # AND
        for child in node.children:
            result = self.eval(child)
            if not result:
                return INT_FALSE
        return INT_TRUE

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
            return INT_TRUE
        else:
            return INT_FALSE

    def visit_comparison(self, node):
        lhs, op, rhs = self.eval_children(node)
        #print(u'compare {} {} {}'.format(lhs, op, rhs))
        if op == u'==':
            return bool_to_int(lhs == rhs)
        elif op == u'!=':
            return bool_to_int(lhs != rhs)
        else:
            raise NotImplementedError(u"unknown op: {}".format(op))

    def visit_prod_op(self, node):
        # */
        result = self.eval(node.children[0])
        index = 1
        while index+1 < len(node.children):
            op = self.eval(node.children[index])
            value = self.eval(node.children[index+1])
            if op == u'*':
                result = int(result) * int(value)
            elif op == u'/':
                result = int(result) / int(value)
            else:
                raise NotImplementedError(u"unknown op: {}".format(op))
            index += 2
        return result

    def visit_sum_op(self, node):
        # +-
        result = self.eval(node.children[0])
        index = 1
        while index+1 < len(node.children):
            op = self.eval(node.children[index])
            value = self.eval(node.children[index+1])
            if op == u'+':
                if isinstance(result, int):
                    result = result + int(value)
                elif isinstance(result, unicode):
                    # 文字列結合
                    result = result + unicode(value)
                else:
                    raise ValueError(u"invalid type: {}".format(result))
            elif op == u'-':
                result = int(result) - int(value)
            else:
                raise NotImplementedError(u"unknown op: {}".format(op))
            index += 2
        return result

    def visit_unary_op(self, node):
        op, value = self.eval_children(node)
        if op == u'+':
            return int(value)
        elif op == u'-':
            return -int(value)
        elif op == u'!':
            #print(u'not {} is {}'.format(value, not value))
            return bool_to_int(not value)
        else:
            raise NotImplementedError(u"unknown op: {}".format(op))

    def visit_variable(self, node):
        if isinstance(node.value, int):
            value = safe_list_get(self.matches, node.value, 0)
        else:
            value = self.env.get(node.value, 0)
        if self.debug:
            print(u'variable {} is {}'.format(node.value, value))
        return value
