"""收件人流转分析引擎（recipientflow.py）单元测试。"""

from __future__ import annotations

import unittest

import tests.support  # noqa: F401
from mailrecon import recipientflow


def addr(value, name=""):
    return {"name": name, "address": value}


def mail(
    uid,
    frm,
    to=None,
    cc=None,
    mid=None,
    parent_uid=None,
    irt=None,
    refs=None,
    date=None,
    issues=None,
    identity=None,
    root_uid=None,
    raw=None,
):
    return {
        "uid": uid,
        "message_id": mid or f"<m{uid}@x.com>",
        "subject": f"s{uid}",
        "from": frm,
        "to": to or [],
        "cc": cc or [],
        "date": date or f"2026-09-{uid + 1:02d}T09:00:00+00:00",
        "raw_sha256": raw or f"raw-{uid}",
        "in_reply_to": irt,
        "references": refs or [],
        "issues": issues or [],
        "identity": identity or {},
        "parent_uid": parent_uid,
        "thread_root_uid": root_uid if root_uid is not None else 0,
        "thread_root_message_id": "<m0@x.com>",
        "source": {"job_id": "j", "source_file": f"{uid}.eml"},
    }


def reply(uid, parent, **kw):
    """构造 parent 的回复（引用头与 parent_uid 一致）。"""
    return mail(
        uid,
        parent_uid=parent["uid"],
        irt=parent["message_id"],
        refs=[parent["message_id"]],
        **kw,
    )


def events(out):
    return [(e["type"], e["address"]) for e in out["events"]]


class NormalizeTest(unittest.TestCase):
    def test_domain_lower_local_preserved(self):
        self.assertEqual(
            recipientflow._normalize("User@Example.COM"), "User@example.com"
        )
        # local-part 大小写不合并（保持原样）
        self.assertNotEqual(
            recipientflow._normalize("User@x.com"),
            recipientflow._normalize("user@x.com"),
        )

    def test_display_name_ignored_via_dict(self):
        root = mail(0, addr("a@x.com", "张三"), [addr("b@x.com", "任意显示名")])
        child = reply(1, root, frm=addr("b@x.com", "李四"), to=[
            addr("a@x.com", "完全不同的显示名")
        ])
        out = recipientflow.analyze([root, child])
        # 显示名不同但地址相同：无新增、无遗漏
        self.assertEqual(out["events"], [])
        self.assertEqual(out["stats"]["edges_compared"], 1)

    def test_malformed_addresses(self):
        for bad in ("no-at-sign", "a@@x.com", "a@x com", "@x.com", "a@",
                    'a b@x.com', "a@.com", "a@x..com"):
            self.assertIsNone(recipientflow._normalize(bad), bad)
        for good in ("a@x.com", "A@X.COM", "a.b+tag@sub.x.com", "用户@例え.みんな"):
            self.assertIsNotNone(recipientflow._normalize(good), good)


class EdgeDiffTest(unittest.TestCase):
    """四类事件：新增 / 未继续列入 / 角色变化 / reply-all 遗漏。"""

    def test_added_and_reply_all_omitted_and_dropped(self):
        root = mail(0, addr("a@x.com"), [addr("b@x.com"), addr("c@x.com")],
                    cc=[addr("d@x.com")])
        # b 回复：To a（父发件人，正常）+ 新增 e；遗漏 c、d
        r1 = reply(1, root, frm=addr("b@x.com"),
                   to=[addr("a@x.com"), addr("e@x.com")])
        out = recipientflow.analyze([root, r1])
        self.assertEqual(
            events(out),
            [
                ("added", "e@x.com"),
                ("dropped", "c@x.com"),
                ("dropped", "d@x.com"),
                ("reply_all_omitted", "c@x.com"),
                ("reply_all_omitted", "d@x.com"),
            ],
        )
        # 父发件人 a 在子邮件 To 中：不算遗漏；子发件人 b 不算新增
        added = {e["address"] for e in out["events"] if e["type"] == "added"}
        self.assertNotIn("b@x.com", added)
        self.assertNotIn("a@x.com", added)
        omitted = {e["address"] for e in out["events"]
                   if e["type"] == "reply_all_omitted"}
        self.assertNotIn("a@x.com", omitted)

    def test_reply_all_when_reply_to_parent_sender_only(self):
        """只回复父发件人：父邮件的其他 To/Cc 全部计为遗漏。"""
        root = mail(0, addr("a@x.com"), [addr("b@x.com"), addr("c@x.com")],
                    cc=[addr("d@x.com")])
        r1 = reply(1, root, frm=addr("b@x.com"), to=[addr("a@x.com")])
        out = recipientflow.analyze([root, r1])
        self.assertEqual(
            sorted(a for t, a in events(out) if t == "reply_all_omitted"),
            ["c@x.com", "d@x.com"],
        )

    def test_role_change(self):
        root = mail(0, addr("a@x.com"), [addr("b@x.com"), addr("c@x.com")],
                    cc=[addr("d@x.com")])
        # c: To→Cc；d: Cc→To
        r1 = reply(1, root, frm=addr("b@x.com"),
                   to=[addr("a@x.com"), addr("d@x.com")],
                   cc=[addr("c@x.com")])
        out = recipientflow.analyze([root, r1])
        changes = {
            e["address"]: (e["evidence"]["from_role"], e["evidence"]["to_role"])
            for e in out["events"] if e["type"] == "role_changed"
        }
        self.assertEqual(changes["c@x.com"], ("to", "cc"))
        self.assertEqual(changes["d@x.com"], ("cc", "to"))

    def test_no_changes_no_events(self):
        root = mail(0, addr("a@x.com"), [addr("b@x.com")], cc=[addr("c@x.com")])
        # b 回复 a，c 保持 Cc：可见参与者集合完全一致
        r1 = reply(1, root, frm=addr("b@x.com"),
                   to=[addr("a@x.com")], cc=[addr("c@x.com")])
        out = recipientflow.analyze([root, r1])
        self.assertEqual(out["events"], [])

    def test_domain_case_insensitive_match(self):
        root = mail(0, addr("a@x.com"), [addr("b@X.com"), addr("C@X.com")])
        r1 = reply(1, root, frm=addr("b@x.com"), to=[
            addr("a@X.com"), addr("C@x.com")
        ])
        out = recipientflow.analyze([root, r1])
        self.assertEqual(out["events"], [])

    def test_local_case_does_not_merge(self):
        root = mail(0, addr("John@x.com"), [addr("b@x.com")])
        r1 = reply(1, root, frm=addr("b@x.com"), to=[addr("john@x.com")])
        out = recipientflow.analyze([root, r1])
        types = {t for t, _ in events(out)}
        self.assertIn("added", types)
        self.assertIn("reply_all_omitted", types)

    def test_every_event_has_required_context(self):
        root = mail(0, addr("a@x.com"), [addr("b@x.com"), addr("c@x.com")])
        r1 = reply(1, root, frm=addr("b@x.com"), to=[addr("a@x.com")])
        out = recipientflow.analyze([root, r1])
        for event in out["events"]:
            self.assertIn("id", event)
            self.assertEqual(event["email"]["uid"], 1)
            self.assertEqual(event["parent_email"]["uid"], 0)
            self.assertEqual(event["thread_root_uid"], 0)
            self.assertTrue(event["fields"])
            self.assertIn(event["address"], event["summary"])
            sets = event["evidence"]["sets"]
            self.assertEqual(sets["parent"]["to"], ["b@x.com", "c@x.com"])
            self.assertIn("differences", sets)
            self.assertIn("basis", event)


class ReviewTest(unittest.TestCase):
    """不推断原则：列表/群组、畸形地址、父链缺失、引用冲突只待复核。"""

    def test_missing_parent_not_compared(self):
        orphan = mail(0, addr("a@x.com"), [addr("b@x.com")],
                      mid="<orphan@x.com>", irt="<ghost@x.com>",
                      refs=["<ghost@x.com>"], root_uid=0)
        out = recipientflow.analyze([orphan])
        self.assertEqual(out["events"], [])
        self.assertEqual(out["reviews"][0]["kind"], "missing_parent")
        self.assertEqual(out["emails"][0]["skip_reason"], "missing_parent")

    def test_walked_up_ancestor_not_compared(self):
        root = mail(0, addr("g@x.com"), [addr("b@x.com")], mid="<grand@x.com>")
        child = mail(1, addr("b@x.com"), [addr("g@x.com")],
                     mid="<child@x.com>", parent_uid=0,
                     irt="<gone@x.com>", refs=["<gone@x.com>", "<grand@x.com>"])
        out = recipientflow.analyze([root, child])
        self.assertEqual(out["events"], [])
        self.assertTrue(
            any(r["kind"] == "missing_parent" for r in out["reviews"])
        )

    def test_reference_conflict(self):
        canon = mail(0, addr("a@x.com"), [addr("b@x.com")],
                     mid="<conf@x.com>", raw="sha-1", root_uid=0)
        dup = mail(1, addr("c@x.com"), [addr("b@x.com")],
                   mid="<conf@x.com>", raw="sha-2", root_uid=1)
        child = reply(2, canon, frm=addr("b@x.com"),
                      to=[addr("a@x.com"), addr("z@x.com")],
                      mid="<child@x.com>")
        out = recipientflow.analyze([canon, dup, child])
        self.assertEqual(out["events"], [])
        self.assertTrue(
            all(r["kind"] == "reference_conflict" for r in out["reviews"])
        )

    def test_self_reference_is_conflict(self):
        loop = mail(0, addr("a@x.com"), [addr("b@x.com")],
                    mid="<self@x.com>", irt="<self@x.com>",
                    refs=["<self@x.com>"], root_uid=0)
        out = recipientflow.analyze([loop])
        self.assertEqual(out["reviews"][0]["kind"], "reference_conflict")

    def test_identical_duplicate_is_skipped_without_review(self):
        canon = mail(0, addr("a@x.com"), [addr("b@x.com")],
                     mid="<dup@x.com>", raw="same", root_uid=0)
        dup = mail(1, addr("a@x.com"), [addr("b@x.com")],
                   mid="<dup@x.com>", raw="same", root_uid=1)
        out = recipientflow.analyze([canon, dup])
        self.assertEqual(out["reviews"], [])
        self.assertEqual(out["emails"][1]["skip_reason"], "duplicate_copy")

    def test_malformed_child_address_no_events(self):
        root = mail(0, addr("a@x.com"), [addr("b@x.com"), addr("c@x.com")])
        child = reply(1, root, frm=addr("b@x.com"),
                      to=[addr("a@x.com"), addr("not-an-email")])
        out = recipientflow.analyze([root, child])
        self.assertEqual(out["events"], [])
        kinds = [r["kind"] for r in out["reviews"]]
        self.assertIn("malformed_address", kinds)
        malformed = next(r for r in out["reviews"]
                         if r["kind"] == "malformed_address"
                         and r["email"]["uid"] == 1)
        self.assertEqual(malformed["evidence"]["raw_address"], "not-an-email")
        self.assertEqual(malformed["evidence"]["field"], "To")

    def test_malformed_parent_address_no_events(self):
        root = mail(0, addr("a@x.com"),
                    [addr("b@x.com"), {"name": "", "address": "bad@@x"}])
        child = reply(1, root, frm=addr("b@x.com"),
                      to=[addr("a@x.com"), addr("new@x.com")])
        out = recipientflow.analyze([root, child])
        self.assertEqual(out["events"], [])
        self.assertTrue(
            any(r["kind"] == "malformed_address"
                and r["email"]["uid"] == 0 for r in out["reviews"])
        )

    def test_mailing_list_sender_hint_downgrades_edge(self):
        identity = {
            "from": [{"address": "a@x.com", "domain": "x.com",
                      "raw": "a@x.com",
                      "addresses": [{"address": "a@x.com"}]}],
            "sender": [{"address": "manager@list.example.org",
                        "domain": "list.example.org"}],
            "reply_to": [],
        }
        root = mail(0, addr("a@x.com"), [addr("team@x.com")], identity=identity)
        child = reply(1, root, frm=addr("team@x.com"),
                      to=[addr("a@x.com"), addr("new@x.com")])
        out = recipientflow.analyze([root, child])
        self.assertEqual(out["events"], [])
        # 一封邮件级列表迹象 + 一条边级背景待复核
        edge_review = next(
            r for r in out["reviews"]
            if r["evidence"].get("background_kinds")
        )
        self.assertIn("possible_mailing_list",
                      edge_review["evidence"]["background_kinds"])
        # 边级待复核仍附集合差异
        diffs = edge_review["evidence"]["sets"]["differences"]
        self.assertEqual(diffs["added"], ["new@x.com"])
        self.assertFalse(out["emails"][1]["compared"])

    def test_list_shaped_local_part_hint(self):
        root = mail(0, addr("a@x.com"),
                    [addr("proj-owners@x.com"), addr("b@x.com")])
        child = reply(1, root, frm=addr("b@x.com"), to=[addr("a@x.com")])
        out = recipientflow.analyze([root, child])
        self.assertEqual(out["events"], [])
        self.assertTrue(
            any(r["kind"] == "possible_mailing_list" for r in out["reviews"])
        )

    def test_group_syntax_from_hint(self):
        identity = {
            "from": [{"address": "a@x.com", "domain": "x.com",
                      "raw": "项目组: a@x.com, b@x.com;",
                      "addresses": [{"address": "a@x.com"},
                                    {"address": "b@x.com"}]}],
            "sender": [],
            "reply_to": [],
        }
        root = mail(0, addr("a@x.com"), [addr("c@x.com")], identity=identity)
        child = reply(1, root, frm=addr("c@x.com"),
                      to=[addr("a@x.com"), addr("d@x.com")])
        out = recipientflow.analyze([root, child])
        self.assertEqual(out["events"], [])
        self.assertTrue(
            any("群组语法" in "；".join(r["evidence"].get("hints", []))
                for r in out["reviews"] if r["kind"] == "possible_mailing_list")
        )


class RegistryAndStatsTest(unittest.TestCase):
    def test_address_registry(self):
        root = mail(0, {"name": "Alice", "address": "a@x.com"},
                    [{"name": "Bob", "address": "b@x.com"}])
        child = reply(1, root, frm={"name": "Robert", "address": "b@x.com"},
                      to=[{"name": "Alice", "address": "a@x.com"}])
        out = recipientflow.analyze([root, child])
        registry = {a["address"]: a for a in out["addresses"]}
        self.assertEqual(set(registry), {"a@x.com", "b@x.com"})
        # 同一地址的多个显示名都保留
        self.assertEqual(set(registry["b@x.com"]["names"]), {"Bob", "Robert"})
        self.assertEqual(registry["b@x.com"]["occurrence_count"], 2)
        self.assertEqual(registry["a@x.com"]["first_seen"]["email"]["uid"], 0)

    def test_thread_summary(self):
        root = mail(0, addr("a@x.com"), [addr("b@x.com"), addr("c@x.com")])
        r1 = reply(1, root, frm=addr("b@x.com"), to=[addr("a@x.com")])
        out = recipientflow.analyze([root, r1])
        self.assertEqual(len(out["threads"]), 1)
        summary = out["threads"][0]
        self.assertEqual(summary["root_uid"], 0)
        self.assertEqual(summary["email_count"], 2)
        self.assertEqual(sorted(summary["node_uids"]), [0, 1])
        self.assertEqual(summary["event_count"], len(out["events"]))

    def test_stats_consistency(self):
        root = mail(0, addr("a@x.com"), [addr("b@x.com"), addr("c@x.com")])
        r1 = reply(1, root, frm=addr("b@x.com"), to=[addr("a@x.com")])
        orphan = mail(2, addr("x@y.com"), [addr("z@y.com")],
                      mid="<o@y.com>", irt="<g@y.com>", refs=["<g@y.com>"],
                      root_uid=2)
        out = recipientflow.analyze([root, r1, orphan])
        stats = out["stats"]
        self.assertEqual(stats["emails"], 3)
        self.assertEqual(stats["edges_total"], 1)
        self.assertEqual(stats["edges_compared"], 1)
        self.assertEqual(stats["events_total"], len(out["events"]))
        self.assertEqual(
            stats["events_by_type"]["reply_all_omitted"],
            sum(1 for e in out["events"]
                if e["type"] == "reply_all_omitted"),
        )
        self.assertEqual(stats["reviews_by_kind"]["missing_parent"], 1)
        self.assertEqual(stats["threads_total"], 2)


if __name__ == "__main__":
    unittest.main()
