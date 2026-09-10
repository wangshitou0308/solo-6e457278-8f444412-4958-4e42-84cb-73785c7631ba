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

    def test_independent_of_recursion_limit(self):
        """深链读写不受 Python 递归深度限制（跨 Python 版本稳定）。

        不能写死“标准库在 1100 层必抛 RecursionError”：不同 Python
        版本（如 3.12）的 json C 实现对嵌套的容忍度不同，该假设不成立。

        改为在测试内把 ``sys.recursionlimit`` 主动降到一个确定很浅的
        值，并用一个朴素递归遍历器作为“深度确实超过递归预算”的稳定
        基准：同一份深数据上，递归遍历必然 RecursionError，而 jsonio
        的序列化与解析（显式栈、不使用 Python 递归）照常成功。
        """
        import sys

        depth = 600  # 远超下面设置的浅递归预算
        root = self._deep(depth)

        def recurse_depth(node) -> int:
            # 朴素递归：每层增加真实 Python 调用帧
            children = node["children"]
            if not children:
                return 0
            return 1 + recurse_depth(children[0])

        original_limit = sys.getrecursionlimit()
        self.addCleanup(sys.setrecursionlimit, original_limit)
        # 降到很低：相对测试方法当前调用栈只留少量预算，足以证明
        # 递归路径会被切断，而迭代实现完全不受影响。
        sys.setrecursionlimit(120)

        # 前置条件：这个深度在浅预算下，朴素递归确实失败 —— 保证测试
        # 本身确实在“超过递归深度”的条件下运行，而不是侥幸通过。
        with self.assertRaises(RecursionError):
            recurse_depth(root)

        # 核心断言：jsonio 写入与读取同一份深数据均成功
        text = jsonio.dumps(root, ensure_ascii=False, indent=2)
        obj = jsonio.loads(text)
        current = obj
        count = 0
        while current["children"]:
            current = current["children"][0]
            count += 1
        self.assertEqual(count, depth)
        self.assertEqual(current["v"], 1)

        # 同样在浅递归预算下，jsonio 紧凑往返也成功
        again = jsonio.loads(jsonio.dumps(obj, ensure_ascii=False))
        current = again
        count = 0
        while current["children"]:
            current = current["children"][0]
            count += 1
        self.assertEqual(count, depth)


if __name__ == "__main__":
    unittest.main()
