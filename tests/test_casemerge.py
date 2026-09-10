"""casemerge 测试：跨包去重、ID 冲突、歧义不猜测、补链、成环、贡献统计。"""

from __future__ import annotations

import unittest

import tests.support  # noqa: F401
from mailrecon import casemerge


def node(
    uid: int,
    source: str,
    mid: str | None = None,
    irt: str | None = None,
    refs: list[str] | None = None,
    sha: str | None = None,
    date: str = "2026-09-01T09:00:00+00:00",
    issues: list[str] | None = None,
):
    return {
        "uid": uid,
        "source_file": source,
        "raw_sha256": sha or f"sha-{source}",
        "message_id": mid,
        "in_reply_to": irt,
        "references": refs or [],
        "date": date,
        "from": {"name": "a", "address": "a@x"},
        "to": [],
        "cc": [],
        "subject": source,
        "body_text": "",
        "body_html_present": False,
        "attachments": [],
        "issues": issues or [],
        "children": [],
    }


def job(job_id: str, nodes: list[dict], filename: str | None = None):
    """把节点直接作为森林根列表（摊平后按 uid 排序，形状不影响结果）。"""
    return {
        "job_id": job_id,
        "original_filename": filename or f"{job_id}.zip",
        "threads": nodes,
    }


def parent_map(roots: list[dict]) -> dict[int, int]:
    """迭代式从森林推出 child_uid -> parent_uid。"""
    parents: dict[int, int] = {}
    stack = list(roots)
    while stack:
        current = stack.pop()
        for child in current["children"]:
            parents[child["uid"]] = current["uid"]
            stack.append(child)
    return parents


def contrib(stats: dict, job_id: str) -> dict:
    return next(
        c for c in stats["job_contributions"] if c["job_id"] == job_id
    )


class CaseMergeTest(unittest.TestCase):
    def test_identical_email_merges_into_one_node(self):
        j1 = job("j1", [node(0, "a.eml", "<a@x>", sha="S")])
        j2 = job("j2", [node(0, "copy.eml", "<a@x>", sha="S")])
        merged = casemerge.build_case_forest([j1, j2])

        self.assertEqual(merged["stats"]["merged_nodes"], 1)
        self.assertEqual(merged["stats"]["duplicates_merged"], 1)
        root = merged["roots"][0]
        self.assertEqual(
            [(s["job_id"], s["source_file"]) for s in root["sources"]],
            [("j1", "a.eml"), ("j2", "copy.eml")],
        )
        info = root["merge_info"]
        self.assertEqual(info["source_count"], 2)
        self.assertEqual(info["duplicates_merged"], 1)
        self.assertFalse(info["conflict"])
        self.assertTrue(any("合并" in n for n in info["notes"]))
        # 各作业贡献
        stats = merged["stats"]
        self.assertEqual(contrib(stats, "j1")["unique_nodes"], 1)
        self.assertEqual(contrib(stats, "j1")["duplicates_merged"], 0)
        self.assertEqual(contrib(stats, "j2")["unique_nodes"], 0)
        self.assertEqual(contrib(stats, "j2")["duplicates_merged"], 1)

    def test_cross_package_parent_relinks_missing(self):
        j1 = job(
            "j1",
            [
                node(
                    0, "reply.eml", "<b@x>", irt="<a@x>", refs=["<a@x>"],
                    issues=["引用的父邮件 <a@x> 在压缩包内缺失"],
                )
            ],
        )
        j2 = job("j2", [node(0, "root.eml", "<a@x>")])
        merged = casemerge.build_case_forest([j1, j2])

        self.assertEqual(len(merged["roots"]), 1)
        root = merged["roots"][0]
        self.assertEqual(root["message_id"], "<a@x>")
        child = root["children"][0]
        self.assertEqual(child["message_id"], "<b@x>")

        relink = child["merge_info"]["relinked_parent"]
        self.assertIsNotNone(relink)
        self.assertEqual(relink["message_id"], "<a@x>")
        self.assertEqual(relink["via"], "in_reply_to")
        self.assertEqual(relink["resolved_for_jobs"], ["j1"])
        self.assertEqual(merged["stats"]["relinked_nodes"], 1)
        self.assertEqual(contrib(merged["stats"], "j1")["relinked_nodes"], 1)
        self.assertEqual(contrib(merged["stats"], "j2")["relinked_nodes"], 0)

    def test_no_relink_when_parent_in_same_job(self):
        j1 = job(
            "j1",
            [
                node(0, "a.eml", "<a@x>"),
                node(1, "b.eml", "<b@x>", irt="<a@x>", refs=["<a@x>"]),
            ],
        )
        merged = casemerge.build_case_forest([j1])
        child = merged["roots"][0]["children"][0]
        self.assertIsNone(child["merge_info"]["relinked_parent"])
        self.assertEqual(merged["stats"]["relinked_nodes"], 0)

    def test_relink_via_references_when_in_reply_to_absent(self):
        j1 = job(
            "j1",
            [node(0, "c.eml", "<c@x>", refs=["<a@x>", "<b@x>"])],
        )
        j2 = job("j2", [node(0, "b.eml", "<b@x>")])
        merged = casemerge.build_case_forest([j1, j2])
        parents = parent_map(merged["roots"])
        nodes = {n["uid"]: n for n in merged["nodes"]}
        c_uid = next(u for u, n in nodes.items() if n["message_id"] == "<c@x>")
        b_uid = next(u for u, n in nodes.items() if n["message_id"] == "<b@x>")
        self.assertEqual(parents[c_uid], b_uid)
        relink = nodes[c_uid]["merge_info"]["relinked_parent"]
        self.assertEqual(relink["via"], "references")
        self.assertEqual(relink["message_id"], "<b@x>")

    def test_conflict_same_mid_different_sha_kept_separate(self):
        j1 = job("j1", [node(0, "v1.eml", "<x@y>", sha="S1")])
        j2 = job("j2", [node(0, "v2.eml", "<x@y>", sha="S2")])
        merged = casemerge.build_case_forest([j1, j2])

        stats = merged["stats"]
        self.assertEqual(stats["merged_nodes"], 2)
        self.assertEqual(stats["conflict_groups"], 1)
        self.assertEqual(stats["conflict_nodes"], 2)
        self.assertEqual(len(merged["roots"]), 2)
        by_sha = {n["raw_sha256"]: n for n in merged["nodes"]}
        n1, n2 = by_sha["S1"], by_sha["S2"]
        self.assertTrue(n1["merge_info"]["conflict"])
        self.assertTrue(n2["merge_info"]["conflict"])
        self.assertEqual(n1["merge_info"]["conflict_with"], [n2["uid"]])
        self.assertEqual(n2["merge_info"]["conflict_with"], [n1["uid"]])
        self.assertTrue(
            any("并列保留" in note for note in n1["merge_info"]["notes"])
        )
        # 冲突双方各自计一次
        self.assertEqual(contrib(stats, "j1")["conflict_nodes"], 1)
        self.assertEqual(contrib(stats, "j2")["conflict_nodes"], 1)

    def test_ambiguous_reference_never_guesses_parent(self):
        j1 = job("j1", [node(0, "v1.eml", "<x@y>", sha="S1")])
        j2 = job(
            "j2",
            [
                node(0, "v2.eml", "<x@y>", sha="S2"),
                node(1, "child.eml", "<c@x>", irt="<x@y>", refs=["<x@y>"]),
            ],
        )
        merged = casemerge.build_case_forest([j1, j2])

        # 子邮件不得挂到任何冲突版本下：三个节点都是根
        self.assertEqual(len(merged["roots"]), 3)
        child = next(
            n for n in merged["nodes"] if n["message_id"] == "<c@x>"
        )
        self.assertIsNone(child["merge_info"]["relinked_parent"])
        self.assertTrue(
            any("不会猜测" in n or "为避免猜测" in n
                for n in child["merge_info"]["notes"])
        )
        self.assertEqual(merged["stats"]["ambiguous_references"], 1)

    def test_ambiguous_reference_falls_back_to_unambiguous_ancestor(self):
        j1 = job(
            "j1",
            [
                node(0, "g.eml", "<g@x>"),
                node(1, "v1.eml", "<x@y>", sha="S1"),
            ],
        )
        j2 = job(
            "j2",
            [
                node(0, "v2.eml", "<x@y>", sha="S2"),
                node(
                    1, "child.eml", "<c@x>",
                    irt="<x@y>", refs=["<g@x>", "<x@y>"],
                ),
            ],
        )
        merged = casemerge.build_case_forest([j1, j2])

        nodes = {n["message_id"]: n for n in merged["nodes"]}
        parents = parent_map(merged["roots"])
        # <x@y> 歧义不可用，沿 References 上溯到无歧义的 <g@x>
        self.assertEqual(
            parents[nodes["<c@x>"]["uid"]], nodes["<g@x>"]["uid"]
        )
        self.assertEqual(merged["stats"]["ambiguous_references"], 1)
        # 上溯的祖先在 j1 中，对 j2 的 child 而言属于跨包补链
        relink = nodes["<c@x>"]["merge_info"]["relinked_parent"]
        self.assertEqual(relink["message_id"], "<g@x>")
        self.assertEqual(relink["resolved_for_jobs"], ["j2"])

    def test_missing_parent_everywhere_stays_root(self):
        j1 = job(
            "j1",
            [node(0, "c.eml", "<c@x>", irt="<ghost@x>", refs=["<ghost@x>"])],
        )
        merged = casemerge.build_case_forest([j1])
        self.assertEqual(len(merged["roots"]), 1)
        notes = merged["roots"][0]["merge_info"]["notes"]
        self.assertTrue(any("仍缺失" in n for n in notes))
        self.assertEqual(merged["stats"]["missing_references"], 1)

    def test_cycle_across_jobs_is_broken(self):
        j1 = job(
            "j1",
            [node(0, "a.eml", "<a@x>", irt="<b@x>", refs=["<b@x>"])],
        )
        j2 = job(
            "j2",
            [node(0, "b.eml", "<b@x>", irt="<a@x>", refs=["<a@x>"])],
        )
        merged = casemerge.build_case_forest([j1, j2])
        self.assertEqual(len(merged["roots"]), 1)
        self.assertEqual(merged["stats"]["reference_cycles"], 1)
        all_notes = [
            note
            for n in merged["nodes"]
            for note in n["merge_info"]["notes"]
        ]
        self.assertTrue(any("成环" in n for n in all_notes))
        # 森林严格无环、每个节点只出现一次
        seen: set[int] = set()
        stack = list(merged["roots"])
        while stack:
            current = stack.pop()
            self.assertNotIn(current["uid"], seen)
            seen.add(current["uid"])
            stack.extend(current["children"])
        self.assertEqual(len(seen), 2)

    def test_self_reference_broken(self):
        j1 = job(
            "j1",
            [node(0, "a.eml", "<a@x>", irt="<a@x>", refs=["<a@x>"])],
        )
        merged = casemerge.build_case_forest([j1])
        self.assertEqual(len(merged["roots"]), 1)
        self.assertEqual(merged["stats"]["self_references"], 1)

    def test_no_message_id_duplicate_merges_by_sha(self):
        j1 = job("j1", [node(0, "x.eml", None, sha="S")])
        j2 = job("j2", [node(0, "y.eml", None, sha="S")])
        merged = casemerge.build_case_forest([j1, j2])
        self.assertEqual(merged["stats"]["merged_nodes"], 1)
        root = merged["roots"][0]
        self.assertEqual(len(root["sources"]), 2)
        self.assertIsNone(root["message_id"])

    def test_forest_sorted_by_date(self):
        j1 = job(
            "j1",
            [
                node(0, "late.eml", "<l@x>", date="2026-09-03T00:00:00+00:00"),
                node(1, "early.eml", "<e@x>", date="2026-09-01T00:00:00+00:00"),
            ],
        )
        merged = casemerge.build_case_forest([j1])
        self.assertEqual(
            [r["message_id"] for r in merged["roots"]], ["<e@x>", "<l@x>"]
        )

    def test_job_contributions_summary(self):
        # j1：根 + 冲突 v1 + 仅 j1 拥有的父邮件；
        # j2：回复（父邮件只在 j1 -> 跨包补链）+ 冲突 v2 + 与 j1 重复的根
        j1 = job(
            "j1",
            [
                node(0, "root.eml", "<a@x>", sha="ROOT"),
                node(1, "v1.eml", "<x@y>", sha="S1"),
                node(2, "other.eml", "<o@x>", sha="OTHER"),
            ],
        )
        j2 = job(
            "j2",
            [
                node(0, "reply.eml", "<b@x>", irt="<o@x>", refs=["<o@x>"]),
                node(1, "v2.eml", "<x@y>", sha="S2"),
                node(2, "root-copy.eml", "<a@x>", sha="ROOT"),
            ],
        )
        merged = casemerge.build_case_forest([j1, j2])
        stats = merged["stats"]

        self.assertEqual(stats["job_count"], 2)
        self.assertEqual(stats["source_emails"], 6)
        self.assertEqual(stats["merged_nodes"], 5)
        self.assertEqual(stats["duplicates_merged"], 1)
        self.assertEqual(stats["relinked_nodes"], 1)
        self.assertEqual(stats["conflict_groups"], 1)

        c1 = contrib(stats, "j1")
        self.assertEqual(c1["emails"], 3)
        self.assertEqual(c1["unique_nodes"], 3)
        self.assertEqual(c1["duplicates_merged"], 0)
        self.assertEqual(c1["conflict_nodes"], 1)
        self.assertEqual(c1["relinked_nodes"], 0)
        # 根：root.eml（首要来源 j1）+ v1.eml = 2（other.eml 有子节点但仍是根）
        self.assertEqual(c1["root_nodes"], 3)

        c2 = contrib(stats, "j2")
        self.assertEqual(c2["emails"], 3)
        self.assertEqual(c2["unique_nodes"], 2)
        self.assertEqual(c2["duplicates_merged"], 1)
        self.assertEqual(c2["conflict_nodes"], 1)
        self.assertEqual(c2["relinked_nodes"], 1)
        self.assertEqual(c2["root_nodes"], 1)  # v2.eml

    def test_deep_chain_across_jobs(self):
        """1200 封邮件跨两个作业拼成一条链（> 默认递归深度 1000）。"""
        depth = 1200
        half = depth // 2
        mids = [f"<m{i:05d}@x>" for i in range(depth)]

        def make(i: int) -> dict:
            return node(
                i % half,
                f"{i:05d}.eml",
                mids[i],
                irt=mids[i - 1] if i else None,
                refs=[mids[i - 1]] if i else [],
                sha=f"sha-{i}",
            )

        j1 = job("j1", [make(i) for i in range(half)])
        j2 = job("j2", [make(i) for i in range(half, depth)])
        merged = casemerge.build_case_forest([j1, j2])

        self.assertEqual(len(merged["roots"]), 1)
        current = merged["roots"][0]
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
        self.assertEqual(merged["stats"]["reference_cycles"], 0)
        # 交界处的那封邮件对 j2 而言是跨包补链
        self.assertEqual(merged["stats"]["relinked_nodes"], 1)

    def test_empty_jobs_produce_empty_forest(self):
        merged = casemerge.build_case_forest(
            [job("j1", []), job("j2", [])]
        )
        self.assertEqual(merged["roots"], [])
        self.assertEqual(merged["stats"]["merged_nodes"], 0)
        self.assertEqual(merged["stats"]["thread_count"], 0)


if __name__ == "__main__":
    unittest.main()
