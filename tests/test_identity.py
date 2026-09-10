"""声明身份核验引擎（identity）单测：四类发现 + 待复核/无法核验口径。"""

from __future__ import annotations

import unittest

from mailrecon import identity


def addr(address: str, name: str | None = None) -> dict:
    domain = address.rsplit("@", 1)[1].lower() if "@" in address else None
    return {"name": name or "", "address": address, "domain": domain}


def from_entry(address: str, name: str | None = None) -> dict:
    a = addr(address, name)
    return {
        "index": 0, "raw": f"<{address}>", "count": 1,
        "addresses": [a], "anomalies": [], **a,
    }


def mid_entry(value: str, domain: str | None) -> dict:
    local = value.strip("<>").rsplit("@", 1)[0] if "@" in value else None
    return {
        "index": 0, "raw": value, "value": value,
        "local_part": local, "domain": domain, "anomalies": [],
    }


def dkim_entry(d: str, h: list[str], index: int = 0) -> dict:
    return {
        "index": index, "raw": "v=1; ...", "present": True,
        "d": d, "s": "s", "i": f"@{d}", "i_local_part": None,
        "i_domain": d, "h": h, "covers_from": "from" in h,
        "anomalies": [],
    }


def make_mail(
    uid: int,
    *,
    from_addr: str | None = "a@example.com",
    sender: str | None = None,
    return_path: str | None = None,
    reply_to: str | None = None,
    message_id: str | None = "<m@example.com>",
    dkim: list[dict] | None = None,
    subject: str = "话题",
    parent_uid: int | None = None,
    source_file: str | None = None,
) -> dict:
    ident = {
        "from": [from_entry(from_addr)] if from_addr else [],
        "sender": [from_entry(sender)] if sender else [],
        "reply_to": [from_entry(reply_to)] if reply_to else [],
        "return_path": [from_entry(return_path)] if return_path else [],
        "message_id": (
            {"present": True, "headers": [
                mid_entry(message_id, message_id.strip("<>").rsplit("@", 1)[-1])
            ]}
            if message_id else {"present": False, "headers": []}
        ),
        "dkim": dkim or [],
        "anomalies": [],
    }
    return {
        "uid": uid,
        "message_id": message_id,
        "subject": subject,
        "date": "2026-09-01T09:00:00+00:00",
        "identity": ident,
        "parent_uid": parent_uid,
        "source": {
            "job_id": "00000000-0000-0000-0000-000000000001",
            "source_file": source_file or f"{uid}.eml",
        },
    }


def findings_of(result, ftype: str) -> list[dict]:
    return [f for f in result["findings"] if f["type"] == ftype]


class FromDomainMismatchTest(unittest.TestCase):
    def test_matching_domains_no_finding(self):
        mails = [make_mail(0, from_addr="a@example.com",
                           sender="b@example.com", return_path="c@example.com")]
        result = identity.analyze(mails)
        self.assertEqual(
            findings_of(result, "from_domain_mismatch"), []
        )

    def test_return_path_domain_differs_is_observed(self):
        # 仅 Return-Path 域不同（无 Sender/Reply-To 列表迹象）=> 客观记录
        mails = [make_mail(
            0, from_addr="a@example.com",
            return_path="bounce@mailer.net",
        )]
        result = identity.analyze(mails)
        found = findings_of(result, "from_domain_mismatch")
        self.assertEqual(len(found), 1)
        f = found[0]
        self.assertEqual(f["status"], "observed")
        self.assertEqual(f["scope"], "email")
        rp = [c for c in f["evidence"]["comparisons"]
              if c["header"] == "Return-Path"][0]
        self.assertEqual(rp["domain"], "mailer.net")
        # 每条发现附来源文件与头字段
        self.assertEqual(f["sources"][0]["source_file"], "0.eml")
        self.assertIn("From", f["headers"])
        self.assertIn("Return-Path", f["headers"])
        self.assertTrue(f["basis"])

    def test_sender_domain_differs_is_list_hint_needs_review(self):
        # Sender 与 From 地址不同本身就是代发/列表迹象 => needs_review
        mails = [make_mail(
            0, from_addr="a@example.com", sender="bot@mailer.net",
        )]
        result = identity.analyze(mails)
        found = findings_of(result, "from_domain_mismatch")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["status"], "needs_review")
        self.assertIn(
            "possible_mailing_list", found[0]["evidence"]["review_signals"]
        )

    def test_missing_from_is_inconclusive(self):
        mails = [make_mail(0, from_addr=None, sender="bot@mailer.net")]
        result = identity.analyze(mails)
        self.assertEqual(findings_of(result, "from_domain_mismatch"), [])
        report = result["email_reports"][0]
        cell = report["checks"]["from_domain_mismatch"]
        self.assertEqual(cell["status"], "inconclusive")
        self.assertIn("From", cell["reason"])

    def test_mailing_list_signal_makes_it_needs_review(self):
        # Sender 不同域 + Reply-To 不同域 => 列表迹象 => needs_review
        mails = [make_mail(
            0, from_addr="user@example.com",
            sender="list@list.example.org",
            reply_to="list@list.example.org",
        )]
        result = identity.analyze(mails)
        found = findings_of(result, "from_domain_mismatch")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["status"], "needs_review")
        self.assertIn("possible_mailing_list", found[0]["evidence"]["review_signals"])
        # 待复核证据单独列出
        kinds = {r["kind"] for r in result["review_flags"]}
        self.assertIn("possible_mailing_list", kinds)


class MessageIdDomainDriftTest(unittest.TestCase):
    def test_email_level_match_and_drift(self):
        ok = make_mail(0, from_addr="a@example.com", message_id="<x@example.com>")
        drift = make_mail(1, from_addr="a@example.com", message_id="<x@other.net>")
        result = identity.analyze([ok, drift])
        found = findings_of(result, "message_id_domain_drift")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["scope"], "email")
        self.assertEqual(found[0]["evidence"]["message_id_domain"], "other.net")

    def test_missing_message_id_inconclusive(self):
        mails = [make_mail(0, message_id=None)]
        result = identity.analyze(mails)
        cell = result["email_reports"][0]["checks"]["message_id_domain_drift"]
        self.assertEqual(cell["status"], "inconclusive")

    def test_thread_level_same_sender_multiple_domains(self):
        # 同一 From 地址在同线程两封邮件中使用不同 Message-ID 域
        parent = make_mail(0, from_addr="h@example.com", message_id="<t1@example.com>")
        child = make_mail(
            1, from_addr="h@example.com", message_id="<t2@new-corp.example>",
            parent_uid=0, subject="Re: 话题",
        )
        result = identity.analyze([parent, child])
        thread_found = [
            f for f in findings_of(result, "message_id_domain_drift")
            if f["scope"] == "thread"
        ]
        self.assertEqual(len(thread_found), 1)
        self.assertEqual(thread_found[0]["status"], "needs_review")
        self.assertEqual(
            sorted(thread_found[0]["evidence"]["domains"]),
            ["example.com", "new-corp.example"],
        )


class ReplyIdentityChangeTest(unittest.TestCase):
    def test_same_domain_no_finding(self):
        parent = make_mail(0, from_addr="a@example.com")
        child = make_mail(1, from_addr="b@example.com", parent_uid=0)
        result = identity.analyze([parent, child])
        self.assertEqual(findings_of(result, "reply_identity_change"), [])

    def test_display_name_same_domain_changed_is_observed(self):
        parent = make_mail(0, from_addr="li@example.com")
        parent["identity"]["from"][0]["name"] = "李雷"
        child = make_mail(1, from_addr="li@examp1e.com", parent_uid=0)
        child["identity"]["from"][0]["name"] = "李雷"
        result = identity.analyze([parent, child])
        found = findings_of(result, "reply_identity_change")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["status"], "observed")
        self.assertEqual(found[0]["headers"], ["From", "In-Reply-To", "References"])
        self.assertEqual(found[0]["evidence"]["same_display_name"], True)
        # 父子两封的来源都附上
        self.assertEqual(len(found[0]["sources"]), 2)

    def test_new_participant_is_needs_review(self):
        parent = make_mail(0, from_addr="a@example.com")
        child = make_mail(1, from_addr="outsider@other.net", parent_uid=0)
        result = identity.analyze([parent, child])
        found = findings_of(result, "reply_identity_change")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["status"], "needs_review")

    def test_seen_domain_rotation_is_observed_low_key(self):
        # 祖父 other.net，父 example.com，回复又回到 other.net
        g = make_mail(0, from_addr="x@other.net")
        p = make_mail(1, from_addr="a@example.com", parent_uid=0)
        c = make_mail(2, from_addr="x@other.net", parent_uid=1)
        result = identity.analyze([g, p, c])
        found = findings_of(result, "reply_identity_change")
        types = {f["evidence"]["reply"]["uid"]: f["status"] for f in found}
        self.assertEqual(types.get(2), "observed")


class DkimCoverageTest(unittest.TestCase):
    def test_covers_from_no_finding(self):
        mails = [make_mail(0, dkim=[dkim_entry("example.com", ["from", "to"])])]
        result = identity.analyze(mails)
        self.assertEqual(findings_of(result, "dkim_from_not_covered"), [])
        cell = result["email_reports"][0]["checks"]["dkim_from_not_covered"]
        self.assertEqual(cell["status"], "observed")

    def test_h_without_from_is_observed(self):
        mails = [make_mail(0, dkim=[dkim_entry("example.com", ["to", "subject"])])]
        result = identity.analyze(mails)
        found = findings_of(result, "dkim_from_not_covered")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["status"], "observed")
        self.assertIn("未查 DNS", found[0]["summary"])

    def test_missing_dkim_is_inconclusive_not_forgery(self):
        mails = [make_mail(0, dkim=None)]
        result = identity.analyze(mails)
        self.assertEqual(findings_of(result, "dkim_from_not_covered"), [])
        cell = result["email_reports"][0]["checks"]["dkim_from_not_covered"]
        self.assertEqual(cell["status"], "inconclusive")
        self.assertIn("不代表无签名即伪造", cell["reason"])

    def test_dual_signature_one_covers_is_needs_review(self):
        mails = [make_mail(0, dkim=[
            dkim_entry("list.example.org", ["to", "subject"], index=0),
            dkim_entry("example.com", ["from", "to"], index=1),
        ])]
        result = identity.analyze(mails)
        found = findings_of(result, "dkim_from_not_covered")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["status"], "needs_review")

    def test_dkim_domains_recorded_but_signature_not_judged(self):
        mails = [make_mail(0, dkim=[dkim_entry("example.com", ["from"])])]
        result = identity.analyze(mails)
        dkim = result["email_reports"][0]["dkim"]
        self.assertEqual(dkim[0]["d"], "example.com")
        self.assertTrue(dkim[0]["covers_from"])


class ForwardAndMissingTest(unittest.TestCase):
    def test_forward_subject_is_review_evidence_not_direct_finding(self):
        # 主题 Fwd: + From/Sender 域不一致：差异发现降级 needs_review，
        # 且转发证据单独留痕
        mails = [make_mail(
            0, from_addr="ceo@bank.example", sender="fw@forwarder.example",
            subject="Fwd: 付款", message_id="<n@forwarder.example>",
        )]
        result = identity.analyze(mails)
        forward_flags = [
            r for r in result["review_flags"]
            if r["kind"] == "possible_forward"
        ]
        self.assertEqual(len(forward_flags), 1)
        self.assertEqual(forward_flags[0]["headers"], ["Subject"])
        for f in result["findings"]:
            self.assertNotEqual(f["status"], "observed")
            self.assertEqual(f["status"], "needs_review")

    def test_missing_from_and_dkim_flags(self):
        mails = [make_mail(0, from_addr=None, message_id=None, dkim=None)]
        result = identity.analyze(mails)
        missing = [
            r for r in result["review_flags"] if r["kind"] == "missing_header"
        ]
        self.assertTrue(missing)
        listed = ",".join(missing[0]["evidence"]["missing"])
        self.assertIn("From", listed)
        self.assertIn("Message-ID", listed)
        self.assertIn("DKIM-Signature", listed)
        # 来源文件附在证据上
        self.assertEqual(missing[0]["sources"][0]["source_file"], "0.eml")

    def test_no_forgery_wording_anywhere(self):
        # 所有摘要都不得直接下“伪造”定性（允许出现“不判定伪造”）
        mails = [make_mail(
            0, from_addr="ceo@bank.example",
            sender="evil@attacker.test", subject="Fwd: x",
        )]
        result = identity.analyze(mails)
        for f in result["findings"]:
            self.assertNotIn("判定为伪造", f["summary"])
            self.assertIn("不判定伪造", f["summary"])


class StructureTest(unittest.TestCase):
    def test_finding_ids_and_stats(self):
        mails = [
            make_mail(0, sender="bot@mailer.net"),
            make_mail(1, from_addr="x@examp1e.com",
                      message_id="<y@examp1e.com>", parent_uid=0),
        ]
        result = identity.analyze(mails)
        ids = [f["id"] for f in result["findings"]]
        self.assertEqual(ids, sorted(ids))
        self.assertTrue(ids[0].startswith("F"))
        stats = result["stats"]
        self.assertEqual(stats["emails"], 2)
        self.assertEqual(
            stats["findings_total"],
            sum(stats["findings_by_type"].values()),
        )
        self.assertIn("from_domain_mismatch", stats["findings_by_type"])
        self.assertIn("dkim_from_not_covered", stats["inconclusive_checks"])

    def test_thread_report_links_findings_and_sequences(self):
        parent = make_mail(0)
        child = make_mail(1, from_addr="b@other.net", parent_uid=0)
        result = identity.analyze([parent, child])
        self.assertEqual(len(result["thread_reports"]), 1)
        report = result["thread_reports"][0]
        self.assertEqual(report["email_count"], 2)
        self.assertEqual(len(report["finding_ids"]), 1)
        self.assertEqual(len(report["identity_sequences"]), 2)
        finding = next(
            f for f in result["findings"] if f["id"] == report["finding_ids"][0]
        )
        self.assertEqual(finding["type"], "reply_identity_change")

    def test_review_flags_have_source_and_basis(self):
        mails = [make_mail(0, from_addr=None)]
        result = identity.analyze(mails)
        for flag in result["review_flags"]:
            self.assertTrue(flag["sources"])
            self.assertTrue(flag["headers"])
            self.assertTrue(flag["basis"])
            self.assertIn("evidence", flag)


if __name__ == "__main__":
    unittest.main()
