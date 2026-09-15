"""TSVのセル参照と文字列連結を、外部通信や数式ライブラリなしで解決する。"""

import re


_OPERAND = re.compile(
    r'\s*(?:"(?P<text>(?:[^"]|"")*)"|'
    r"(?:(?:'(?P<quoted>(?:[^']|'')*)'|(?P<sheet>\w+))!)?"
    r'\$?(?P<column>[A-Za-z]+)\$?(?P<row>[1-9][0-9]*))\s*')
_IMAGE = re.compile(r'=\s*IMAGE\s*\(', re.IGNORECASE)


def is_formula(value):
    value = value.lstrip()
    return value.startswith('=') and not _IMAGE.match(value)


def _column_name(index):
    result = ''
    while index >= 0:
        index, remainder = divmod(index, 26)
        result = chr(65 + remainder) + result
        index -= 1
    return result


class FormulaEvaluator:
    def __init__(self, read_sheet, sheet_names, literal_cells=None):
        self.read_sheet = read_sheet
        self.sheet_names = sheet_names
        self.literal_cells = set() if literal_cells is None else literal_cells
        self.results = {}

    def _error(self, key, message):
        title, row, column = key
        source = self.read_sheet(title)[row]
        position = f'{title}!{_column_name(column)}{row + 1}'
        return ValueError(
            f'TSV式エラー: {position} '
            f'({source.source_path}:{source.source_position[1] + 1}行目): {message}')

    def _cell(self, key):
        title, row, column = key
        rows = self.read_sheet(title)
        if row >= len(rows) or column >= len(rows[row]):
            return ''
        return rows[row][column]

    def _parts(self, key, value):
        expression = value.lstrip()[1:].strip()
        position = 0
        while position < len(expression):
            match = _OPERAND.match(expression, position)
            if match is None:
                break
            if match['text'] is not None:
                yield match['text'].replace('""', '"')
            else:
                title = key[0]
                if match['quoted'] is not None:
                    title = match['quoted'].replace("''", "'")
                elif match['sheet'] is not None:
                    title = match['sheet']
                if title not in self.sheet_names:
                    raise self._error(key, f'参照先シートがmanifestにありません: {title}')
                column = 0
                for letter in match['column'].upper():
                    column = column * 26 + ord(letter) - ord('A') + 1
                yield title, int(match['row']) - 1, column - 1
            position = match.end()
            if position == len(expression):
                return
            if expression[position] != '&':
                break
            position += 1
        raise self._error(key, '対応する式はセル参照・二重引用符の文字列・&による連結だけです')

    def evaluate(self, title, row, column):
        key = (title, row, column)
        if key in self.results:
            return self.results[key]
        value = self._cell(key)
        if key in self.literal_cells or not is_formula(value):
            return value

        # 参照の深さをPythonの再帰上限へ結び付けず、式セルだけ結果を保持する。
        active = {key}
        stack = [(key, self._parts(key, value), [])]
        while stack:
            current, parts, fragments = stack[-1]
            try:
                part = next(parts)
            except StopIteration:
                result = ''.join(fragments)
                self.results[current] = result
                active.remove(current)
                stack.pop()
                if stack:
                    stack[-1][2].append(result)
                continue
            if isinstance(part, str):
                fragments.append(part)
            elif part in self.results:
                fragments.append(self.results[part])
            elif part in active:
                target = f'{part[0]}!{_column_name(part[2])}{part[1] + 1}'
                raise self._error(current, f'循環参照があります: {target}')
            else:
                value = self._cell(part)
                if part not in self.literal_cells and is_formula(value):
                    active.add(part)
                    stack.append((part, self._parts(part, value), []))
                else:
                    fragments.append(value)
        return self.results[key]
