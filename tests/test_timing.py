"""时序核验引擎（timing.py）单元测试。"""

from __future__ import annotations

import unittest

import tests.support  # noqa: F401
from mailrecon import timing

THRESHOLDS = {"clock_skew_seconds": 120, "max_transit_seconds": 300}


def hop(index, from_host="a", by_host="b", time_utc=None, issues=None):
    return {
        "index": index,
        "raw": f"raw-{index}",
        "from_host": from_host,
        "by_host": by_host,
        "time_utc": time_utc,
        "time_original": time_utc,
        "timezone": "+0000" if time_utc else None,
        "issues": issues or [],
    }


def mail(uid, mid=None, date=None, received=None, parent_uid=None, sha=None):
    return {
        "uid": uid,
        "message_id": mid or f"<m{uid}@x>",
        "subject": f"s{uid}",
        "from": {"name": "", "address": "a@x"},
        "date": date,
        "raw_sha256": sha or f"sha-{uid}",
        "received": received or [],
        "parent_uid": parent_uid,
        "source": {"job_id": "j", "source_file": f"{uid}.eml"},
    }


def types_of(findings):
    return [f["type"] for f in findings]


class ClockSkewTest(unittest.TestCase):
    def test_skew_over_threshold_flagged(self):
        emails = [
            mail(
                0,
                date="2026-09-01T09:00:00+00:00",
                received=[hop(0, time_utc="2026-09-01T09:05:00+00:00")],
            )
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(types_of(out["findings"]), ["client_clock_skew"])
        f = out["findings"][0]
        self.assertEqual(f["id"], "F0001")
        self.assertEqual(f["fields"], ["Date", "Received"])
        self.assertEqual(f["thresholds"], {"clock_skew_seconds": 120})
        self.assertEqual(f["evidence"]["skew_seconds"], 300.0)
        self.assertEqual(f["evidence"]["direction"], "client_behind")
        self.assertEqual(f["evidence"]["first_hop"]["index"], 0)

    def test_skew_within_threshold_not_flagged(self):
        emails = [
            mail(
                0,
                date="2026-09-01T09:00:00+00:00",
                received=[hop(0, time_utc="2026-09-01T09:01:59+00:00")],
            )
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(out["findings"], [])

    def test_client_ahead_direction(self):
        emails = [
            mail(
                0,
                date="2026-09-01T09:10:00+00:00",
                received=[hop(0, time_utc="2026-09-01T09:00:00+00:00")],
            )
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(
            out["findings"][0]["evidence"]["direction"], "client_ahead"
        )

    def test_naive_date_not_compared(self):
        """Date 缺时区：不猜测，不产生结论，时间线记录原因。"""
        emails = [
            mail(
                0,
                date="2026-09-01T09:00:00",  # 无时区
                received=[hop(0, time_utc="2026-09-01T23:00:00+00:00")],
            )
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(out["findings"], [])
        entry = out["timeline"][0]
        self.assertTrue(any("缺少时区" in n for n in entry["notes"]))
        # 定位时间降级为首跳时间
        self.assertEqual(entry["time"], "2026-09-01T23:00:00+00:00")
        self.assertEqual(entry["time_basis"], "received")

    def test_missing_date_and_received_no_finding(self):
        out = timing.analyze([mail(0)], THRESHOLDS)
        self.assertEqual(out["findings"], [])
        entry = out["timeline"][0]
        self.assertIsNone(entry["time"])
        self.assertTrue(any("缺少 Date 头" in n for n in entry["notes"]))


class TransitTest(unittest.TestCase):
    def test_slow_transit_flagged(self):
        # 文件顺序：最后一跳在最上方；路径方向为 index 0 -> 1
        emails = [
            mail(
                0,
                date="2026-09-01T09:00:00+00:00",
                received=[
                    hop(1, "b", "c", "2026-09-01T09:10:00+00:00"),
                    hop(0, "a", "b", "2026-09-01T09:00:00+00:00"),
                ],
            )
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(types_of(out["findings"]), ["abnormal_transit"])
        f = out["findings"][0]
        self.assertEqual(f["thresholds"], {"max_transit_seconds": 300})
        self.assertEqual(f["evidence"]["gap_seconds"], 600.0)
        self.assertEqual(f["evidence"]["earlier_hop"]["index"], 0)
        self.assertEqual(f["evidence"]["later_hop"]["index"], 1)

    def test_inversion_flagged_not_guessed(self):
        emails = [
            mail(
                0,
                date="2026-09-01T09:00:00+00:00",
                received=[
                    hop(1, "b", "c", "2026-09-01T08:50:00+00:00"),
                    hop(0, "a", "b", "2026-09-01T09:00:00+00:00"),
                ],
            )
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(types_of(out["findings"]), ["hop_time_inversion"])
        f = out["findings"][0]
        self.assertEqual(f["thresholds"], {})
        self.assertEqual(f["evidence"]["gap_seconds"], -600.0)
        self.assertIn("不猜测", f["summary"])

    def test_hop_without_time_skips_pair(self):
        """中间跳缺时间：涉及它的相邻对不比较，也不产生结论。"""
        emails = [
            mail(
                0,
                date="2026-09-01T09:00:00+00:00",
                received=[
                    hop(2, "c", "d", "2026-09-01T09:00:30+00:00"),
                    hop(1, "b", "c", None, issues=["日期时间缺少时区，未换算 UTC，本跳不参与时序比较"]),
                    hop(0, "a", "b", "2026-09-01T09:00:00+00:00"),
                ],
            )
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(out["findings"], [])
        entry = out["timeline"][0]
        self.assertTrue(any("第 1 跳" in n for n in entry["notes"]))

    def test_gap_exactly_at_threshold_not_flagged(self):
        emails = [
            mail(
                0,
                date="2026-09-01T09:00:00+00:00",
                received=[
                    hop(1, "b", "c", "2026-09-01T09:05:00+00:00"),
                    hop(0, "a", "b", "2026-09-01T09:00:00+00:00"),
                ],
            )
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(out["findings"], [])


class ReplyBeforeParentTest(unittest.TestCase):
    def test_reply_earlier_than_parent_flagged(self):
        emails = [
            mail(0, "<p@x>", date="2026-09-01T10:00:00+00:00"),
            mail(
                1, "<c@x>", date="2026-09-01T09:00:00+00:00", parent_uid=0
            ),
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(types_of(out["findings"]), ["reply_before_parent"])
        f = out["findings"][0]
        self.assertEqual(f["node_uids"], [1, 0])
        self.assertEqual(f["fields"], ["Date", "In-Reply-To", "References"])
        self.assertEqual(f["thresholds"], {"clock_skew_seconds": 120})
        self.assertEqual(f["evidence"]["diff_seconds"], 3600.0)
        self.assertEqual(f["evidence"]["parent"]["uid"], 0)

    def test_reply_within_tolerance_not_flagged(self):
        emails = [
            mail(0, "<p@x>", date="2026-09-01T10:00:00+00:00"),
            mail(
                1, "<c@x>", date="2026-09-01T09:59:00+00:00", parent_uid=0
            ),
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(out["findings"], [])

    def test_parent_without_date_not_compared(self):
        emails = [
            mail(0, "<p@x>", date=None),
            mail(
                1, "<c@x>", date="2026-09-01T09:00:00+00:00", parent_uid=0
            ),
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(out["findings"], [])


class ChainMismatchTest(unittest.TestCase):
    def test_different_chains_listed_side_by_side(self):
        emails = [
            mail(
                0, "<dup@x>", sha="sha-a",
                received=[hop(0, "a", "b", "2026-09-01T09:00:00+00:00")],
            ),
            mail(
                1, "<dup@x>", sha="sha-b",
                received=[hop(0, "a", "OTHER", "2026-09-01T09:00:00+00:00")],
            ),
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(types_of(out["findings"]), ["chain_mismatch"])
        f = out["findings"][0]
        self.assertEqual(f["fields"], ["Message-ID", "Received"])
        self.assertEqual(f["thresholds"], {})
        variants = f["evidence"]["variants"]
        self.assertEqual(len(variants), 2)
        self.assertEqual(variants[0]["raw_sha256"], "sha-a")
        self.assertEqual(variants[1]["raw_sha256"], "sha-b")
        # 并列展示：每个版本带完整跳点链
        self.assertEqual(variants[0]["hops"][0]["by_host"], "b")
        self.assertEqual(variants[1]["hops"][0]["by_host"], "OTHER")

    def test_identical_chains_not_flagged(self):
        hops = [hop(0, "a", "b", "2026-09-01T09:00:00+00:00")]
        emails = [
            mail(0, "<dup@x>", sha="sha-a", received=hops),
            mail(1, "<dup@x>", sha="sha-b", received=list(hops)),
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(out["findings"], [])

    def test_same_sha_not_flagged(self):
        """字节相同的重复副本传输链必然一致，不报。"""
        emails = [
            mail(0, "<dup@x>", sha="same"),
            mail(1, "<dup@x>", sha="same"),
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(out["findings"], [])

    def test_hop_count_difference(self):
        emails = [
            mail(
                0, "<dup@x>", sha="sha-a",
                received=[hop(0, "a", "b", "2026-09-01T09:00:00+00:00")],
            ),
            mail(
                1, "<dup@x>", sha="sha-b",
                received=[
                    hop(1, "b", "c", "2026-09-01T09:01:00+00:00"),
                    hop(0, "a", "b", "2026-09-01T09:00:00+00:00"),
                ],
            ),
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(types_of(out["findings"]), ["chain_mismatch"])
        self.assertIn("跳数不同", out["findings"][0]["summary"])


class TimelineAndStatsTest(unittest.TestCase):
    def test_timeline_sorted_with_timeless_entries_last(self):
        emails = [
            mail(0, date=None),
            mail(1, date="2026-09-01T10:00:00+00:00"),
            mail(2, date="2026-09-01T09:00:00+00:00"),
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(
            [e["node_uid"] for e in out["timeline"]], [2, 1, 0]
        )

    def test_finding_ids_linked_in_timeline(self):
        emails = [
            mail(
                0,
                date="2026-09-01T09:00:00+00:00",
                received=[hop(0, time_utc="2026-09-01T09:05:00+00:00")],
            )
        ]
        out = timing.analyze(emails, THRESHOLDS)
        entry = out["timeline"][0]
        self.assertEqual(entry["finding_ids"], ["F0001"])

    def test_stats(self):
        emails = [
            mail(
                0,
                date="2026-09-01T09:00:00+00:00",
                received=[
                    hop(1, "b", "c", "2026-09-01T09:10:00+00:00"),
                    hop(0, "a", "b", "2026-09-01T09:00:00+00:00"),
                ],
            ),
            mail(1, date=None),
        ]
        out = timing.analyze(emails, THRESHOLDS)
        stats = out["stats"]
        self.assertEqual(stats["emails"], 2)
        self.assertEqual(stats["emails_with_received"], 1)
        self.assertEqual(stats["hops_total"], 2)
        self.assertEqual(stats["findings_total"], 1)
        self.assertEqual(stats["findings_by_type"]["abnormal_transit"], 1)
        self.assertEqual(stats["findings_by_type"]["client_clock_skew"], 0)

    def test_finding_ids_sequential(self):
        emails = [
            mail(
                0,
                date="2026-09-01T09:00:00+00:00",
                received=[hop(0, time_utc="2026-09-01T09:05:00+00:00")],
            ),
            mail(
                1,
                date="2026-09-01T09:00:00+00:00",
                received=[hop(0, time_utc="2026-09-01T09:06:00+00:00")],
            ),
        ]
        out = timing.analyze(emails, THRESHOLDS)
        self.assertEqual(
            [f["id"] for f in out["findings"]], ["F0001", "F0002"]
        )


if __name__ == "__main__":
    unittest.main()
