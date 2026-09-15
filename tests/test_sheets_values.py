"""通信せず、型の維持・TSVの引用・編集時の値変換を確認する。"""

import copy
from pathlib import Path
import tempfile
import unittest

from plugin.tsv_values import (
    content_hash, local_values, normalize, read_tsv, to_text, write_tsv,
)


class SheetsValuesTest(unittest.TestCase):
    def test_末尾の未設定だけを除き内部空行と空文字型は保つ(self):
        rows = [
            [{}, {'stringValue': '本文'}, {}, {}],
            [{}],
            [{'stringValue': ''}, {}],
            [], [{}],
        ]
        original = copy.deepcopy(rows)

        result = normalize(rows)

        self.assertEqual([[{}, {'stringValue': '本文'}], [], [{'stringValue': ''}]], result)
        self.assertEqual(original, rows)
        result[0][1]['stringValue'] = '変更'
        self.assertEqual(original, rows)
        self.assertEqual([], normalize([[{}], []]))

    def test_hashは数値表現差を吸収し型と内部位置は区別する(self):
        self.assertEqual(content_hash([[{'numberValue': 1.0}]]), content_hash([[{'numberValue': 1}, {}], []]))
        self.assertEqual(content_hash([[{'numberValue': -0.0}]]), content_hash([[{'numberValue': 0}]]))
        variants = [
            [], [[{'stringValue': ''}]], [[{'stringValue': '1'}]],
            [[{'numberValue': 1}]], [[{'boolValue': True}]],
            [[{'stringValue': '=A1'}]], [[{'formulaValue': '=A1'}]],
            [[], [{'stringValue': '1'}]],
        ]
        self.assertEqual(len(variants), len({content_hash(rows) for rows in variants}))

    def test_表示は型を推測せず既存loaderの文字列表現を使う(self):
        values = [
            ({}, ''), ({'stringValue': '001'}, '001'),
            ({'stringValue': 'TRUE'}, 'TRUE'), ({'stringValue': '=1+2'}, '=1+2'),
            ({'numberValue': 1.0}, '1.0'), ({'numberValue': 45292.5}, '45292.5'),
            ({'boolValue': True}, 'True'), ({'boolValue': False}, 'False'),
            ({'formulaValue': '=A1&"さん"'}, '=A1&"さん"'),
        ]
        for cell, expected in values:
            with self.subTest(cell=cell):
                self.assertEqual(expected, to_text(cell))

    def test_TSVの引用と改行を往復して未変更セルの型を保つ(self):
        rows = [
            [{'stringValue': '001'}, {'numberValue': 3.5}, {'boolValue': True},
             {'formulaValue': '=A2&"さん"'}, {'stringValue': '=1+2'}],
            [],
            [{'stringValue': '改行\nCR\rTAB\t"引用"'}, {'stringValue': ''}],
            [{}], [],
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'story.tsv'
            write_tsv(path, rows)
            text_rows = read_tsv(path)

        self.assertEqual([
            ['001', '3.5', 'True', '=A2&"さん"', '=1+2'], [],
            ['改行\nCR\rTAB\t"引用"', ''],
        ], text_rows)
        self.assertEqual(normalize(rows), local_values(text_rows, rows))

    def test_元文字列の数値とbooleanは型を保ち編集した式は式になる(self):
        base = [[{'stringValue': '001'}, {'stringValue': 'TRUE'}, {'stringValue': '=1+2'}]]
        self.assertEqual([
            [{'stringValue': '002'}, {'stringValue': 'FALSE'}, {'formulaValue': '=3+4'}],
        ], local_values([['002', 'FALSE', '=3+4']], base))

    def test_元の数値とbooleanと式は同種の有効な表記を保つ(self):
        base = [[{'numberValue': 1}, {'boolValue': True}, {'formulaValue': '=A1'}]]
        self.assertEqual([
            [{'numberValue': 20}, {'boolValue': False}, {'formulaValue': '=A2&"次"'}],
        ], local_values([[' +02.00e1 ', ' false ', '=A2&"次"']], base))
        self.assertEqual([[{'numberValue': 0.5}]], local_values([['.5']], [[{'numberValue': 0}]]))

    def test_新規セルは先頭イコールだけ式にして数値文字列は変換しない(self):
        self.assertEqual([[
            {'stringValue': '001'}, {'stringValue': 'TRUE'}, {'stringValue': '2026-01-09'},
            {'formulaValue': '=IMAGE("https://example.invalid/image.png")'},
            {'formulaValue': '=A1'},
        ]], local_values([['001', 'TRUE', '2026-01-09', '=IMAGE("https://example.invalid/image.png")', ' =A1']], []))

    def test_空欄と削除した末尾はclearにし元の空文字型は未変更なら保持する(self):
        base = [[{'stringValue': '本文'}, {'numberValue': 1}], [{'boolValue': True}]]
        self.assertEqual([[{'stringValue': '本文'}]], local_values([['本文', '']], base))
        self.assertEqual([], local_values([], base))
        self.assertEqual([[{'stringValue': ''}]], local_values([['']], [[{'stringValue': ''}]]))
        self.assertEqual([], local_values([['']], [[{'stringValue': '本文'}]]))
        self.assertEqual([], local_values([], [[{'stringValue': ''}]]))
        self.assertEqual([[{'stringValue': '本文'}]], local_values(
            [['本文']], [[{'stringValue': '本文'}, {'stringValue': ''}]]))

    def test_元の数値や真偽値に合わない編集は文字列として受け入れる(self):
        for cell, text in (
                ({'numberValue': 1}, '本文'), ({'numberValue': 1}, '1,000'),
                ({'numberValue': 1}, 'NaN'),
                ({'boolValue': True}, 'yes')):
            with self.subTest(cell=cell, text=text):
                self.assertEqual([[{'stringValue': text}]], local_values([[text]], [[cell]]))

    def test_数値構文の非有限数は座標付きで拒否する(self):
        for text in ('1e999', '-1e999'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, 'B2:'):
                    local_values([[], ['', text]], [[], [{}, {'numberValue': 1}]])

    def test_式と台詞を入れ替え行を移しても式は式として扱う(self):
        expression = '=A1&"さん"'
        base = [[{'stringValue': '開始'}, {'stringValue': '本文'}],
                [{}, {'formulaValue': expression}], [{}, {'stringValue': '終わり'}]]
        result = local_values([['開始', '本文'], [], ['', expression], ['', '終わり']], base)
        self.assertEqual({'formulaValue': expression}, result[2][1])
        self.assertEqual([[{'stringValue': '普通の台詞'}]],
                         local_values([['普通の台詞']], [[{'formulaValue': expression}]]))
        self.assertEqual([[{'formulaValue': '=A1'}]],
                         local_values([[' \t=A1']], [[{'numberValue': 1}]]))

    def test_不正な型付き値は拒否する(self):
        for cell in (
                None, {'errorValue': {}}, {'stringValue': 'x', 'boolValue': True},
                {'numberValue': True}, {'numberValue': float('nan')},
                {'numberValue': float('inf')}, {'numberValue': 10 ** 1000},
                {'boolValue': 1}, {'stringValue': 123}, {'formulaValue': 'A1'}):
            with self.subTest(cell=cell):
                with self.assertRaisesRegex(ValueError, 'A1:'):
                    normalize([[cell]])

    def test_TSVは26列上限とUTF8とstrictな引用を検査する(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'story.tsv'
            path.write_text('\t'.join(['x'] * 26) + '\n', encoding='utf-8')
            self.assertEqual([['x'] * 26], read_tsv(path))
            path.write_text('\t'.join([''] * 27) + '\n', encoding='utf-8')
            self.assertEqual([[''] * 26], read_tsv(path))
            for extra in ('値', '0', ' '):
                path.write_text('\t' * 26 + extra + '\n', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'AA1'):
                    read_tsv(path)
            path.write_text('開始\t"一行目\n二行目"\n次へ\t"閉じていない', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, ':3行目:'):
                read_tsv(path)
            path.write_bytes(b'\xff')
            with self.assertRaises(UnicodeDecodeError):
                read_tsv(path)

    def test_同期範囲外の空paddingだけを無視し候補を膨らませない(self):
        text_rows = [['本文'] + [''] * 16383]
        self.assertEqual([[{'stringValue': '本文'}]], local_values(text_rows, []))
        self.assertEqual(16384, len(text_rows[0]))
        self.assertEqual([[{'stringValue': '本文'}]],
                         normalize([[{'stringValue': '本文'}] + [{}] * 16383]))
        for extra in (' ', '0', {'numberValue': 0}, {'boolValue': False},
                      {'stringValue': ''}, None, 0):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(ValueError, 'AA1'):
                    normalize([[{}] * 26 + [extra]])

    def test_親directoryの作成は呼出し側に任せる(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing' / 'story.tsv'
            with self.assertRaises(FileNotFoundError):
                write_tsv(path, [[{'stringValue': '本文'}]])
            self.assertFalse(path.parent.exists())


if __name__ == '__main__':
    unittest.main()
