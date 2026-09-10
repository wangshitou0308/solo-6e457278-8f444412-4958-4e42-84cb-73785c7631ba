"""jsonio 测试：与标准库 json 行为对齐 + 深嵌套不触发递归限制。"""

from __future__ import annotations

import json as stdjson
import unittest

import tests.support  # noqa: F401
from mailrecon import jsonio


class DumpsEquivalenceTest(unittest.TestCase):
    CASES = [
        {},
        [],
        {"a": 1},
        [1, 2, 3],
        {"a": [1, {"b": 2}], "c": "d"},
        {"zh": "中文测试", "esc": 'q"\\\n\t\r\b\f'},
        [True, False, None, -3, 0, 2.5e3],
        {"emoji": "😀"},
        [{"x": [1, 2]}, {"y": []}, {}],
        {"a": {"b": {"c": [1, [2, 3]]}}},
        [],
        {"": ""},
    ]

    def test_compact_ascii(self):
        for v in self.CASES:
            self.assertEqual(
                jsonio.dumps(v),
                stdjson.dumps(v),
                msg=f"value={v!r}",
            )

    def test_compact_unicode(self):
        for v in self.CASES:
            self.assertEqual(
                jsonio.dumps(v, ensure_ascii=False),
                stdjson.dumps(v, ensure_ascii=False),
                msg=f"value={v!r}",
            )

    def test_indent(self):
        for indent in (2, 4):
            for v in self.CASES:
                self.assertEqual(
                    jsonio.dumps(v, ensure_ascii=False, indent=indent),
                    stdjson.dumps(v, ensure_ascii=False, indent=indent),
                    msg=f"indent={indent} value={v!r}",
                )

    def test_unsupported_type(self):
        with self.assertRaises(TypeError):
            jsonio.dumps({"x": object()})


class LoadsEquivalenceTest(unittest.TestCase):
    SAMPLES = [
        '{"a":true,"b":false,"c":null}',
        "[1, 2, 3]",
        '{"k":"a\\u0041b\\n\\t\\""}',
        '{"zh": [ -1.5e2, 0, -0 ] }',
        '"é"',
        '{"surrogate":"😀"}',
        "[]",
        "{}",
        "  { \"a\" : [ 1 ] }  ",
        '{"esc":"\\b\\f\\r\\/\\\\"}',
    ]

    def test_valid_inputs(self):
        for s in self.SAMPLES:
            self.assertEqual(
                jsonio.loads(s), stdjson.loads(s), msg=f"input={s!r}"
            )

    def test_bytes_input(self):
        self.assertEqual(jsonio.loads(b'{"a":1}'), {"a": 1})

    def test_roundtrip(self):
        for v in DumpsEquivalenceTest.CASES:
            self.assertEqual(
                jsonio.loads(jsonio.dumps(v)), v, msg=f"value={v!r}"
            )

    def test_malformed_rejected(self):
        bad = [
            "", "{", "[", "[1,]", '{"a":}', "tru", '{"a" 1}',
            '{"a":1}x', "01", "[1 2]", "[,1]", "[1,,2]",
            '{"a":', '"unterminated', "[1,", '{"a":1', '"\\u00"',
            '{"a":tru}', "{,}", '{"a":1,}', '"x"x', "1.", "1e", "-",
        ]
        for s in bad:
            with self.assertRaises(
                jsonio.JsonDecodeError, msg=f"应拒绝 {s!r}"
            ):
                jsonio.loads(s)


class DeepNestingTest(unittest.TestCase):
    """超过默认递归限制（1000）的深嵌套必须可序列化、可解析。"""

    def _deep(self, depth: int):
        leaf = {"v": depth, "children": []}
        root = {"v": 0, "children": [leaf]}
        node = leaf
        for i in range(depth - 1, 0, -1):
            nxt = {"v": i, "children": []}
            node["children"] = [nxt]
            node = nxt
        return root

    def test_dumps_deep(self):
        for depth in (1100, 3000):
            root = self._deep(depth)
            # 不应抛 RecursionError
            text = jsonio.dumps(root, ensure_ascii=False, indent=2)
            self.assertIn("\"children\"", text)

    def test_loads_deep(self):
        depth = 1100
        text = jsonio.dumps(self._deep(depth))
        obj = jsonio.loads(text)
        current = obj
        count = 0
        while current["children"]:
            current = current["children"][0]
            count += 1
        self.assertEqual(count, depth)
        self.assertEqual(current["v"], 1)

    def test_standard_json_would_recurse(self):
        """防御性断言：标准库在该深度确实会 RecursionError，证明测试有效。"""
        root = self._deep(1100)
        with self.assertRaises(RecursionError):
            stdjson.dumps(root, indent=2)
        text = jsonio.dumps(root)
        with self.assertRaises(RecursionError):
            stdjson.loads(text)


if __name__ == "__main__":
    unittest.main()
