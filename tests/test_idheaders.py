"""声明身份头解析（idheaders）单测：重复头保留、原始值留存、DKIM 标签。"""

from __future__ import annotations

import unittest
from email import message_from_bytes
from email.policy import default

from mailrecon.idheaders import parse_dkim_signature, parse_identity_headers


def parse(raw: bytes) -> dict:
    return parse_identity_headers(message_from_bytes(raw, policy=default))


class AddressHeaderTest(unittest.TestCase):
    def test_from_sender_replyto_returnpath_extracted(self):
        ident = parse(
            b"From: =?utf-8?b?5p2O6Zu3?= <lilei@example.com>\r\n"
            b"Sender:  <bounce@mailer.net>\r\n"
            b"Reply-To: legal-team@example.org\r\n"
            b"Return-Path: <bounce@mailer.net>\r\n"
            b"Subject: s\r\n\r\nbody"
        )
        self.assertEqual(ident["from"][0]["address"], "lilei@example.com")
        self.assertEqual(ident["from"][0]["domain"], "example.com")
        self.assertEqual(ident["from"][0]["name"], "李雷")
        self.assertEqual(ident["sender"][0]["domain"], "mailer.net")
        self.assertEqual(ident["reply_to"][0]["domain"], "example.org")
        self.assertEqual(ident["return_path"][0]["domain"], "mailer.net")

    def test_duplicate_headers_preserved_in_order_with_raw(self):
        # 两个 From 与两个 Return-Path：全部按原顺序保留，raw 保留折叠形态
        ident = parse(
            b"From: A <a@example.com>\r\n"
            b"From: B <b@example.org>\r\n"
            b"Return-Path: <x@example.com>\r\n"
            b"Return-Path: <y@example.org>\r\n\r\n"
        )
        self.assertEqual([f["domain"] for f in ident["from"]],
                         ["example.com", "example.org"])
        self.assertEqual(
            [rp["raw"] for rp in ident["return_path"]],
            ["<x@example.com>", "<y@example.org>"],
        )
        # 重复 From 本身不产生解析异常（From 允许多地址语义）
        kinds = [a["kind"] for a in ident["anomalies"]]
        self.assertNotIn("duplicate_header", kinds)

    def test_raw_folded_value_preserved(self):
        ident = parse(
            b"DKIM-Signature: v=1; d=example.com; s=sel;\r\n"
            b"\th=from:to;\r\n b=abc\r\n"
            b"From: A <a@example.com>\r\n\r\nbody"
        )
        raw = ident["dkim"][0]["raw"]
        self.assertIn("\r\n", raw)  # 原始折叠换行保留
        # 解析使用去折叠文本，标签仍可提取
        self.assertEqual(ident["dkim"][0]["d"], "example.com")
        self.assertEqual(ident["dkim"][0]["h"], ["from", "to"])

    def test_address_without_domain_recorded(self):
        ident = parse(b"From: root\r\nSender: postmaster\r\n\r\n")
        self.assertIsNone(ident["from"][0]["domain"])
        self.assertTrue(
            any("不含有效域名" in a["detail"] for a in ident["anomalies"])
        )

    def test_multiple_sender_addresses_is_anomaly(self):
        ident = parse(b"Sender: A <a@x.com>, B <b@x.com>\r\n\r\n")
        self.assertGreaterEqual(
            len(ident["sender"][0]["addresses"]), 2
        )
        self.assertTrue(
            any("只含一个地址" in a["detail"] for a in ident["anomalies"])
        )

    def test_empty_header_value(self):
        ident = parse(b"From: \r\n\r\n")
        self.assertEqual(ident["from"][0]["addresses"], [])
        self.assertIn("头值为空", ident["from"][0]["anomalies"])


class MessageIdHeaderTest(unittest.TestCase):
    def test_angle_msgid(self):
        ident = parse(b"Message-ID: <a.b-c@Mail.Example.COM>\r\n\r\n")
        h = ident["message_id"]["headers"][0]
        self.assertEqual(h["value"], "<a.b-c@Mail.Example.COM>")
        self.assertEqual(h["local_part"], "a.b-c")
        self.assertEqual(h["domain"], "mail.example.com")  # 域小写

    def test_missing_angle_accepted_with_anomaly(self):
        ident = parse(b"Message-ID: bare@example.com\r\n\r\n")
        h = ident["message_id"]["headers"][0]
        self.assertEqual(h["domain"], "example.com")
        self.assertTrue(any("缺少尖括号" in d for d in h["anomalies"]))

    def test_no_domain(self):
        ident = parse(b"Message-ID: <weird>\r\n\r\n")
        h = ident["message_id"]["headers"][0]
        self.assertIsNone(h["domain"])
        self.assertTrue(any("'@'" in d for d in h["anomalies"]))

    def test_duplicate_message_id_flagged(self):
        ident = parse(
            b"Message-ID: <a@x.com>\r\nMessage-ID: <b@y.com>\r\n\r\n"
        )
        self.assertEqual(len(ident["message_id"]["headers"]), 2)
        self.assertTrue(
            any(
                a["kind"] == "duplicate_header"
                and a["header"] == "message-id"
                for a in ident["anomalies"]
            )
        )

    def test_absent(self):
        ident = parse(b"From: a@x.com\r\n\r\n")
        self.assertFalse(ident["message_id"]["present"])
        self.assertEqual(ident["message_id"]["headers"], [])


class DkimHeaderTest(unittest.TestCase):
    def test_basic_tags_and_h_list(self):
        parsed = parse_dkim_signature(
            "v=1; a=rsa-sha256; d=Example.COM; s=selector; "
            "i=@example.com; h=From:To :Subject; b=zzz"
        )
        self.assertEqual(parsed["d"], "Example.COM")
        self.assertEqual(parsed["s"], "selector")
        self.assertEqual(parsed["i"], "@example.com")
        self.assertEqual(parsed["i_domain"], "example.com")
        self.assertEqual(parsed["h"], ["from", "to", "subject"])
        self.assertTrue(parsed["covers_from"])

    def test_h_is_case_insensitive_and_duplicates_kept(self):
        parsed = parse_dkim_signature(
            "v=1; d=x.com; s=s; h=FROM:from:to; b=z"
        )
        self.assertEqual(parsed["h"], ["from", "from", "to"])
        self.assertTrue(parsed["covers_from"])

    def test_missing_required_tags(self):
        parsed = parse_dkim_signature("v=1; h=from; b=z")
        self.assertIsNone(parsed["d"])
        self.assertIsNone(parsed["s"])
        details = " ".join(parsed["anomalies"])
        self.assertIn("缺少 d=", details)
        self.assertIn("缺少 s=", details)

    def test_missing_h_tag(self):
        parsed = parse_dkim_signature("v=1; d=x.com; s=s; b=z")
        self.assertFalse(parsed["covers_from"])
        self.assertTrue(any("缺少 h=" in d for d in parsed["anomalies"]))

    def test_duplicate_tag_invalid_but_first_kept(self):
        parsed = parse_dkim_signature(
            "v=1; d=x.com; d=y.com; s=s; h=from; b=z"
        )
        self.assertEqual(parsed["d"], "x.com")
        self.assertTrue(any("重复出现" in d for d in parsed["anomalies"]))

    def test_i_domain_not_under_d_recorded_but_not_verified(self):
        parsed = parse_dkim_signature(
            "v=1; d=example.com; s=s; i=user@evil.test; h=from; b=z"
        )
        self.assertTrue(
            any("不一致" in d for d in parsed["anomalies"])
        )
        # 子域是允许的形态，仅字符串比较
        parsed2 = parse_dkim_signature(
            "v=1; d=example.com; s=s; i=@mail.example.com; h=from; b=z"
        )
        self.assertFalse(
            any("不一致" in d for d in parsed2["anomalies"])
        )

    def test_b_tag_never_verified(self):
        # 无 b= 只记录缺失；有 b= 也绝不尝试验证
        parsed = parse_dkim_signature("v=1; d=x.com; s=s; h=from")
        self.assertTrue(any("缺少 b=" in d for d in parsed["anomalies"]))
        parsed2 = parse_dkim_signature("v=1; d=x.com; s=s; h=from; b=garbage")
        self.assertFalse(
            any("b=" in d and "签名" in d for d in parsed2["anomalies"])
        )

    def test_bad_version_and_nameless_spec(self):
        parsed = parse_dkim_signature("v=2; d=x.com; s=s; h=from; b=z; junk")
        details = " ".join(parsed["anomalies"])
        self.assertIn("v 标签值非 1", details)
        self.assertIn("不含 '='", details)

    def test_multiple_dkim_signatures_preserved(self):
        ident = parse(
            b"DKIM-Signature: v=1; d=a.com; s=1; h=from; b=x\r\n"
            b"DKIM-Signature: v=1; d=b.com; s=2; h=to; b=y\r\n\r\n"
        )
        self.assertEqual([d["d"] for d in ident["dkim"]], ["a.com", "b.com"])
        self.assertTrue(
            any(
                a["kind"] == "duplicate_header"
                and a["header"] == "dkim-signature"
                for a in ident["anomalies"]
            )
        )
        self.assertTrue(ident["dkim"][0]["covers_from"])
        self.assertFalse(ident["dkim"][1]["covers_from"])


class UndecodableBytesTest(unittest.TestCase):
    def test_invalid_header_bytes_do_not_crash_serialization(self):
        import json

        ident = parse(
            b"From: =?utf-8?b?5p2o?= <a@x.com>\r\n"
            b"Sender: stray\xff\xfe byte <s@x.com>\r\n\r\n"
        )
        # 结果必须可 JSON 序列化（无 surrogate 残留）
        json.dumps(ident, ensure_ascii=False)
        self.assertEqual(ident["sender"][0]["domain"], "x.com")


if __name__ == "__main__":
    unittest.main()
