import unittest

from expression import Expression, set_version


class ExpressionParseTest(unittest.TestCase):
    def setUp(self):
        # version 3 以降の真偽値変換（bool）がデフォルトになるように設定
        set_version(3)

    def test_literal_dict_and_list(self):
        expr = Expression.from_str('{"a": 1, "b": 2, "c": [3, 4]}')
        result = expr.eval({}, [])
        self.assertEqual(result, {"a": 1, "b": 2, "c": [3, 4]})

    def test_empty_structures(self):
        self.assertEqual(Expression.from_str('{}').eval({}, []), {})
        self.assertEqual(Expression.from_str('[]').eval({}, []), [])

    def test_arithmetic_and_string_operations(self):
        expr = Expression.from_str('("Hello, " + $name) + "!"')
        result = expr.eval({"$name": "世界"}, [])
        self.assertEqual(result, "Hello, 世界!")

        sum_expr = Expression.from_str('10 + 5 * 2 - 4')
        self.assertEqual(sum_expr.eval({}, []), 16)

    def test_list_and_dict_concatenation(self):
        list_expr = Expression.from_str('[1, 2] + [3, 4]')
        self.assertEqual(list_expr.eval({}, []), [1, 2, 3, 4])

        dict_expr = Expression.from_str('{"a": 1} + {"b": $value}')
        self.assertEqual(dict_expr.eval({"$value": 2}, []), {"a": 1, "b": 2})

    def test_nested_index_access(self):
        expr = Expression.from_str('{"selected": $data["items"][1]["name"]}')
        env = {
            "$data": {
                "items": [
                    {"name": "first"},
                    {"name": "second"},
                    {"name": "third"}
                ]
            }
        }
        self.assertEqual(expr.eval(env, []), {"selected": "second"})

    def test_boolean_operations(self):
        expr = Expression.from_str('($flag && ($count > 0)) || false')
        self.assertTrue(expr.eval({"$flag": True, "$count": 2}, []))

    def test_regex_match(self):
        expr = Expression.from_str('"Hello" =~ /h.*/i')
        self.assertTrue(expr.eval({}, []))

    def test_list_comprehension(self):
        expr = Expression.from_str('[ $x * 2 for $x in $numbers if $x > 1 ]')
        result = expr.eval({"$numbers": [1, 2, 3, 4]}, [])
        self.assertEqual(result, [4, 6, 8])

    def test_list_comprehension_without_if(self):
        expr = Expression.from_str('[ $x for $x in $numbers ]')
        result = expr.eval({"$numbers": [1, 2, 3]}, [])
        self.assertEqual(result, [1, 2, 3])

    def test_nested_list_comprehension(self):
        expr = Expression.from_str('[ {"id": $x, "double": $x * 2} for $x in $numbers ]')
        result = expr.eval({"$numbers": [1, 2, 3]}, [])
        self.assertEqual(
            result,
            [
                {"id": 1, "double": 2},
                {"id": 2, "double": 4},
                {"id": 3, "double": 6}
            ]
        )

    def test_dict_comprehension(self):
        expr = Expression.from_str('{ "key" + $i: $i for $i in $values if $i != 2 }')
        result = expr.eval({"$values": [1, 2, 3]}, [])
        self.assertEqual(result, {"key1": 1, "key3": 3})

    def test_dict_comprehension_without_if(self):
        expr = Expression.from_str('{ "key" + $i: $i for $i in $values }')
        result = expr.eval({"$values": [1, 2]}, [])
        self.assertEqual(result, {"key1": 1, "key2": 2})

    def test_dict_comprehension_merge_complex_conditions(self):
        expr_str = r'''{ $_k: true for $_k in $Message if $Message[$_k] != 0 } + { $_k: true for $_k in $MessageTable if $MessageTable[$_k]["lv"] <= $Lv && (!$MessageTable[$_k]["if"] || $Flag[$MessageTable[$_k]["if"]]) && (!$MessageTable[$_k]["ifnot"] || !$Flag[$MessageTable[$_k]["ifnot"]]) }'''
        expr = Expression.from_str(expr_str)

        base_table = {
            "msg1": {"lv": 1, "if": "", "ifnot": "", "extra": True},
            "msg2": {"lv": 3, "if": "flag_ok", "ifnot": "flag_ng", "extra": False},
            "msg3": {"lv": 6, "if": "", "ifnot": "", "extra": True},
            "msg4": {"lv": 2, "if": "flag_late", "ifnot": "", "extra": True},
            "msg5": {"lv": 5, "if": "", "ifnot": "flag_disable", "extra": True},
            "msg6": {"lv": 2.1, "if": "", "ifnot": "", "extra": False},
        }

        cases = [
            {
                "name": "basic_union",
                "env": {
                    "$message": {"msg1": 1, "msg2": 0, "msg3": 2},
                    "$messagetable": {k: base_table[k] for k in ("msg1", "msg2", "msg3")},
                    "$flag": {"flag_ok": True, "flag_ng": False},
                    "$lv": 3,
                },
                "expected": {"msg1": True, "msg3": True, "msg2": True},
            },
            {
                "name": "all_messages_zero",
                "env": {
                    "$message": {"msg1": 0, "msg2": 0},
                    "$messagetable": {k: base_table[k] for k in ("msg1", "msg2")},
                    "$flag": {"flag_ok": True, "flag_ng": False},
                    "$lv": 5,
                },
                "expected": {"msg1": True, "msg2": True},
            },
            {
                "name": "lv_filter_blocks_high_level",
                "env": {
                    "$message": {"msg3": 3},
                    "$messagetable": {"msg3": base_table["msg3"]},
                    "$flag": {},
                    "$lv": 3,
                },
                "expected": {"msg3": True},
            },
            {
                "name": "flag_required_true",
                "env": {
                    "$message": {"msg2": 0},
                    "$messagetable": {"msg2": base_table["msg2"]},
                    "$flag": {"flag_ok": True, "flag_ng": False},
                    "$lv": 5,
                },
                "expected": {"msg2": True},
            },
            {
                "name": "flag_required_false",
                "env": {
                    "$message": {"msg2": 0},
                    "$messagetable": {"msg2": base_table["msg2"]},
                    "$flag": {"flag_ok": False, "flag_ng": False},
                    "$lv": 5,
                },
                "expected": {},
            },
            {
                "name": "flag_ifnot_blocks",
                "env": {
                    "$message": {"msg5": 0},
                    "$messagetable": {"msg5": base_table["msg5"]},
                    "$flag": {"flag_disable": True},
                    "$lv": 6,
                },
                "expected": {},
            },
            {
                "name": "flag_ifnot_allows",
                "env": {
                    "$message": {"msg5": 0},
                    "$messagetable": {"msg5": base_table["msg5"]},
                    "$flag": {"flag_disable": False},
                    "$lv": 6,
                },
                "expected": {"msg5": True},
            },
            {
                "name": "message_override_table",
                "env": {
                    "$message": {"msg1": 0, "msg4": 1},
                    "$messagetable": {k: base_table[k] for k in ("msg1", "msg4")},
                    "$flag": {"flag_late": True},
                    "$lv": 10,
                },
                "expected": {"msg1": True, "msg4": True},
            },
            {
                "name": "message_zero_table_fail",
                "env": {
                    "$message": {"msg4": 0},
                    "$messagetable": {"msg4": base_table["msg4"]},
                    "$flag": {"flag_late": False},
                    "$lv": 10,
                },
                "expected": {},
            },
            {
                "name": "decimal_string_vs_float",
                "env": {
                    "$message": {},
                    "$messagetable": {"msg1": {"lv": "2.1", "if": "", "ifnot": ""}},
                    "$flag": {},
                    "$lv": 2.1,
                },
                "expected": {"msg1": True},
            },
            {
                "name": "decimal_string_vs_string",
                "env": {
                    "$message": {},
                    "$messagetable": {"msg1": {"lv": "2.1", "if": "", "ifnot": ""}},
                    "$flag": {},
                    "$lv": "03.0",
                },
                "expected": {},
            },
            {
                "name": "mixed_large_lv",
                "env": {
                    "$message": {"msg1": 0, "msg2": 0, "msg3": 0, "msg4": 0, "msg5": 0, "msg6": 0},
                    "$messagetable": base_table,
                    "$flag": {"flag_ok": False, "flag_ng": True, "flag_late": False, "flag_disable": True},
                    "$lv": 2.1,
                },
                "expected": {"msg1": True, "msg6": True},
            },
            {
                "name": "mixed_large_lv",
                "env": {
                    "$message": {"msg1": 1, "msg2": 0, "msg3": 0, "msg4": 2, "msg5": 0, "msg6": 0},
                    "$messagetable": base_table,
                    "$flag": {"flag_ok": True, "flag_ng": False, "flag_late": True, "flag_disable": False},
                    "$lv": 5,
                },
                "expected": {"msg1": True, "msg2": True, "msg4": True, "msg5": True, "msg6": True},
            },
        ]

        for case in cases:
            with self.subTest(case["name"]):
                result = expr.eval(case["env"], [])
                self.assertEqual(result, case["expected"])

    def test_conditional_expression(self):
        expr = Expression.from_str('$value > 0 ? "positive" : "non-positive"')
        self.assertEqual(expr.eval({"$value": 5}, []), "positive")
        self.assertEqual(expr.eval({"$value": 0}, []), "non-positive")

    def test_null_and_boolean_literals(self):
        expr = Expression.from_str('null == null && true != false')
        self.assertTrue(expr.eval({}, []))

    def test_numeric_string_comparison(self):
        expr = Expression.from_str('$a <= $b')
        cases = [
            {"env": {"$a": 2.1, "$b": 2.1}, "expected": True},
            {"env": {"$a": 2.1, "$b": 2.11}, "expected": True},
            {"env": {"$a": 2.11, "$b": 2.1}, "expected": False},
            {"env": {"$a": "2.1", "$b": 2.1}, "expected": True},
            {"env": {"$a": 2.1, "$b": "2.1"}, "expected": True},
            {"env": {"$a": "2.10", "$b": "2.1"}, "expected": False},
            {"env": {"$a": "2.1", "$b": "02.2"}, "expected": False},
        ]
        for case in cases:
            with self.subTest(case=case):
                result = expr.eval(case["env"], [])
                self.assertEqual(bool(result), case["expected"])

    def test_jsonish_nested_structure(self):
        expr = Expression.from_str('{"outer": [{"name": "base"}, {"name": "user_" + $id}], "flag": true}')
        result = expr.eval({"$id": "001"}, [])
        self.assertEqual(
            result,
            {
                "outer": [{"name": "base"}, {"name": "user_001"}],
                "flag": True
            }
        )

    def test_jsonish_expression_key(self):
        expr = Expression.from_str('{"key_" + $idx: {"value": $idx}}')
        result = expr.eval({"$idx": 5}, [])
        self.assertEqual(result, {"key_5": {"value": 5}})

    def test_jsonish_list_of_values(self):
        expr = Expression.from_str('[{"id": 1}, {"id": 2}, {"id": $dynamic}]')
        result = expr.eval({"$dynamic": 3}, [])
        self.assertEqual(result, [{"id": 1}, {"id": 2}, {"id": 3}])

    def test_list_comprehension_with_nested_access(self):
        expr_str = r'''[ { "id": $_k, "sort": $MessageTable[$_k]["sort"], "read": $MessageTable[$_k]["read"]  || ($Message[$_k] == 2), "from": $MessageTable[$_k]["from"], "title": $MessageTable[$_k]["title"], "content": $MessageTable[$_k]["content"], "onopen": $MessageTable[$_k]["onopen"] } for $_k in $_messageKeys ]'''
        expr = Expression.from_str(expr_str)

        base_table = {
            "msg1": {"sort": 10, "read": False, "from": "A", "title": "T1", "content": "C1", "onopen": "open1"},
            "msg2": {"sort": 5, "read": False, "from": "B", "title": "T2", "content": "C2", "onopen": "open2"},
            "msg3": {"sort": 20, "read": True, "from": "C", "title": "T3", "content": "C3", "onopen": "open3"},
            "msg4": {"sort": 15, "read": False, "from": "D", "title": "T4", "content": "C4", "onopen": "open4"},
            "msg5": {"sort": 8, "read": True, "from": "E", "title": "T5", "content": "C5", "onopen": "open5"},
            "msg6": {"sort": 1, "read": False, "from": "F", "title": "T6", "content": "C6", "onopen": "open6"},
        }

        cases = [
            {
                "name": "mixed_messages",
                "env": {
                    "$message": {"msg1": 1, "msg2": 2, "msg3": 0},
                    "$messagetable": {k: base_table[k] for k in ("msg1", "msg2", "msg3")},
                    "$_messagekeys": ["msg1", "msg2", "msg3"],
                },
                "expected": [
                    {**base_table["msg1"], "id": "msg1", "read": False},
                    {**base_table["msg2"], "id": "msg2", "read": True},
                    {**base_table["msg3"], "id": "msg3", "read": True},
                ],
            },
            {
                "name": "all_unread_messages_zero",
                "env": {
                    "$message": {"msg1": 0, "msg2": 0},
                    "$messagetable": {k: base_table[k] for k in ("msg1", "msg2")},
                    "$_messagekeys": ["msg1", "msg2"],
                },
                "expected": [
                    {**base_table["msg1"], "id": "msg1", "read": False},
                    {**base_table["msg2"], "id": "msg2", "read": False},
                ],
            },
            {
                "name": "message_forces_read_true",
                "env": {
                    "$message": {"msg4": 2},
                    "$messagetable": {"msg4": base_table["msg4"]},
                    "$_messagekeys": ["msg4"],
                },
                "expected": [
                    {**base_table["msg4"], "id": "msg4", "read": True},
                ],
            },
            {
                "name": "table_already_read",
                "env": {
                    "$message": {"msg3": 1},
                    "$messagetable": {"msg3": base_table["msg3"]},
                    "$_messagekeys": ["msg3"],
                },
                "expected": [
                    {**base_table["msg3"], "id": "msg3", "read": True},
                ],
            },
            {
                "name": "missing_message_entry",
                "env": {
                    "$message": {},
                    "$messagetable": {"msg1": base_table["msg1"]},
                    "$_messagekeys": ["msg1"],
                },
                "expected": [
                    {**base_table["msg1"], "id": "msg1", "read": False},
                ],
            },
            {
                "name": "subset_keys",
                "env": {
                    "$message": {"msg1": 2, "msg2": 1, "msg3": 2},
                    "$messagetable": {k: base_table[k] for k in ("msg1", "msg2", "msg3")},
                    "$_messagekeys": ["msg2"],
                },
                "expected": [
                    {**base_table["msg2"], "id": "msg2", "read": False},
                ],
            },
            {
                "name": "empty_keys",
                "env": {
                    "$message": {"msg1": 2},
                    "$messagetable": {"msg1": base_table["msg1"]},
                    "$_messagekeys": [],
                },
                "expected": [],
            },
            {
                "name": "unordered_keys",
                "env": {
                    "$message": {"msg1": 1, "msg2": 2, "msg4": 0},
                    "$messagetable": {k: base_table[k] for k in ("msg2", "msg4", "msg1")},
                    "$_messagekeys": ["msg4", "msg2", "msg1"],
                },
                "expected": [
                    {**base_table["msg4"], "id": "msg4", "read": False},
                    {**base_table["msg2"], "id": "msg2", "read": True},
                    {**base_table["msg1"], "id": "msg1", "read": False},
                ],
            },
            {
                "name": "duplicate_keys",
                "env": {
                    "$message": {"msg1": 2},
                    "$messagetable": {"msg1": base_table["msg1"]},
                    "$_messagekeys": ["msg1", "msg1"],
                },
                "expected": [
                    {**base_table["msg1"], "id": "msg1", "read": True},
                    {**base_table["msg1"], "id": "msg1", "read": True},
                ],
            },
            {
                "name": "long_list",
                "env": {
                    "$message": {"msg1": 2, "msg2": 2, "msg3": 0, "msg4": 1, "msg5": 0},
                    "$messagetable": {k: base_table[k] for k in ("msg1", "msg2", "msg3", "msg4", "msg5")},
                    "$_messagekeys": ["msg1", "msg2", "msg3", "msg4", "msg5"],
                },
                "expected": [
                    {**base_table["msg1"], "id": "msg1", "read": True},
                    {**base_table["msg2"], "id": "msg2", "read": True},
                    {**base_table["msg3"], "id": "msg3", "read": True},
                    {**base_table["msg4"], "id": "msg4", "read": False},
                    {**base_table["msg5"], "id": "msg5", "read": True},
                ],
            },
            {
                "name": "additional_entry_missing_in_table",
                "env": {
                    "$message": {"msg6": 2},
                    "$messagetable": {"msg6": {"sort": None, "read": False, "from": None, "title": None, "content": None, "onopen": None}},
                    "$_messagekeys": ["msg6"],
                },
                "expected": [
                    {"id": "msg6", "sort": None, "read": True, "from": None, "title": None, "content": None, "onopen": None},
                ],
            },
        ]

        for case in cases:
            with self.subTest(case["name"]):
                env = case["env"]
                result = expr.eval(env, [])
                self.assertEqual(result, case["expected"])

class ExpressionVersionCompatibilityTest(unittest.TestCase):
    def tearDown(self):
        set_version(3)

    def test_versionごとの除算(self):
        expr = Expression.from_str('5 / 2')

        for version in (1, 2):
            with self.subTest(version=version):
                set_version(version)
                self.assertEqual(expr.eval({}, []), 2)

        set_version(3)
        self.assertEqual(expr.eval({}, []), 2.5)

    def test_versionごとの真偽値とnull(self):
        missing_expr = Expression.from_str('$missing')

        for version in (1, 2):
            with self.subTest(version=version):
                set_version(version)
                true_expr = Expression.from_str('true')
                self.assertIs(type(true_expr.eval({}, [])), int)
                self.assertEqual(true_expr.eval({}, []), 1)
                self.assertEqual(missing_expr.eval({}, []), 0)

        set_version(3)
        true_expr = Expression.from_str('true')
        self.assertIs(true_expr.eval({}, []), True)
        self.assertIsNone(missing_expr.eval({}, []))

    def test_v3三項演算子は選択しない枝も評価する(self):
        set_version(3)
        expr = Expression.from_str('true ? 1 : (1 / 0)')

        with self.assertLogs(level='ERROR'):
            result = expr.eval({}, [])
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
