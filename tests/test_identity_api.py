"""声明身份核验 HTTP API 端到端测试：进程内启动服务，urllib 发请求。"""

from __future__ import annotations

import json
import time
import unittest
import uuid

import tests.support  # noqa: F401
from mailrecon import config
from mailrecon.server import ApiServer
from mailrecon.storage import Storage
from mailrecon.identityproc import IdentityProcessor
from tests.test_http_api import ApiClient, make_zip_bytes

# 正常邮件：From/Sender/Return-Path 同域、DKIM 覆盖 From
NORMAL_EML = (
    b"Message-ID: <normal@example.com>\r\nFrom: A <a@example.com>\r\n"
    b"Sender: A <a@example.com>\r\nReturn-Path: <a@example.com>\r\n"
    b"To: B <b@x>\r\nSubject: normal\r\n"
    b"Date: Mon, 01 Sep 2026 09:00:00 +0000\r\n"
    b"DKIM-Signature: v=1; d=example.com; s=k; h=from:to; b=zz\r\n\r\nnormal"
)
# From 与 Return-Path 域不一致（observed）；DKIM h= 漏 From（observed）
MISMATCH_EML = (
    b"Message-ID: <mm@example.com>\r\nFrom: A <a@example.com>\r\n"
    b"Return-Path: <bounce@mailer.net>\r\nTo: B <b@x>\r\n"
    b"Subject: mismatch\r\nDate: Mon, 01 Sep 2026 10:00:00 +0000\r\n"
    b"DKIM-Signature: v=1; d=example.com; s=k; h=to:subject; b=zz\r\n\r\nmm"
)
# 回复链：显示名相同、域换成仿冒域（reply_identity_change observed）
REPLY_GOOD_EML = (
    b"Message-ID: <pay-1@example.com>\r\nFrom: =?utf-8?b?5p2O6Zu3?= <li@example.com>\r\n"
    b"To: B <b@x>\r\nSubject: Re: pay\r\n"
    b"Date: Mon, 01 Sep 2026 11:00:00 +0000\r\n"
    b"In-Reply-To: <normal@example.com>\r\n"
    b"References: <normal@example.com>\r\n\r\ngood reply"
)
REPLY_BAD_EML = (
    b"Message-ID: <pay-2@examp1e.com>\r\nFrom: =?utf-8?b?5p2O6Zu3?= <li@examp1e.com>\r\n"
    b"To: B <b@x>\r\nSubject: Re: pay\r\n"
    b"Date: Mon, 01 Sep 2026 12:00:00 +0000\r\n"
    b"In-Reply-To: <pay-1@example.com>\r\n"
    b"References: <normal@example.com> <pay-1@example.com>\r\n\r\nbad reply"
)
# 转发 + 列表：所有差异降级 needs_review，不判伪造
FORWARD_EML = (
    b"Message-ID: <fwd@forwarder.example>\r\nFrom: CEO <ceo@bank.example>\r\n"
    b"Sender: bot@forwarder.example\r\nReply-To: <it@forwarder.example>\r\n"
    b"Return-Path: <bot@forwarder.example>\r\nTo: A <a@example.com>\r\n"
    b"Subject: Fwd: notice\r\nDate: Mon, 01 Sep 2026 13:00:00 +0000\r\n\r\nfwd"
)
# 缺 From / Message-ID / DKIM：全部无法核验
MISSING_EML = b"To: B <b@x>\r\nSubject: anon\r\n\r\nanon"

# 案件用：两个包里同一 From 地址用不同 Message-ID 域（跨包回复）
PACK_A = {
    "a.eml": (
        b"Message-ID: <t-1@old.example>\r\nFrom: H <h@example.com>\r\n"
        b"To: B <b@x>\r\nSubject: weekly\r\n"
        b"Date: Mon, 01 Sep 2026 09:00:00 +0000\r\n\r\nv1"
    ),
}
PACK_B = {
    "b.eml": (
        b"Message-ID: <t-2@new-corp.example>\r\nFrom: H <h@example.com>\r\n"
        b"To: B <b@x>\r\nSubject: Re: weekly\r\n"
        b"Date: Mon, 01 Sep 2026 11:00:00 +0000\r\n"
        b"In-Reply-To: <t-1@old.example>\r\n"
        b"References: <t-1@old.example>\r\n\r\nv2"
    ),
}


class IdentityApiTest(unittest.TestCase):
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

    def create_check(self, payload: dict):
        return self.client.request(
            "POST",
            "/api/v1/identity-checks",
            json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
        )

    def wait_check(self, check_id: str, timeout: float = 10.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _, body = self.client.request(
                "GET", f"/api/v1/identity-checks/{check_id}"
            )
            self.assertEqual(status, 200, body)
            check = json.loads(body)
            if check["status"] in (
                config.STATUS_COMPLETED, config.STATUS_FAILED,
            ):
                return check
            time.sleep(0.05)
        raise AssertionError("核验处理超时")

    def completed_check(self, job_id: str) -> dict:
        status, _, body = self.create_check(
            {"target_type": "job", "target_id": job_id}
        )
        self.assertEqual(status, 201, body)
        return self.wait_check(json.loads(body)["id"])

    # ------------------------------------------------------------ 用例

    def test_01_job_check_end_to_end(self):
        job_id = self.create_completed_job({
            "normal.eml": NORMAL_EML,
            "mismatch.eml": MISMATCH_EML,
            "reply-good.eml": REPLY_GOOD_EML,
            "reply-bad.eml": REPLY_BAD_EML,
            "forward.eml": FORWARD_EML,
            "missing.eml": MISSING_EML,
        })
        status, _, body = self.create_check(
            {"target_type": "job", "target_id": job_id}
        )
        self.assertEqual(status, 201, body)
        created = json.loads(body)
        self.assertEqual(created["status"], config.STATUS_QUEUED)
        self.assertEqual(created["review_count"], 0)
        self.assertNotIn("thresholds", created)  # 身份核验无阈值参数

        done = self.wait_check(created["id"])
        self.assertEqual(done["status"], config.STATUS_COMPLETED, done)
        self.assertEqual(done["email_count"], 6)
        by_type = done["stats"]["findings_by_type"]
        self.assertGreaterEqual(by_type["from_domain_mismatch"], 2)
        self.assertGreaterEqual(by_type["reply_identity_change"], 1)
        self.assertGreaterEqual(by_type["message_id_domain_drift"], 1)
        self.assertGreaterEqual(by_type["dkim_from_not_covered"], 1)
        # 无法核验计数（缺 DKIM/From 等）
        incon = done["stats"]["inconclusive_checks"]
        self.assertGreaterEqual(incon["dkim_from_not_covered"], 1)

        # findings 端点
        status, _, body = self.client.request(
            "GET", f"/api/v1/identity-checks/{done['id']}/findings"
        )
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["finding_count"], done["finding_count"])
        for f in payload["findings"]:
            self.assertTrue(f["sources"])
            self.assertTrue(f["headers"])
            self.assertTrue(f["basis"])
            self.assertIn("evidence", f)
        # 待复核证据（转发/列表/缺失头/解析异常）
        self.assertGreater(payload["review_flag_count"], 0)
        kinds = {r["kind"] for r in payload["review_flags"]}
        self.assertIn("possible_forward", kinds)
        self.assertIn("possible_mailing_list", kinds)
        self.assertIn("missing_header", kinds)

        # 仿冒回复：observed，父子两来源都在
        reply = next(
            f for f in payload["findings"]
            if f["type"] == "reply_identity_change"
        )
        self.assertEqual(reply["status"], "observed")
        self.assertEqual(len(reply["sources"]), 2)
        self.assertEqual(
            reply["evidence"]["same_display_name"], True
        )

        # 下载结果 JSON
        status, headers, body = self.client.request(
            "GET", f"/api/v1/identity-checks/{done['id']}/result"
        )
        self.assertEqual(status, 200, body)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn("identity-result.json", headers["Content-Disposition"])
        result = json.loads(body)
        self.assertEqual(result["identity_check_id"], done["id"])
        self.assertEqual(result["target_id"], job_id)
        self.assertIn("email_reports", result)
        self.assertIn("thread_reports", result)

    def test_02_identity_block_in_job_tree_preserves_raw_duplicates(self):
        """作业树节点的 identity 块保留重复头与原始折叠值。"""
        eml = (
            b"DKIM-Signature: v=1; d=a.com; s=1;\r\n\th=from; b=x\r\n"
            b"DKIM-Signature: v=1; d=b.com; s=2; h=to; b=y\r\n"
            b"Message-ID: <dup@a.com>\r\nFrom: A <a@a.com>\r\n\r\nbody"
        )
        job_id = self.create_completed_job({"x.eml": eml})
        status, _, body = self.client.request(
            "GET", f"/api/v1/jobs/{job_id}/tree"
        )
        self.assertEqual(status, 200, body)
        node = json.loads(body)["threads"][0]
        ident = node["identity"]
        self.assertEqual(len(ident["dkim"]), 2)
        self.assertIn("\r\n", ident["dkim"][0]["raw"])
        self.assertEqual([d["d"] for d in ident["dkim"]], ["a.com", "b.com"])
        # 重复 DKIM 作为异常留痕，但不判断签名
        self.assertTrue(
            any(a["header"] == "dkim-signature" and a["kind"] == "duplicate_header"
                for a in ident["anomalies"])
        )
        self.assertEqual(ident["message_id"]["headers"][0]["domain"], "a.com")

    def test_03_findings_filter_by_type_and_domain(self):
        job_id = self.create_completed_job({
            "normal.eml": NORMAL_EML,
            "mismatch.eml": MISMATCH_EML,
        })
        done = self.completed_check(job_id)
        cid = done["id"]

        status, _, body = self.client.request(
            "GET", f"/api/v1/identity-checks/{cid}/findings?type=dkim_from_not_covered"
        )
        payload = json.loads(body)
        self.assertTrue(payload["findings"])
        self.assertTrue(all(
            f["type"] == "dkim_from_not_covered" for f in payload["findings"]
        ))

        # 按域名筛选：mailer.net 只命中 Return-Path 不一致
        status, _, body = self.client.request(
            "GET", f"/api/v1/identity-checks/{cid}/findings?domain=mailer.net"
        )
        payload = json.loads(body)
        self.assertTrue(payload["findings"])
        for f in payload["findings"]:
            self.assertEqual(f["type"], "from_domain_mismatch")
        self.assertEqual(payload["filters"]["domain"], "mailer.net")

        # 未知类型 / 非法状态 / 非法域名 -> 400
        for q in ("type=bogus", "status=bogus", "domain="):
            status, _, _ = self.client.request(
                "GET", f"/api/v1/identity-checks/{cid}/findings?{q}"
            )
            self.assertEqual(status, 400, q)

    def test_04_emails_endpoint_filter(self):
        job_id = self.create_completed_job({
            "normal.eml": NORMAL_EML,
            "mismatch.eml": MISMATCH_EML,
            "missing.eml": MISSING_EML,
        })
        done = self.completed_check(job_id)
        cid = done["id"]

        status, _, body = self.client.request(
            "GET", f"/api/v1/identity-checks/{cid}/emails?status=inconclusive"
        )
        emails = json.loads(body)["emails"]
        files = [e["source"]["source_file"] for e in emails]
        self.assertIn("missing.eml", files)
        self.assertNotIn("normal.eml", files)

        status, _, body = self.client.request(
            "GET",
            f"/api/v1/identity-checks/{cid}/emails?type=from_domain_mismatch",
        )
        emails = json.loads(body)["emails"]
        files = [e["source"]["source_file"] for e in emails]
        self.assertEqual(files, ["mismatch.eml"])

        # reply_identity_change 是会话级类型，邮件端点明确拒绝
        status, _, body = self.client.request(
            "GET",
            f"/api/v1/identity-checks/{cid}/emails?type=reply_identity_change",
        )
        self.assertEqual(status, 400, body)

    def test_05_threads_endpoint_groups_findings(self):
        job_id = self.create_completed_job({
            "normal.eml": NORMAL_EML,
            "reply-good.eml": REPLY_GOOD_EML,
            "reply-bad.eml": REPLY_BAD_EML,
        })
        done = self.completed_check(job_id)
        cid = done["id"]
        status, _, body = self.client.request(
            "GET", f"/api/v1/identity-checks/{cid}/threads"
        )
        self.assertEqual(status, 200, body)
        threads = json.loads(body)["threads"]
        self.assertEqual(len(threads), 1)
        report = threads[0]
        self.assertEqual(report["email_count"], 3)
        types = [f["type"] for f in report["findings"]]
        self.assertIn("reply_identity_change", types)

        # 类型筛选
        status, _, body = self.client.request(
            "GET",
            f"/api/v1/identity-checks/{cid}/threads?type=reply_identity_change",
        )
        threads = json.loads(body)["threads"]
        self.assertEqual(len(threads), 1)
        self.assertTrue(all(
            f["type"] == "reply_identity_change"
            for f in threads[0]["findings"]
        ))

    def test_06_case_check_and_source_deletion_independence(self):
        j1 = self.create_completed_job(PACK_A)
        j2 = self.create_completed_job(PACK_B)
        payload = json.dumps({"name": "身份案", "job_ids": [j1, j2]}).encode()
        status, _, body = self.client.request(
            "POST", "/api/v1/cases", payload,
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 201, body)
        case_id = json.loads(body)["id"]
        deadline = time.time() + 10
        while time.time() < deadline:
            _, _, body = self.client.request("GET", f"/api/v1/cases/{case_id}")
            current = json.loads(body)
            if current["status"] == config.STATUS_COMPLETED:
                break
            time.sleep(0.05)
        self.assertEqual(current["status"], config.STATUS_COMPLETED, current)

        status, _, body = self.create_check(
            {"target_type": "case", "target_id": case_id}
        )
        self.assertEqual(status, 201, body)
        done = self.wait_check(json.loads(body)["id"])
        self.assertEqual(done["status"], config.STATUS_COMPLETED, done)
        self.assertEqual(done["target_type"], "case")
        # 跨包会话级 Message-ID 域漂移
        status, _, body = self.client.request(
            "GET",
            f"/api/v1/identity-checks/{done['id']}/findings"
            "?type=message_id_domain_drift",
        )
        findings = json.loads(body)["findings"]
        scopes = {f["scope"] for f in findings}
        self.assertIn("thread", scopes)
        thread_f = next(f for f in findings if f["scope"] == "thread")
        self.assertEqual(
            sorted(thread_f["evidence"]["domains"]),
            ["new-corp.example", "old.example"],
        )

        # 删除源作业与案件：核验结果仍可读
        for job_id in (j1, j2):
            status, _, _ = self.client.request("DELETE", f"/api/v1/jobs/{job_id}")
            self.assertEqual(status, 200)
        status, _, body = self.client.request(
            "GET", f"/api/v1/identity-checks/{done['id']}/result"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["target"]["case_id"], case_id)

    def test_07_create_validation(self):
        ok_job = self.create_completed_job({"normal.eml": NORMAL_EML})
        # 非 JSON
        status, _, _ = self.client.request(
            "POST", "/api/v1/identity-checks", b"{}",
            {"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        # 非法 JSON
        status, _, _ = self.client.request(
            "POST", "/api/v1/identity-checks", b"{nope",
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        # target_type 非法
        status, _, _ = self.create_check(
            {"target_type": "thread", "target_id": ok_job}
        )
        self.assertEqual(status, 400)
        # target_id 非法
        status, _, _ = self.create_check(
            {"target_type": "job", "target_id": "not-uuid"}
        )
        self.assertEqual(status, 400)
        # 未知字段
        status, _, body = self.create_check(
            {"target_type": "job", "target_id": ok_job, "extra": 1}
        )
        self.assertEqual(status, 400, body)
        # 目标不存在
        ghost = "00000000-0000-0000-0000-000000000000"
        status, _, body = self.create_check(
            {"target_type": "job", "target_id": ghost}
        )
        self.assertEqual(status, 404, body)
        self.assertEqual(
            json.loads(body)["error"]["code"], "target_not_found"
        )

    def test_08_reject_unfinished_target(self):
        status, _, body = self.client.create_job(b"not a zip")
        self.assertEqual(status, 201)
        job = json.loads(body)
        self.client.wait_for(job["id"])
        status, _, body = self.create_check(
            {"target_type": "job", "target_id": job["id"]}
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(
            json.loads(body)["error"]["code"], "target_not_completed"
        )

    def test_09_missing_and_not_ready(self):
        status, _, _ = self.client.request(
            "GET", "/api/v1/identity-checks/xx"
        )
        self.assertEqual(status, 400)
        ghost = "00000000-0000-0000-0000-000000000000"
        for suffix in ("", "/findings", "/emails", "/threads", "/result"):
            status, _, _ = self.client.request(
                "GET", f"/api/v1/identity-checks/{ghost}{suffix}"
            )
            self.assertEqual(status, 404, suffix)

        job_id = self.create_completed_job({"normal.eml": NORMAL_EML})
        status, _, body = self.create_check(
            {"target_type": "job", "target_id": job_id}
        )
        check = json.loads(body)
        status, _, _ = self.client.request(
            "GET", f"/api/v1/identity-checks/{check['id']}/result"
        )
        # 极快机器上可能已完成；未完成时必须是 409
        self.assertIn(status, (200, 409))
        self.wait_check(check["id"])

    def test_10_list_checks(self):
        status, _, body = self.client.request("GET", "/api/v1/identity-checks")
        self.assertEqual(status, 200)
        self.assertIn("identity_checks", json.loads(body))

    def test_11_restart_recovers_unfinished_check(self):
        job_id = self.create_completed_job({"mismatch.eml": MISMATCH_EML})
        check_id = str(uuid.uuid4())
        Storage.prepare_identity_check_dir(check_id)
        # 不经过 HTTP 层入队，模拟服务崩溃留下的未完成核验
        self.server.storage.create_identity_check(check_id, "job", job_id)

        recovery = IdentityProcessor(self.server.storage)
        recovery.start()
        deadline = time.time() + 10
        check = None
        while time.time() < deadline:
            check = self.server.storage.get_identity_check(check_id)
            if check["status"] in (
                config.STATUS_COMPLETED, config.STATUS_FAILED,
            ):
                break
            time.sleep(0.05)
        self.assertEqual(check["status"], config.STATUS_COMPLETED, check)
        self.assertGreaterEqual(check["finding_count"], 1)
        # 恢复完成的结果可通过 HTTP 读取
        status, _, body = self.client.request(
            "GET", f"/api/v1/identity-checks/{check_id}/result"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(
            json.loads(body)["identity_check_id"], check_id
        )

    def test_12_check_fails_when_target_deleted_before_processing(self):
        job_id = self.create_completed_job({"normal.eml": NORMAL_EML})
        check_id = str(uuid.uuid4())
        Storage.prepare_identity_check_dir(check_id)
        self.server.storage.create_identity_check(check_id, "job", job_id)
        status, _, _ = self.client.request("DELETE", f"/api/v1/jobs/{job_id}")
        self.assertEqual(status, 200)

        recovery = IdentityProcessor(self.server.storage)
        recovery.start()
        deadline = time.time() + 10
        check = None
        while time.time() < deadline:
            check = self.server.storage.get_identity_check(check_id)
            if check["status"] in (
                config.STATUS_COMPLETED, config.STATUS_FAILED,
            ):
                break
            time.sleep(0.05)
        self.assertEqual(check["status"], config.STATUS_FAILED, check)
        self.assertIn("不存在", check["error"])


if __name__ == "__main__":
    unittest.main()
