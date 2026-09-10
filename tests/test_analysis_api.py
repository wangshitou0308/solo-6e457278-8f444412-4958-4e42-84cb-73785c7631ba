"""时序核验分析 HTTP API 端到端测试：进程内启动服务，urllib 发请求。"""

from __future__ import annotations

import json
import time
import unittest
import uuid

import tests.support  # noqa: F401
from mailrecon import config
from mailrecon.server import ApiServer
from mailrecon.storage import Storage
from mailrecon.timeproc import TimingProcessor
from tests.test_http_api import ApiClient, make_zip_bytes

# 正常两跳：Date 与首跳仅差 5 秒，跳间 15 秒
ROOT_EML = (
    b"Received: from mail.example.com by mx.example.org with ESMTPS;\r\n"
    b"\tMon, 01 Sep 2026 09:00:20 +0000\r\n"
    b"Received: from client-a ([192.0.2.1]) by mail.example.com with ESMTP;\r\n"
    b"\tMon, 01 Sep 2026 09:00:05 +0000\r\n"
    b"Message-ID: <root@x>\r\nFrom: A <a@x>\r\nTo: B <b@x>\r\n"
    b"Subject: root\r\nDate: Mon, 01 Sep 2026 09:00:00 +0000\r\n\r\nroot body"
)
# 客户端时钟偏差：Date 09:00，首跳 09:10（差 600 秒）
SKEW_EML = (
    b"Received: from client-b by mail.example.com with ESMTP;\r\n"
    b"\tMon, 01 Sep 2026 09:10:00 +0000\r\n"
    b"Message-ID: <skew@x>\r\nFrom: C <c@x>\r\nTo: B <b@x>\r\n"
    b"Subject: skew\r\nDate: Mon, 01 Sep 2026 09:00:00 +0000\r\n\r\nskew"
)
# 异常传输耗时：相邻跳间隔 30 分钟
SLOW_EML = (
    b"Received: from relay.example.com by mx.example.org with ESMTPS;\r\n"
    b"\tMon, 01 Sep 2026 09:30:00 +0000\r\n"
    b"Received: from client-d by relay.example.com with ESMTP;\r\n"
    b"\tMon, 01 Sep 2026 09:00:00 +0000\r\n"
    b"Message-ID: <slow@x>\r\nFrom: D <d@x>\r\nTo: B <b@x>\r\n"
    b"Subject: slow\r\nDate: Mon, 01 Sep 2026 09:00:00 +0000\r\n\r\nslow"
)
# 回复早于父邮件 1 小时
EARLY_REPLY_EML = (
    b"Message-ID: <early@x>\r\nFrom: B <b@x>\r\nTo: A <a@x>\r\n"
    b"Subject: Re: root\r\nDate: Mon, 01 Sep 2026 08:00:00 +0000\r\n"
    b"In-Reply-To: <root@x>\r\nReferences: <root@x>\r\n\r\nearly reply"
)
# 同一 Message-ID、不同 Received 链（用于案件级并列展示）
DUP_V1 = (
    b"Received: from a by relay-one.example; Mon, 01 Sep 2026 09:00:00 +0000\r\n"
    b"Message-ID: <dup@x>\r\nFrom: A <a@x>\r\nSubject: v1\r\n"
    b"Date: Mon, 01 Sep 2026 09:00:00 +0000\r\n\r\nversion one"
)
DUP_V2 = (
    b"Received: from a by relay-two.example; Mon, 01 Sep 2026 09:00:00 +0000\r\n"
    b"Message-ID: <dup@x>\r\nFrom: A <a@x>\r\nSubject: v2\r\n"
    b"Date: Mon, 01 Sep 2026 09:00:00 +0000\r\n\r\nversion two"
)


class AnalysisApiTest(unittest.TestCase):
    server: ApiServer
    client: ApiClient

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ApiServer("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        import threading

        cls.server.start()
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()
        cls.client = ApiClient(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    # ------------------------------------------------------------ 工具

    def create_completed_job(self, members: dict[str, bytes]) -> str:
        status, _, body = self.client.create_job(make_zip_bytes(members))
        self.assertEqual(status, 201, body)
        job = json.loads(body)
        done = self.client.wait_for(job["id"])
        self.assertEqual(done["status"], config.STATUS_COMPLETED, done)
        return job["id"]

    def create_analysis(self, payload: dict):
        return self.client.request(
            "POST",
            "/api/v1/analyses",
            json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
        )

    def wait_analysis(self, analysis_id: str, timeout: float = 10.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _, body = self.client.request(
                "GET", f"/api/v1/analyses/{analysis_id}"
            )
            self.assertEqual(status, 200, body)
            analysis = json.loads(body)
            if analysis["status"] in (
                config.STATUS_COMPLETED,
                config.STATUS_FAILED,
            ):
                return analysis
            time.sleep(0.05)
        raise AssertionError("分析处理超时")

    # ------------------------------------------------------------ 用例

    def test_01_job_analysis_end_to_end(self):
        job_id = self.create_completed_job(
            {
                "root.eml": ROOT_EML,
                "skew.eml": SKEW_EML,
                "slow.eml": SLOW_EML,
                "early.eml": EARLY_REPLY_EML,
            }
        )
        status, _, body = self.create_analysis(
            {
                "target_type": "job",
                "target_id": job_id,
                "thresholds": {
                    "clock_skew_seconds": 120,
                    "max_transit_seconds": 300,
                },
            }
        )
        self.assertEqual(status, 201, body)
        analysis = json.loads(body)
        self.assertEqual(analysis["status"], config.STATUS_QUEUED)
        self.assertEqual(analysis["target_type"], "job")
        self.assertEqual(
            analysis["thresholds"],
            {"clock_skew_seconds": 120, "max_transit_seconds": 300},
        )

        done = self.wait_analysis(analysis["id"])
        self.assertEqual(done["status"], config.STATUS_COMPLETED, done)
        self.assertEqual(done["email_count"], 4)
        stats = done["stats"]
        self.assertEqual(stats["emails"], 4)
        self.assertEqual(stats["emails_with_received"], 3)
        self.assertEqual(stats["hops_total"], 5)
        self.assertEqual(stats["findings_by_type"]["client_clock_skew"], 1)
        self.assertEqual(stats["findings_by_type"]["abnormal_transit"], 1)
        self.assertEqual(stats["findings_by_type"]["reply_before_parent"], 1)

        # 时间线：4 个条目，每条带定位时间与结论链接
        status, _, body = self.client.request(
            "GET", f"/api/v1/analyses/{analysis['id']}/timeline"
        )
        self.assertEqual(status, 200, body)
        timeline = json.loads(body)
        self.assertEqual(timeline["entry_count"], 4)
        self.assertEqual(timeline["filters"], {"from": None, "to": None, "type": None})
        by_mid = {e["message_id"]: e for e in timeline["entries"]}
        skew_entry = by_mid["<skew@x>"]
        self.assertEqual(len(skew_entry["findings"]), 1)
        finding = skew_entry["findings"][0]
        self.assertEqual(finding["type"], "client_clock_skew")
        self.assertEqual(finding["thresholds"], {"clock_skew_seconds": 120})
        self.assertEqual(finding["fields"], ["Date", "Received"])
        self.assertEqual(finding["evidence"]["skew_seconds"], 600.0)
        # root 的两跳解析正确（UTC 与原始时区保留）
        root_entry = by_mid["<root@x>"]
        self.assertEqual(root_entry["hop_count"], 2)

        # 下载结果 JSON
        status, headers, body = self.client.request(
            "GET", f"/api/v1/analyses/{analysis['id']}/result"
        )
        self.assertEqual(status, 200, body)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn("analysis-result.json", headers["Content-Disposition"])
        result = json.loads(body)
        self.assertEqual(result["analysis_id"], analysis["id"])
        self.assertEqual(result["target_id"], job_id)
        self.assertEqual(len(result["findings"]), 3)
        self.assertEqual(len(result["timeline"]), 4)
        # 结论含所用字段与阈值
        for f in result["findings"]:
            self.assertIn("fields", f)
            self.assertIn("thresholds", f)
            self.assertIn("evidence", f)

    def test_02_received_chain_in_job_tree(self):
        """作业结果节点携带按原顺序保留的 Received 跳点。"""
        job_id = self.create_completed_job({"root.eml": ROOT_EML})
        status, _, body = self.client.request(
            "GET", f"/api/v1/jobs/{job_id}/tree"
        )
        self.assertEqual(status, 200, body)
        root = json.loads(body)["threads"][0]
        hops = root["received"]
        self.assertEqual(len(hops), 2)
        # 原顺序：最上方（最后一跳）在前
        self.assertEqual(hops[0]["by_host"], "mx.example.org")
        self.assertEqual(hops[1]["by_host"], "mail.example.com")
        self.assertEqual(hops[0]["time_utc"], "2026-09-01T09:00:20+00:00")
        self.assertEqual(hops[0]["timezone"], "+0000")
        self.assertEqual(hops[1]["from_host"], "client-a")

    def test_03_timeline_filter_by_type(self):
        job_id = self.create_completed_job(
            {"root.eml": ROOT_EML, "skew.eml": SKEW_EML, "slow.eml": SLOW_EML}
        )
        status, _, body = self.create_analysis(
            {"target_type": "job", "target_id": job_id}
        )
        self.assertEqual(status, 201, body)
        analysis = json.loads(body)
        self.wait_analysis(analysis["id"])

        status, _, body = self.client.request(
            "GET",
            f"/api/v1/analyses/{analysis['id']}/timeline?type=client_clock_skew",
        )
        self.assertEqual(status, 200, body)
        timeline = json.loads(body)
        self.assertEqual(timeline["entry_count"], 1)
        entry = timeline["entries"][0]
        self.assertEqual(entry["message_id"], "<skew@x>")
        self.assertEqual(
            [f["type"] for f in entry["findings"]], ["client_clock_skew"]
        )
        self.assertEqual(timeline["filters"]["type"], "client_clock_skew")

        # 未知类型：400 并列出可选值
        status, _, body = self.client.request(
            "GET", f"/api/v1/analyses/{analysis['id']}/timeline?type=bogus"
        )
        self.assertEqual(status, 400, body)
        self.assertIn("client_clock_skew", json.loads(body)["error"]["message"])

    def test_04_timeline_filter_by_time_range(self):
        late_eml = (
            b"Message-ID: <late@x>\r\nFrom: E <e@x>\r\nTo: B <b@x>\r\n"
            b"Subject: late\r\nDate: Mon, 01 Sep 2026 12:00:00 +0000\r\n"
            b"\r\nlate"
        )
        job_id = self.create_completed_job(
            {"root.eml": ROOT_EML, "late.eml": late_eml}
        )
        status, _, body = self.create_analysis(
            {"target_type": "job", "target_id": job_id}
        )
        analysis = json.loads(body)
        self.wait_analysis(analysis["id"])

        # 只保留 10:00 之后的条目：late（Date 12:00）命中，root（09:00）排除
        status, _, body = self.client.request(
            "GET",
            f"/api/v1/analyses/{analysis['id']}/timeline"
            "?from=2026-09-01T10:00:00%2B00:00",
        )
        self.assertEqual(status, 200, body)
        timeline = json.loads(body)
        self.assertEqual(timeline["entry_count"], 1)
        self.assertEqual(
            timeline["entries"][0]["message_id"], "<late@x>"
        )

        # to 边界：只保留 10:00 之前
        status, _, body = self.client.request(
            "GET",
            f"/api/v1/analyses/{analysis['id']}/timeline"
            "?to=2026-09-01T10:00:00%2B00:00",
        )
        timeline = json.loads(body)
        self.assertEqual(timeline["entry_count"], 1)
        self.assertEqual(
            timeline["entries"][0]["message_id"], "<root@x>"
        )

        # 缺时区的时间边界：不猜测，400
        status, _, body = self.client.request(
            "GET",
            f"/api/v1/analyses/{analysis['id']}/timeline?from=2026-09-01T09:00:00",
        )
        self.assertEqual(status, 400, body)
        # 非法时间格式
        status, _, _ = self.client.request(
            "GET",
            f"/api/v1/analyses/{analysis['id']}/timeline?from=not-a-time",
        )
        self.assertEqual(status, 400)

    def test_05_case_analysis_chain_mismatch_and_job_deletion(self):
        """案件分析：同 Message-ID 传输链并列展示；删源作业不影响结果。"""
        j1 = self.create_completed_job({"v1.eml": DUP_V1})
        j2 = self.create_completed_job({"v2.eml": DUP_V2})
        payload = json.dumps({"name": "时序案件", "job_ids": [j1, j2]}).encode()
        status, _, body = self.client.request(
            "POST", "/api/v1/cases", payload,
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 201, body)
        case = json.loads(body)
        deadline = time.time() + 10
        while time.time() < deadline:
            _, _, body = self.client.request(
                "GET", f"/api/v1/cases/{case['id']}"
            )
            current = json.loads(body)
            if current["status"] == config.STATUS_COMPLETED:
                break
            time.sleep(0.05)
        self.assertEqual(current["status"], config.STATUS_COMPLETED, current)

        status, _, body = self.create_analysis(
            {"target_type": "case", "target_id": case["id"]}
        )
        self.assertEqual(status, 201, body)
        analysis = json.loads(body)
        done = self.wait_analysis(analysis["id"])
        self.assertEqual(done["status"], config.STATUS_COMPLETED, done)
        self.assertEqual(
            done["stats"]["findings_by_type"]["chain_mismatch"], 1
        )

        # 并列展示：两个版本的传输链都在结论里
        _, _, body = self.client.request(
            "GET",
            f"/api/v1/analyses/{analysis['id']}/timeline?type=chain_mismatch",
        )
        timeline = json.loads(body)
        self.assertEqual(timeline["entry_count"], 2)
        finding = timeline["entries"][0]["findings"][0]
        variants = finding["evidence"]["variants"]
        self.assertEqual(len(variants), 2)
        by_hosts = {v["hops"][0]["by_host"] for v in variants}
        self.assertEqual(
            by_hosts, {"relay-one.example", "relay-two.example"}
        )

        # 删除全部源作业：案件分析结果仍然可读
        for job_id in (j1, j2):
            status, _, _ = self.client.request(
                "DELETE", f"/api/v1/jobs/{job_id}"
            )
            self.assertEqual(status, 200)
        status, _, body = self.client.request(
            "GET", f"/api/v1/analyses/{analysis['id']}/timeline"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["entry_count"], 2)
        status, _, body = self.client.request(
            "GET", f"/api/v1/analyses/{analysis['id']}/result"
        )
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertEqual(result["target_type"], "case")
        self.assertEqual(result["target"]["case_id"], case["id"])

    def test_06_create_analysis_validation(self):
        ok_job = self.create_completed_job({"root.eml": ROOT_EML})

        # 非 JSON Content-Type
        status, _, _ = self.client.request(
            "POST", "/api/v1/analyses", b"{}",
            {"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        # 非法 JSON
        status, _, _ = self.client.request(
            "POST", "/api/v1/analyses", b"{nope",
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        # target_type 非法
        status, _, _ = self.create_analysis(
            {"target_type": "thread", "target_id": ok_job}
        )
        self.assertEqual(status, 400)
        # target_id 格式非法
        status, _, _ = self.create_analysis(
            {"target_type": "job", "target_id": "not-a-uuid"}
        )
        self.assertEqual(status, 400)
        # 未知阈值项
        status, _, body = self.create_analysis(
            {
                "target_type": "job",
                "target_id": ok_job,
                "thresholds": {"unknown_key": 1},
            }
        )
        self.assertEqual(status, 400, body)
        # 阈值类型非法（bool 也拒绝）
        status, _, _ = self.create_analysis(
            {
                "target_type": "job",
                "target_id": ok_job,
                "thresholds": {"clock_skew_seconds": True},
            }
        )
        self.assertEqual(status, 400)
        # 阈值超上限
        status, _, _ = self.create_analysis(
            {
                "target_type": "job",
                "target_id": ok_job,
                "thresholds": {"max_transit_seconds": 99999999},
            }
        )
        self.assertEqual(status, 400)
        # 目标不存在
        ghost = "00000000-0000-0000-0000-000000000000"
        status, _, body = self.create_analysis(
            {"target_type": "job", "target_id": ghost}
        )
        self.assertEqual(status, 404, body)
        self.assertEqual(json.loads(body)["error"]["code"], "target_not_found")

    def test_07_reject_unfinished_target(self):
        status, _, body = self.client.create_job(b"definitely not a zip")
        self.assertEqual(status, 201)
        job = json.loads(body)
        done = self.client.wait_for(job["id"])
        self.assertEqual(done["status"], config.STATUS_FAILED)
        status, _, body = self.create_analysis(
            {"target_type": "job", "target_id": job["id"]}
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(
            json.loads(body)["error"]["code"], "target_not_completed"
        )

    def test_08_missing_analysis_and_not_ready(self):
        status, _, _ = self.client.request("GET", "/api/v1/analyses/xx")
        self.assertEqual(status, 400)
        fake = "00000000-0000-0000-0000-000000000000"
        status, _, _ = self.client.request("GET", f"/api/v1/analyses/{fake}")
        self.assertEqual(status, 404)
        status, _, _ = self.client.request(
            "GET", f"/api/v1/analyses/{fake}/timeline"
        )
        self.assertEqual(status, 404)
        status, _, _ = self.client.request(
            "GET", f"/api/v1/analyses/{fake}/result"
        )
        self.assertEqual(status, 404)

        # 未完成时取时间线/结果：409 not_ready（极快机器上可能已完成）
        job_id = self.create_completed_job({"root.eml": ROOT_EML})
        status, _, body = self.create_analysis(
            {"target_type": "job", "target_id": job_id}
        )
        analysis = json.loads(body)
        _, _, body = self.client.request(
            "GET", f"/api/v1/analyses/{analysis['id']}"
        )
        current = json.loads(body)
        if current["status"] != config.STATUS_COMPLETED:
            status, _, _ = self.client.request(
                "GET", f"/api/v1/analyses/{analysis['id']}/timeline"
            )
            self.assertEqual(status, 409)
            status, _, _ = self.client.request(
                "GET", f"/api/v1/analyses/{analysis['id']}/result"
            )
            self.assertEqual(status, 409)
        self.wait_analysis(analysis["id"])

    def test_09_list_analyses(self):
        status, _, body = self.client.request("GET", "/api/v1/analyses")
        self.assertEqual(status, 200)
        self.assertIn("analyses", json.loads(body))

    def test_10_restart_recovers_unfinished_analysis(self):
        """库里留下 queued 分析，新 TimingProcessor 启动后应接手完成。"""
        job_id = self.create_completed_job({"skew.eml": SKEW_EML})
        analysis_id = str(uuid.uuid4())
        Storage.prepare_analysis_dir(analysis_id)
        # 不经过 HTTP 层入队，模拟服务崩溃时留下的未完成分析
        self.server.storage.create_analysis(
            analysis_id, "job", job_id,
            {"clock_skew_seconds": 60, "max_transit_seconds": 300},
        )

        recovery = TimingProcessor(self.server.storage)
        recovery.start()

        deadline = time.time() + 10
        analysis = None
        while time.time() < deadline:
            analysis = self.server.storage.get_analysis(analysis_id)
            if analysis["status"] in (
                config.STATUS_COMPLETED,
                config.STATUS_FAILED,
            ):
                break
            time.sleep(0.05)
        self.assertEqual(
            analysis["status"], config.STATUS_COMPLETED, analysis
        )
        # 自定义阈值（60 秒）被沿用
        self.assertEqual(analysis["thresholds"]["clock_skew_seconds"], 60)
        self.assertEqual(
            analysis["stats"]["findings_by_type"]["client_clock_skew"], 1
        )

        # 恢复完成的结果可通过 HTTP 读取
        status, _, body = self.client.request(
            "GET", f"/api/v1/analyses/{analysis_id}/result"
        )
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertEqual(result["analysis_id"], analysis_id)

    def test_11_analysis_fails_when_target_deleted_before_processing(self):
        """目标在分析处理前被删除：分析失败并说明原因。"""
        job_id = self.create_completed_job({"root.eml": ROOT_EML})
        analysis_id = str(uuid.uuid4())
        Storage.prepare_analysis_dir(analysis_id)
        self.server.storage.create_analysis(
            analysis_id, "job", job_id,
            {"clock_skew_seconds": 120, "max_transit_seconds": 300},
        )
        # 在处理器接手前删除目标作业
        status, _, _ = self.client.request("DELETE", f"/api/v1/jobs/{job_id}")
        self.assertEqual(status, 200)

        recovery = TimingProcessor(self.server.storage)
        recovery.start()
        deadline = time.time() + 10
        analysis = None
        while time.time() < deadline:
            analysis = self.server.storage.get_analysis(analysis_id)
            if analysis["status"] in (
                config.STATUS_COMPLETED,
                config.STATUS_FAILED,
            ):
                break
            time.sleep(0.05)
        self.assertEqual(analysis["status"], config.STATUS_FAILED, analysis)
        self.assertIn("不存在", analysis["error"])


if __name__ == "__main__":
    unittest.main()
