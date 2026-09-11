"""附件流转追踪引擎（attachflow.py）单元测试。"""

from __future__ import annotations

import hashlib
import unittest

import tests.support  # noqa: F401
from mailrecon import attachflow


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# 各测试共用的内容哈希
A = sha(b"contract-v1")
A2 = sha(b"contract-v2")
D = sha(b"data-csv")
E = sha(b"extra")
F = sha(b"invoice-f")
G = sha(b"invoice-g")
EMPTY = sha(b"")


def att(name, content_sha, size=10, original=None, undecodable=False):
    item = {
        "filename": name,
        "content_type": "application/octet-stream",
        "size": size,
        "sha256": content_sha,
    }
    if original is not None:
        item["original_filename"] = original
    if undecodable:
        item["undecodable"] = True
    return item


def mail(
    uid,
    mid=None,
    parent_uid=None,
    atts=None,
    refs=None,
    irt=None,
    date=None,
    issues=None,
    root_uid=None,
    raw=None,
):
    return {
        "uid": uid,
        "message_id": mid or f"<m{uid}@x>",
        "subject": f"s{uid}",
        "from": {"name": "", "address": "a@x"},
        "date": date or f"2026-09-{uid + 1:02d}T09:00:00+00:00",
        "raw_sha256": raw or f"raw-{uid}",
        "in_reply_to": irt,
        "references": refs or [],
        "attachments": atts or [],
        "issues": issues or [],
        "parent_uid": parent_uid,
        "thread_root_uid": root_uid if root_uid is not None else 0,
        "thread_root_message_id": "<m0@x>",
        "source": {"job_id": "j", "source_file": f"{uid}.eml"},
    }


def reply(uid, parent, mid=None, **kw):
    """构造 parent 的回复（引用头与 parent_uid 一致）。"""
    parent_mid = parent["message_id"]
    return mail(
        uid,
        mid=mid,
        parent_uid=parent["uid"],
        irt=parent_mid,
        refs=[parent_mid],
        **kw,
    )


def events_of(out):
    return [(e["type"], e["filename"], e["count"]) for e in out["events"]]


class EdgeDiffTest(unittest.TestCase):
    """父子边多重集合比较：四类事件与实例计数。"""

    def test_added_removed_renamed_content_changed(self):
        root = mail(0, atts=[att("合同.txt", A), att("数据.csv", D)])
        # 数据.csv 改名（内容不变）、补充.txt 新增
        r1 = reply(1, root, atts=[
            att("合同.txt", A), att("数据-新.csv", D), att("补充.txt", E),
        ])
        # 合同.txt 同名内容变化、补充.txt 移除
        r2 = reply(2, r1, atts=[att("合同.txt", A2), att("数据-新.csv", D)])
        out = attachflow.analyze([root, r1, r2])
        self.assertEqual(
            events_of(out),
            [
                ("renamed", "数据-新.csv", 1),
                ("added", "补充.txt", 1),
                ("content_changed", "合同.txt", 1),
                ("removed", "补充.txt", 1),
            ],
        )
        renamed = out["events"][0]
        self.assertEqual(renamed["previous_filename"], "数据.csv")
        self.assertEqual(renamed["sha256"], D)
        changed = out["events"][2]
        self.assertEqual(changed["previous_sha256"], A)
        self.assertEqual(changed["sha256"], A2)
        # 每条事件都附邮件、来源、时间、哈希依据
        for event in out["events"]:
            self.assertIn("source_file", event["email"]["source"])
            self.assertEqual(event["time"], event["email"]["date"])
            self.assertIn("basis", event)
            self.assertIn("evidence", event)
        self.assertEqual(out["reviews"], [])

    def test_no_change_produces_no_events(self):
        root = mail(0, atts=[att("a.bin", A)])
        r1 = reply(1, root, atts=[att("a.bin", A)])
        out = attachflow.analyze([root, r1])
        self.assertEqual(out["events"], [])
        self.assertEqual(out["stats"]["edges_compared"], 1)

    def test_duplicate_instances_counted_separately(self):
        # 父：report.pdf(F) 两份（展示名带 (1) 后缀）；子：一份 -> 移除 1 个实例
        root = mail(0, atts=[
            att("report.pdf", F),
            att("report(1).pdf", F, original="report.pdf"),
        ])
        r1 = reply(1, root, atts=[att("report.pdf", F)])
        out = attachflow.analyze([root, r1])
        self.assertEqual(events_of(out), [("removed", "report.pdf", 1)])
        # 反向：子变三份 -> 新增 1 个实例
        r2 = reply(2, root, atts=[
            att("report.pdf", F),
            att("report(1).pdf", F, original="report.pdf"),
            att("report(2).pdf", F, original="report.pdf"),
        ])
        out = attachflow.analyze([root, r2])
        self.assertEqual(events_of(out), [("added", "report.pdf", 1)])
        self.assertEqual(out["stats"]["event_instances_total"], 1)

    def test_same_name_reordered_produces_no_events(self):
        """同名不同内容的附件仅调换 MIME 顺序：0 条事件（回归）。"""
        root = mail(0, atts=[
            att("report.pdf", F),
            att("report(1).pdf", G, original="report.pdf"),
        ])
        # 子邮件两个附件内容不变但 MIME 顺序对调（展示名随顺序重排）
        r1 = reply(1, root, atts=[
            att("report.pdf", G),
            att("report(1).pdf", F, original="report.pdf"),
        ])
        out = attachflow.analyze([root, r1])
        self.assertEqual(out["events"], [])
        self.assertEqual(out["reviews"], [])

    def test_display_suffix_not_reported_as_rename(self):
        """展示层 (1) 后缀不触发误报改名（回归）。"""
        root = mail(0, atts=[
            att("report.pdf", F),
            att("report(1).pdf", G, original="report.pdf"),
        ])
        r1 = reply(1, root, atts=[
            att("report.pdf", F),
            att("report(1).pdf", G, original="report.pdf"),
        ])
        out = attachflow.analyze([root, r1])
        self.assertEqual(out["events"], [])

    def test_renamed_pairing_uses_sha_not_order(self):
        root = mail(0, atts=[att("旧名.txt", A)])
        r1 = reply(1, root, atts=[att("新名.txt", A)])
        out = attachflow.analyze([root, r1])
        self.assertEqual(events_of(out), [("renamed", "新名.txt", 1)])
        event = out["events"][0]
        self.assertEqual(event["previous_filename"], "旧名.txt")
        self.assertEqual(event["sha256"], A)


class ReviewTest(unittest.TestCase):
    """不推断原则：父缺失 / 引用冲突 / 无法解码只列待复核。"""

    def test_missing_parent_not_compared(self):
        orphan = mail(
            0, mid="<orphan@x>", irt="<ghost@x>", refs=["<ghost@x>"],
            atts=[att("x.bin", E)], root_uid=0,
        )
        out = attachflow.analyze([orphan])
        self.assertEqual(out["events"], [])
        self.assertEqual(len(out["reviews"]), 1)
        review = out["reviews"][0]
        self.assertEqual(review["kind"], "missing_parent")
        self.assertEqual(review["evidence"]["claimed_parent"], "<ghost@x>")
        self.assertEqual(out["emails"][0]["skip_reason"], "missing_parent")
        # 附件仍进入内容台账（全部来源）
        self.assertEqual(out["attachments"][0]["sha256"], E)

    def test_walked_up_ancestor_not_compared(self):
        """直接父缺失、挂载到上溯祖先时也不做推断。"""
        root = mail(0, mid="<grand@x>", atts=[att("g.txt", A)])
        child = mail(
            1, mid="<child@x>", parent_uid=0, irt="<gone@x>",
            refs=["<gone@x>", "<grand@x>"], atts=[att("g.txt", A)],
        )
        out = attachflow.analyze([root, child])
        self.assertEqual(out["events"], [])
        self.assertEqual(out["reviews"][0]["kind"], "missing_parent")

    def test_reference_conflict_reviews_only(self):
        """Message-ID 内容冲突：冲突方与引用方都只列待复核（回归）。"""
        canon = mail(0, mid="<conf@x>", atts=[att("a.txt", A)], raw="sha-1")
        dup = mail(1, mid="<conf@x>", atts=[att("b.txt", D)],
                   raw="sha-2", root_uid=1)
        child = reply(2, canon, mid="<child@x>", atts=[att("a.txt", A)])
        out = attachflow.analyze([canon, dup, child])
        self.assertEqual(out["events"], [])
        kinds = [r["kind"] for r in out["reviews"]]
        self.assertEqual(kinds, ["reference_conflict", "reference_conflict"])
        # 冲突 ID 写入证据（不是引用方自己的 ID）
        for review in out["reviews"]:
            self.assertEqual(review["evidence"]["message_id"], "<conf@x>")
            self.assertEqual(review["evidence"]["variant_count"], 2)
        # 非规范重复节点与引用冲突 ID 的子邮件都被跳过
        self.assertEqual(out["emails"][1]["skip_reason"], "reference_conflict")
        self.assertEqual(out["emails"][2]["skip_reason"], "reference_conflict")

    def test_identical_duplicate_not_reviewed(self):
        """字节相同的重复副本：不列复核，内容经台账归并全部来源。"""
        canon = mail(0, mid="<dup@x>", atts=[att("a.txt", A)], raw="same")
        dup = mail(1, mid="<dup@x>", atts=[att("a.txt", A)],
                   raw="same", root_uid=1)
        out = attachflow.analyze([canon, dup])
        self.assertEqual(out["reviews"], [])
        self.assertEqual(out["emails"][1]["skip_reason"], "duplicate_copy")
        entry = out["attachments"][0]
        self.assertEqual(entry["occurrence_count"], 2)
        self.assertEqual(
            [o["email"]["uid"] for o in entry["occurrences"]], [0, 1]
        )

    def test_self_reference_is_conflict_review(self):
        loop = mail(0, mid="<self@x>", irt="<self@x>", refs=["<self@x>"],
                    atts=[att("s.txt", A)], root_uid=0)
        out = attachflow.analyze([loop])
        self.assertEqual(out["events"], [])
        self.assertEqual(out["reviews"][0]["kind"], "reference_conflict")

    def test_undecodable_attachment_review_and_suppression(self):
        """无法解码附件：本身列待复核；对侧剩余实例抑制而非误报。"""
        root = mail(0, atts=[att("合同.txt", A), att("数据.csv", D)])
        child = reply(1, root, atts=[
            att("bad.bin", EMPTY, 0, undecodable=True),
            att("合同.txt", A),
        ])
        out = attachflow.analyze([root, child])
        # bad.bin 列待复核；数据.csv 的移除被抑制（可能就是 bad.bin）
        self.assertEqual(out["events"], [])
        self.assertEqual(len(out["reviews"]), 1)
        review = out["reviews"][0]
        self.assertEqual(review["kind"], "undecodable_attachment")
        self.assertEqual(review["evidence"]["filename"], "bad.bin")
        suppressed = out["emails"][1]["suppressed"]
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(suppressed[0]["type"], "removed")
        self.assertEqual(suppressed[0]["filename"], "数据.csv")

    def test_undecodable_on_parent_side_suppresses_added(self):
        root = mail(0, atts=[att("bad.bin", EMPTY, 0, undecodable=True)])
        child = reply(1, root, atts=[att("new.txt", E)])
        out = attachflow.analyze([root, child])
        self.assertEqual(out["events"], [])
        suppressed = out["emails"][1]["suppressed"]
        self.assertEqual(suppressed[0]["type"], "added")
        self.assertEqual(suppressed[0]["filename"], "new.txt")

    def test_legacy_metadata_fallback_marks_whole_email(self):
        """旧版结果（无 undecodable 标记、只有问题文本）：整封不可验证。"""
        root = mail(0, atts=[att("a.txt", A)])
        child = reply(
            1, root,
            atts=[att("a.txt", A), att("bad.bin", EMPTY, 0)],
            issues=["附件 'bad.bin' 内容无法解码，大小按 0 记录"],
        )
        out = attachflow.analyze([root, child])
        self.assertEqual(out["events"], [])
        self.assertEqual(len(out["reviews"]), 2)  # 两个附件都列待复核
        self.assertTrue(
            all(r["kind"] == "undecodable_attachment" for r in out["reviews"])
        )
        self.assertIn("旧版", out["reviews"][0]["evidence"]["note"])
        self.assertEqual(out["emails"][1]["verifiable_count"], 0)


class RegistryTest(unittest.TestCase):
    """内容台账：SHA-256 归并、首次出现位置与全部来源。"""

    def test_single_email_registry(self):
        """单封邮件（无父子边）也生成台账（保留行为）。"""
        only = mail(0, atts=[att("a.txt", A), att("b.txt", D)])
        out = attachflow.analyze([only])
        self.assertEqual(out["events"], [])
        self.assertEqual(len(out["attachments"]), 2)
        by_sha = {e["sha256"]: e for e in out["attachments"]}
        self.assertEqual(by_sha[A]["first_seen"]["email"]["uid"], 0)
        self.assertEqual(by_sha[A]["names"], ["a.txt"])
        self.assertEqual(by_sha[A]["occurrence_count"], 1)

    def test_first_seen_and_all_sources(self):
        root = mail(0, atts=[att("报告.pdf", F)], date="2026-09-03T09:00:00+00:00")
        # uid 更大但日期更早：首次出现按 (日期, uid) 取最早
        earlier = mail(1, mid="<m1@x>", atts=[att("报告.pdf", F)],
                       date="2026-09-01T09:00:00+00:00", root_uid=1)
        renamed = reply(2, root, atts=[att("报告-终版.pdf", F)])
        out = attachflow.analyze([root, earlier, renamed])
        entry = out["attachments"][0]
        self.assertEqual(entry["sha256"], F)
        self.assertEqual(entry["first_seen"]["email"]["uid"], 1)
        self.assertEqual(entry["names"], ["报告-终版.pdf", "报告.pdf"])
        self.assertEqual(entry["occurrence_count"], 3)
        self.assertEqual(
            [o["email"]["uid"] for o in entry["occurrences"]], [0, 1, 2]
        )

    def test_undecodable_excluded_from_registry(self):
        bad = mail(0, atts=[att("bad.bin", EMPTY, 0, undecodable=True)])
        out = attachflow.analyze([bad])
        self.assertEqual(out["attachments"], [])
        self.assertEqual(out["stats"]["unique_contents"], 0)


class StatsTest(unittest.TestCase):
    def test_stats_consistency(self):
        root = mail(0, atts=[att("a.txt", A), att("b.txt", D)])
        child = reply(1, root, atts=[att("a.txt", A), att("c.txt", E)])
        orphan = mail(2, mid="<o@x>", irt="<ghost@x>", refs=["<ghost@x>"],
                      atts=[att("x.bin", F)], root_uid=2)
        out = attachflow.analyze([root, child, orphan])
        stats = out["stats"]
        self.assertEqual(stats["emails"], 3)
        self.assertEqual(stats["attachments_total"], 5)
        self.assertEqual(stats["edges_compared"], 1)
        self.assertEqual(stats["events_total"], 2)
        self.assertEqual(stats["events_by_type"]["added"], 1)
        self.assertEqual(stats["events_by_type"]["removed"], 1)
        self.assertEqual(stats["reviews_total"], 1)
        self.assertEqual(stats["reviews_by_kind"]["missing_parent"], 1)
        self.assertEqual(stats["unique_contents"], 4)


if __name__ == "__main__":
    unittest.main()
