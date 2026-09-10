"""Received 头解析单元测试。"""

from __future__ import annotations

import unittest

import tests.support  # noqa: F401
from mailrecon.received import parse_received_headers


class ReceivedParseTest(unittest.TestCase):
    def test_normal_hop_extracts_hosts_and_utc(self):
        hops = parse_received_headers(
            [
                "from mail-out.example.com (mail-out.example.com [203.0.113.10])\n"
                "\tby mx.example.org with ESMTPS id abc123;\n"
                "\tMon, 01 Sep 2026 09:00:12 +0200"
            ]
        )
        self.assertEqual(len(hops), 1)
        hop = hops[0]
        self.assertEqual(hop["index"], 0)
        self.assertEqual(hop["from_host"], "mail-out.example.com")
        self.assertEqual(hop["by_host"], "mx.example.org")
        # +0200 换算 UTC 为 07:00:12，原始时区标记保留
        self.assertEqual(hop["time_utc"], "2026-09-01T07:00:12+00:00")
        self.assertEqual(hop["time_original"], "2026-09-01T09:00:12+02:00")
        self.assertEqual(hop["timezone"], "+0200")
        self.assertEqual(hop["issues"], [])
        self.assertIn("mail-out.example.com", hop["raw"])

    def test_original_order_preserved(self):
        hops = parse_received_headers(
            [
                "from a by b; Mon, 01 Sep 2026 09:03:00 +0000",
                "from c by d; Mon, 01 Sep 2026 09:02:00 +0000",
                "from e by f; Mon, 01 Sep 2026 09:01:00 +0000",
            ]
        )
        self.assertEqual([h["index"] for h in hops], [0, 1, 2])
        self.assertEqual([h["from_host"] for h in hops], ["a", "c", "e"])

    def test_named_timezone_comment_stripped(self):
        hops = parse_received_headers(
            [
                "from clientpc ([192.0.2.5]) by mail-out.example.com "
                "with ESMTP; Mon, 01 Sep 2026 10:59:50 +0300 (MSK)"
            ]
        )
        hop = hops[0]
        self.assertEqual(hop["timezone"], "+0300")
        self.assertEqual(hop["time_utc"], "2026-09-01T07:59:50+00:00")
        self.assertEqual(hop["issues"], [])

    def test_missing_timezone_not_guessed(self):
        hops = parse_received_headers(
            ["by internal.local with LMTP; Mon, 01 Sep 2026 09:01:00"]
        )
        hop = hops[0]
        self.assertIsNone(hop["time_utc"])
        self.assertIsNone(hop["timezone"])
        # 原始（无时区）时间保留为证据
        self.assertEqual(hop["time_original"], "2026-09-01T09:01:00")
        self.assertTrue(any("缺少时区" in i for i in hop["issues"]))
        self.assertTrue(any("from 子句" in i for i in hop["issues"]))

    def test_unrecognized_named_timezone_not_guessed(self):
        hops = parse_received_headers(
            ["from a by b; Mon, 01 Sep 2026 09:00:12 CEST"]
        )
        hop = hops[0]
        self.assertIsNone(hop["time_utc"])
        self.assertEqual(hop["timezone"], "CEST")  # 原样保留为证据
        self.assertTrue(any("无法识别" in i for i in hop["issues"]))

    def test_recognized_named_timezone(self):
        hops = parse_received_headers(
            ["from a by b; Mon, 01 Sep 2026 09:00:12 GMT"]
        )
        hop = hops[0]
        self.assertEqual(hop["timezone"], "GMT")
        self.assertEqual(hop["time_utc"], "2026-09-01T09:00:12+00:00")

    def test_unparseable_date_keeps_evidence(self):
        hops = parse_received_headers(["from x by y; garbage-date"])
        hop = hops[0]
        self.assertIsNone(hop["time_utc"])
        self.assertIsNone(hop["time_original"])
        # 不可解析时不把尾部单词当时区
        self.assertIsNone(hop["timezone"])
        self.assertTrue(any("无法解析" in i for i in hop["issues"]))
        self.assertEqual(hop["raw"], "from x by y; garbage-date")

    def test_missing_semicolon(self):
        hops = parse_received_headers(["from a by b"])
        hop = hops[0]
        self.assertIsNone(hop["time_utc"])
        self.assertTrue(any("';'" in i for i in hop["issues"]))
        # 主机部分仍然提取
        self.assertEqual(hop["from_host"], "a")
        self.assertEqual(hop["by_host"], "b")

    def test_missing_by_clause(self):
        hops = parse_received_headers(
            ["from only-from.example; Mon, 01 Sep 2026 09:02:00 +0000"]
        )
        hop = hops[0]
        self.assertEqual(hop["from_host"], "only-from.example")
        self.assertIsNone(hop["by_host"])
        self.assertTrue(any("by 子句" in i for i in hop["issues"]))
        # 时间不受 by 缺失影响
        self.assertEqual(hop["time_utc"], "2026-09-01T09:02:00+00:00")

    def test_empty_date_part(self):
        hops = parse_received_headers(["from a by b;"])
        hop = hops[0]
        self.assertIsNone(hop["time_utc"])
        self.assertTrue(any("为空" in i for i in hop["issues"]))

    def test_negative_offset(self):
        hops = parse_received_headers(
            ["from a by b; Mon, 01 Sep 2026 04:00:00 -0500"]
        )
        self.assertEqual(hops[0]["time_utc"], "2026-09-01T09:00:00+00:00")
        self.assertEqual(hops[0]["timezone"], "-0500")

    def test_empty_input(self):
        self.assertEqual(parse_received_headers([]), [])


if __name__ == "__main__":
    unittest.main()
