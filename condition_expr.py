# coding: utf-8
import re
import string
from unicodedata import normalize

from arpeggio import ParserPython, PTNodeVisitor, visit_parse_tree, Optional, ZeroOrMore, OneOrMore, EOF
from arpeggio import RegExMatch as _

from syntax_tree import SyntaxNode, SyntaxTreeEvaluator



DEBUG = False

def token_and():  return _(ur"[&＆]")
def token_or():  return _(ur"[|｜]")
def token_lparen():  return _(ur"[(（]")
def token_rparen():  return _(ur"[])）]")
def regex_match(): return _(ur'/(\\/|[^/])*/[iLN]*')
def string_match(): return _(ur'(\\.|[^&＆\|｜\)）])*')
def sub_expression(): return token_lparen, expression, token_rparen
def factor():     return [sub_expression, regex_match, string_match]
def term():       return OneOrMore(factor, sep=token_and)
def expression(): return ZeroOrMore(term, sep=token_or)
def top():        return expression, EOF

regex_match_regex = re.compile(ur'/((?:(?:\/)|[^/])*)/([iLN]*)?')
unescape_sub_regex = re.compile(ur'\\(.)')
OPTION_REGEXP_NORMALIZE = 1
OPTION_REGEXP_LOWER_CASE = 2

expression_parser = ParserPython(top, ws=u'\t\n\r 　', debug=DEBUG)

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
            raise RuntimeError

    def __getattr__(self, name):
        # 未定義のルールはデフォルト処理
        if name.startswith('visit_token_'):
            # token_ とついているルールは省略する
            return self.suppress
        elif name.startswith('visit_'):
            return self.node
        else:
            raise AttributeError

    def visit_string_match(self, node, children):
        value = node.value
        value = unescape_sub_regex.sub(r'\1', value)
        value = normalize('NFKC', value).lower().strip()
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


class ConditionExpression(object):
    def __init__(self):
        self.expr = None

    @classmethod
    def from_str(cls, s):
        self = cls()
        expr = expression_parser.parse(s)
        self.expr = visit_parse_tree(expr, ExpressionConverter())
        return self

    def eval(self, env, matches=[]):
        return ExpressionEvaluator(env, matches).eval(self.expr)

    def check(self, action):
        action_normalized = normalize('NFKC', action).lower()
        result, matched = ConditionExpressionEvaluator(action, action_normalized).eval(self.expr)
        if result:
            return matched
        else:
            return None


class ConditionExpressionEvaluator(SyntaxTreeEvaluator):
    def __init__(self, action, action_normalized, **kwargs):
        self.action = action
        self.action_normalized = action_normalized
        super(ConditionExpressionEvaluator, self).__init__(**kwargs)

    def visit_top(self, node):
        children = self.eval_children(node)
        if len(children) > 0:
            return children[0]
        return (False, ('',))

    def visit_expression(self, node):
        matched = ('',)
        flag = False
        for child in node.children:
            result, sub_matched = self.eval(child)
            if result:
                flag = True
                if len(matched[0]) < len(sub_matched[0]):
                    matched = sub_matched
        return (flag, matched)

    def visit_term(self, node):
        matched = ['']
        for child in node.children:
            result, sub_matched = self.eval(child)
            if not result:
                return (False, ('',))
            matched[0] = matched[0] + sub_matched[0]
            matched.extend(sub_matched[1:])
        return (True, tuple(matched))

    def visit_string_match(self, node):
        value = node.value
        if value in self.action_normalized:
            #print('%s found in %s' % (value, self.action_normalized))
            return (True, (node.value,))
        else:
            #print('%s not found in %s' % (value, self.action_normalized))
            return (False, ('',))

    def visit_regex_match(self, node):
        target_string = self.action
        regex = node.value[0]
        options = node.value[1]
        if OPTION_REGEXP_NORMALIZE in options:
            target_string = normalize('NFKC', target_string)
        if OPTION_REGEXP_LOWER_CASE in options:
            target_string = target_string.lower()
        m = regex.search(target_string)
        if m:
            return (True, (m.group(0),) + m.groups())
        else:
            return (False, ('',))

