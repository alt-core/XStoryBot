# coding: utf-8
import re
import string
from unicodedata import normalize

from utility import safe_list_get


class SyntaxNode(object):
    __slots__ = ['name', 'is_terminal', 'value']
    def __init__(self, name, is_terminal, value):
        self.name = name
        self.is_terminal = is_terminal
        self.value = value

    @property
    def children(self):
        if self.is_terminal:
            raise RuntimeError
        return self.value

    def __repr__(self):
        if self.is_terminal:
            return repr(self.value)
        else:
            return u'{}({})'.format(self.name, u','.join(map(repr, self.children)))

    def __getstate__(self):
        return { name: getattr(self, name) for name in self.__slots__ }

    def __setstate__(self, st):
        for name in st:
            setattr(self, name, st[name])


class SyntaxTreeEvaluator(object):
    def __init__(self):
        self.root = None

    def eval(self, node):
        func_name = 'visit_' + node.name
        if hasattr(self, func_name):
            return getattr(self, func_name)(node)
        else:
            if node.is_terminal:
                return node.value
            elif (not node.is_terminal) and len(node.children) == 1:
                return self.eval(node.children[0])
            else:
                print('visitor for rule {} is not found'.format(node.name))
                raise NotImplementedError

    def eval_children(self, node):
        if node.is_terminal:
            raise RuntimeError
        return map(self.eval, node.children)

