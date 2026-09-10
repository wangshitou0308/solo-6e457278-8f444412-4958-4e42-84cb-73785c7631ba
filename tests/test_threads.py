"""threads 测试：正常链、缺父、成环、自引用、重复 ID、缺 ID。"""

from __future__ import annotations

import unittest

import tests.support  # noqa: F401
from mailrecon import threads


def rec(
    source: str,
    mid: str | None = None,
    irt: str | None = None,
    refs: list[str] | None = None,
    body: str = "",
    date: str | None = None,
    raw_sha: str | None = None,
):
    return {
        "source_file": source,
        "raw_sha256": raw_sha or f"sha-{source}",
        "message_id": mid,
        "in_reply_to": irt,
        "references": refs or [],
        "date": date or "2026-09-01T00:00:00+00:00",
        "from": {"name": "a", "address": "a@x"},
        "to": [],
        "cc": [],
        "subject": source,
        "body_text": body,
        "body_html_present": False,
        "attachments": [],
        "issues": [],
    }


def index(tree):
    return {n["uid"]: n for n in tree["messages"]}


def parent_map(tree):
    parents = {}
    for node in tree["messages"]:
        for child in node["children"]:
            parents[child["uid"]] = node["uid"]
    return parents


class ThreadsTest(unittest.TestCase):
    def test_simple_chain(self):
        records = [
            rec("a", "<a@x>"),
            rec("b", "<b@x>", irt="<a@x>", refs=["<a@x>"]),
            rec("c", "<c@x>", irt="<b@x>", refs=["<a@x>", "<b@x>"]),
        ]
        tree = threads.build_threads(records)
        self.assertEqual(len(tree["roots"]), 1)
        root = tree["roots"][0]
        self.assertEqual(root["source_file"], "a")
        self.assertEqual([c["source_file"] for c in root["children"]], ["b"])
        self.assertEqual(
            [c["source_file"] for c in root["children"][0]["children"]], ["c"]
        )

    def test_walks_references_when_direct_parent_missing(self):
        # b 缺失，c 引用 [a, b]：c 应挂到 a 下，而不是成为根
        records = [
            rec("a", "<a@x>"),
            rec("c", "<c@x>", irt="<b@x>", refs=["<a@x>", "<b@x>"]),
        ]
        tree = threads.build_threads(records)
        self.assertEqual(len(tree["roots"]), 1)
        self.assertEqual(tree["roots"][0]["source_file"], "a")
        self.assertEqual(
            [c["source_file"] for c in tree["roots"][0]["children"]], ["c"]
        )
        c_node = tree["messages"][1]
        self.assertTrue(any("缺失" in i for i in c_node["issues"]))

    def test_all_parents_missing_becomes_root(self):
        records = [rec("c", "<c@x>", irt="<ghost@x>", refs=["<ghost@x>"])]
        tree = threads.build_threads(records)
        self.assertEqual(len(tree["roots"]), 1)
        self.assertEqual(tree["roots"][0]["source_file"], "c")
        self.assertGreaterEqual(
            tree["issues_summary"]["missing_parent"], 1
        )

    def test_reference_cycle_is_broken_and_marked(self):
        records = [
            rec("a", "<a@x>", irt="<b@x>", refs=["<b@x>"], date="2026-09-01T01:00:00+00:00"),
            rec("b", "<b@x>", irt="<a@x>", refs=["<a@x>"], date="2026-09-01T02:00:00+00:00"),
        ]
        tree = threads.build_threads(records)
        # 成环必须被打破：只有一个根，且总体仍是森林（无重复挂载）
        self.assertEqual(len(tree["roots"]), 1)
        parents = parent_map(tree)
        uids = {n["source_file"]: n["uid"] for n in tree["messages"]}
        self.assertEqual(parents[uids["a"]], uids["b"])
        self.assertNotIn(uids["b"], parents)  # 闭环节点是根
        self.assertGreaterEqual(tree["issues_summary"]["reference_cycle"], 1)
        self.assertTrue(
            any("成环" in i for n in tree["messages"] for i in n["issues"])
        )

    def test_self_reference_broken(self):
        records = [rec("a", "<a@x>", irt="<a@x>", refs=["<a@x>"])]
        tree = threads.build_threads(records)
        self.assertEqual(len(tree["roots"]), 1)
        self.assertGreaterEqual(tree["issues_summary"]["self_reference"], 1)

    def test_duplicate_message_id_different_content(self):
        records = [
            rec("v1", "<dup@x>", raw_sha="sha-1"),
            rec("v2", "<dup@x>", raw_sha="sha-2"),
        ]
        tree = threads.build_threads(records)
        self.assertEqual(len(tree["roots"]), 2)
        self.assertGreaterEqual(
            tree["issues_summary"]["duplicate_message_id"], 1
        )
        v2 = next(n for n in tree["messages"] if n["source_file"] == "v2")
        self.assertTrue(any("重复" in i for i in v2["issues"]))

    def test_duplicate_message_id_identical_content_marked_copy(self):
        records = [
            rec("v1", "<dup@x>", raw_sha="same"),
            rec("v2", "<dup@x>", raw_sha="same"),
        ]
        tree = threads.build_threads(records)
        self.assertEqual(len(tree["roots"]), 2)
        v2 = next(n for n in tree["messages"] if n["source_file"] == "v2")
        self.assertTrue(any("重复副本" in i for i in v2["issues"]))

    def test_duplicate_id_node_is_not_attached_as_child(self):
        # v2 声称回复 a，但因 ID 冲突必须保持独立根
        records = [
            rec("a", "<a@x>"),
            rec("v1", "<dup@x>", irt="<a@x>", refs=["<a@x>"], raw_sha="sha-1"),
            rec("v2", "<dup@x>", irt="<a@x>", refs=["<a@x>"], raw_sha="sha-2"),
        ]
        tree = threads.build_threads(records)
        sources = {n["source_file"] for n in tree["roots"]}
        self.assertIn("v2", sources)
        self.assertEqual(len(tree["roots"]), 2)  # a->v1 一条链 + v2

    def test_missing_message_id_is_root(self):
        records = [rec("x", mid=None)]
        tree = threads.build_threads(records)
        self.assertEqual(len(tree["roots"]), 1)
        self.assertGreaterEqual(
            tree["issues_summary"]["missing_message_id"], 1
        )

    def test_result_nodes_carry_payload_fields(self):
        records = [
            rec(
                "a", "<a@x>",
                body="你好\n正文",
            )
        ]
        tree = threads.build_threads(records)
        node = tree["roots"][0]
        self.assertEqual(node["body_text"], "你好\n正文")
        self.assertEqual(node["message_id"], "<a@x>")
        self.assertEqual(node["attachments"], [])

    def test_tree_is_acyclic_under_complex_graph(self):
        # 构造 diamond + 环混合，验证输出严格为树
        records = [
            rec("a", "<a@x>"),
            rec("b", "<b@x>", irt="<a@x>", refs=["<a@x>"]),
            rec("c", "<c@x>", irt="<a@x>", refs=["<a@x>"]),
            rec("d", "<d@x>", irt="<b@x>", refs=["<a@x>", "<b@x>", "<c@x>"]),
            rec("e", "<e@x>", irt="<d@x>", refs=["<d@x>", "<e@x>"]),  # 环引用
        ]
        tree = threads.build_threads(records)
        seen = set()

        def walk(node, stack):
            self.assertNotIn(node["uid"], stack, "输出树中出现环")
            self.assertNotIn(node["uid"], seen, "节点被挂载了多次")
            seen.add(node["uid"])
            for child in node["children"]:
                walk(child, stack | {node["uid"]})

        for root in tree["roots"]:
            walk(root, set())
        self.assertEqual(len(seen), 5)

    def test_deep_chain_beyond_recursion_limit(self):
        """1200 封邮件单链（> 默认递归深度 1000），必须建成一条直链。"""
        depth = 1200
        mids = [f"<m{i:05d}@x>" for i in range(depth)]
        records = [rec("00000.eml", mids[0])]
        for i in range(1, depth):
            records.append(
                rec(
                    f"{i:05d}.eml",
                    mids[i],
                    irt=mids[i - 1],
                    refs=[mids[i - 1]],
                )
            )
        tree = threads.build_threads(records)

        # 只有一个根，且沿 children 一直走到底，深度 = depth
        self.assertEqual(len(tree["roots"]), 1)
        current = tree["roots"][0]
        seen = 0
        while True:
            seen += 1
            if current["children"]:
                self.assertEqual(len(current["children"]), 1)
                current = current["children"][0]
            else:
                break
        self.assertEqual(seen, depth)
        self.assertEqual(current["message_id"], mids[-1])
        # 没有任何异常标注
        self.assertEqual(tree["issues_summary"]["reference_cycle"], 0)
        self.assertEqual(tree["issues_summary"]["missing_parent"], 0)

    def test_deep_chain_shuffled_input_order(self):
        """乱序输入的深链同样能还原成一条链。"""
        depth = 1200
        mids = [f"<s{i:05d}@x>" for i in range(depth)]
        records = [
            rec(
                f"{i:05d}.eml",
                mids[i],
                irt=mids[i - 1] if i else None,
                refs=[mids[i - 1]] if i else [],
            )
            for i in range(depth)
        ]
        # 逆序输入（子在父之前），这正是递归解析会一路下探的情形
        records = list(reversed(records))
        tree = threads.build_threads(records)
        self.assertEqual(len(tree["roots"]), 1)
        current = tree["roots"][0]
        seen = 0
        while current["children"]:
            self.assertEqual(len(current["children"]), 1)
            current = current["children"][0]
            seen += 1
        self.assertEqual(seen, depth - 1)
        self.assertEqual(tree["roots"][0]["message_id"], mids[0])


if __name__ == "__main__":
    unittest.main()
