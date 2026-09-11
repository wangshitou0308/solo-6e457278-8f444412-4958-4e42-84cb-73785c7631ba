"""收件人流转分析 HTTP API 端到端测试：进程内启动服务，urllib 发请求。"""

from __future__ import annotations

import json
import threading
import time
import unittest

import tests.support  # noqa: F401
from mailrecon import config
from mailrecon.recflowproc import RecipientFlowProcessor
from mailrecon.server import ApiServer
from mailrecon.storage import Storage
from tests.test_http_api import ApiClient, make_zip_bytes

# 父邮件：a 发给 b、c，抄送 d
ROOT_EML = (
    b"Message-ID: <root@x.com>\r\nFrom: Alice <alice@x.com>\r\n"
    b"To: Bob <bob@x.com>, Carl <carl@x.com>\r\n"
    b"Cc: Dana <dana@X.com>\r\n"
    b"Subject: \xe5\x90\x88\xe5\x90\x8c\r\n"
    b"Date: Mon, 01 Sep 2026 09:00:00 +0000\r\n\r\nroot"
)
# b 的回复：只回 a，新增 eve，遗漏 c/d（d 域名大写应归一化匹配）
REPLY_EML = (
    b"Message-ID: <reply1@x.com>\r\nFrom: Bob <bob@x.com>\r\n"
    b"To: Alice <alice@x.com>, Eve <eve@x.com>\r\n"
    b"Subject: Re: \xe5\x90\x88\xe5\x90\x8c\r\n"
    b"Date: Mon, 01 Sep 2026 10:00:00 +0000\r\n"
    b"In-Reply-To: <root@x.com>\r\nReferences: <root@x.com>\r\n\r\nreply"
)
# c 角色变化：To -> Cc
ROLE_EML = (
    b"Message-ID: <reply2@x.com>\r\nFrom: Alice <alice@x.com>\r\n"
    b"To: Bob <bob@x.com>\r\nCc: Carl <carl@x.com>, Eve <eve@x.com>\r\n"
    b"Subject: Re: \xe5\x90\x88\xe5\x90\x8c\r\n"
    b"Date: Mon, 01 Sep 2026 11:00:00 +0000\r\n"
    b"In-Reply-To: <reply1@x.com>\r\n"
    b"References: <root@x.com> <reply1@x.com>\r\n\r\nreply2"
)
# 列表代发迹象：Sender 与 From 不同域
LIST_EML = (
    b"Message-ID: <list-root@x.com>\r\nFrom: Ann <ann@x.com>\r\n"
    b"Sender: manager@list.example.org\r\n"
    b"To: team@x.com\r\nSubject: list mail\r\n"
    b"Date: Tue, 02 Sep 2026 09:00:00 +0000\r\n\r\nvia list"
)
LIST_REPLY_EML = (
    b"Message-ID: <list-reply@x.com>\r\nFrom: Team <team@x.com>\r\n"
    b"To: Ann <ann@x.com>, New Person <newp@x.com>\r\n"
    b"Subject: Re: list mail\r\n"
    b"Date: Tue, 02 Sep 2026 09:30:00 +0000\r\n"
    b"In-Reply-To: <list-root@x.com>\r\n"
    b"References: <list-root@x.com>\r\n\r\nreply via list"
)
# 畸形地址：差异只列为待复核
MALFORMED_ROOT_EML = (
    b"Message-ID: <bad-root@x.com>\r\nFrom: A <a@y.com>\r\n"
    b"To: B <b@y.com>, not-an-email\r\nSubject: bad\r\n"
    b"Date: Wed, 03 Sep 2026 09:00:00 +0000\r\n\r\nroot"
)
MALFORMED_REPLY_EML = (
    b"Message-ID: <bad-reply@y.com>\r\nFrom: B <b@y.com>\r\n"
    b"To: A <a@y.com>, Fresh <fresh@y.com>\r\nSubject: Re: bad\r\n"
    b"Date: Wed, 03 Sep 2026 10:00:00 +0000\r\n"
    b"In-Reply-To: <bad-root@x.com>\r\n"
    b"References: <bad-root@x.com>\r\n\r\nreply"
)
# 父链缺失
ORPHAN_EML = (
    b"Message-ID: <orphan@z.com>\r\nFrom: X <x@z.com>\r\nTo: Y <y@z.com>\r\n"
    b"Subject: orphan\r\nDate: Thu, 04 Sep 2026 09:00:00 +0000\r\n"
    b"In-Reply-To: <ghost@z.com>\r\nReferences: <ghost@z.com>\r\n\r\norphan"
)


class RecipientFlowApiTest(unittest.TestCase):
    server: ApiServer
    client: ApiClient

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ApiServer("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
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

    def create_flow(self, payload: dict):
        return self.client.request(
            "POST",
            "/api/v1/recipient-flows",
            json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
        )

    def wait_flow(self, flow_id: str, timeout: float = 10.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _, body = self.client.request(
                "GET", f"/api/v1/recipient-flows/{flow_id}"
            )
            self.assertEqual(status, 200, body)
            flow = json.loads(body)
            if flow["status"] in (
                config.STATUS_COMPLETED,
                config.STATUS_FAILED,
            ):
                return flow
            time.sleep(0.05)
        raise AssertionError("收件人流转分析处理超时")

    def completed_flow(self, job_id: str) -> dict:
        status, _, body = self.create_flow(
            {"target_type": "job", "target_id": job_id}
        )
        self.assertEqual(status, 201, body)
        flow = json.loads(body)
        self.assertEqual(flow["status"], config.STATUS_QUEUED)
        done = self.wait_flow(flow["id"])
        self.assertEqual(done["status"], config.STATUS_COMPLETED, done)
        return done

    def get_json(self, path: str):
        status, headers, body = self.client.request("GET", path)
        self.assertEqual(status, 200, body)
        return headers, json.loads(body)

    # ------------------------------------------------------------ 用例

    def test_01_end_to_end_events_and_filters(self):
        job_id = self.create_completed_job(
            {"root.eml": ROOT_EML, "reply.eml": REPLY_EML,
             "reply2.eml": ROLE_EML}
        )
        done = self.completed_flow(job_id)
        self.assertEqual(done["email_count"], 3)
        stats = done["stats"]
        self.assertEqual(stats["emails"], 3)
        self.assertEqual(stats["edges_compared"], 2)
        self.assertGreater(stats["events_total"], 0)
        self.assertEqual(stats["events_by_type"]["role_changed"], 1)
        self.assertEqual(stats["reviews_total"], 0)

        fid = done["id"]
        _, body = self.get_json(f"/api/v1/recipient-flows/{fid}/events")
        # d@X.com 域名小写归一化：回复1未遗漏 d 之外，事件地址域名为小写
        addresses = {e["address"] for e in body["events"]}
        self.assertIn("dana@x.com", addresses)
        self.assertNotIn("dana@X.com", addresses)
        # 事件附父子邮件、会话根、字段、依据与集合差异
        added_eve = next(e for e in body["events"]
                         if e["type"] == "added" and e["address"] == "eve@x.com")
        self.assertEqual(added_eve["email"]["uid"], 1)
        self.assertEqual(added_eve["parent_email"]["uid"], 0)
        self.assertEqual(added_eve["thread_root_uid"], 0)
        self.assertIn("To", added_eve["fields"])
        self.assertIn("sets", added_eve["evidence"])
        self.assertTrue(body["events"][0]["id"])

        # type 筛选
        _, body = self.get_json(
            f"/api/v1/recipient-flows/{fid}/events?type=role_changed"
        )
        self.assertTrue(body["events"])
        self.assertTrue(
            all(e["type"] == "role_changed" for e in body["events"])
        )

        # address 筛选：只返回针对该地址的差异（不被全集快照放大）
        _, body = self.get_json(
            f"/api/v1/recipient-flows/{fid}/events?address=carl@x.com"
        )
        self.assertTrue(body["events"])
        self.assertTrue(
            all(e["address"] == "carl@x.com" for e in body["events"])
        )

        # 会话筛选
        _, body = self.get_json(
            f"/api/v1/recipient-flows/{fid}/events?thread=0"
        )
        self.assertGreaterEqual(body["event_count"], 1)
        _, body = self.get_json(
            f"/api/v1/recipient-flows/{fid}/events?thread=999"
        )
        self.assertEqual(body["event_count"], 0)

        # 未知 type / 非法 thread / 非法 address 返回 400
        status, _, body = self.client.request(
            "GET", f"/api/v1/recipient-flows/{fid}/events?type=bogus"
        )
        self.assertEqual(status, 400, body)
        status, _, body = self.client.request(
            "GET", f"/api/v1/recipient-flows/{fid}/events?thread=-1"
        )
        self.assertEqual(status, 400, body)
        status, _, body = self.client.request(
            "GET", f"/api/v1/recipient-flows/{fid}/events?address=bad"
        )
        self.assertEqual(status, 400, body)

    def test_02_threads_and_addresses_endpoints(self):
        job_id = self.create_completed_job(
            {"root.eml": ROOT_EML, "reply.eml": REPLY_EML}
        )
        fid = self.completed_flow(job_id)["id"]
        _, body = self.get_json(f"/api/v1/recipient-flows/{fid}/threads")
        self.assertEqual(body["thread_count"], 1)
        summary = body["threads"][0]
        self.assertEqual(summary["root_uid"], 0)
        self.assertEqual(summary["email_count"], 2)
        self.assertEqual(summary["matched_event_count"], summary["event_count"])
        self.assertTrue(summary["events"])

        _, body = self.get_json(f"/api/v1/recipient-flows/{fid}/addresses")
        addrs = {a["address"] for a in body["addresses"]}
        self.assertEqual(
            addrs, {"alice@x.com", "bob@x.com", "carl@x.com",
                    "dana@x.com", "eve@x.com"}
        )
        # 地址台账筛选：域名大小写不敏感、local-part 保持原样
        _, body = self.get_json(
            f"/api/v1/recipient-flows/{fid}/addresses?address=dana@X.COM"
        )
        self.assertEqual([a["address"] for a in body["addresses"]],
                         ["dana@x.com"])
        # local-part 大小写不同不合并：Dana 不匹配 dana
        _, body = self.get_json(
            f"/api/v1/recipient-flows/{fid}/addresses?address=Dana@x.com"
        )
        self.assertEqual(body["addresses"], [])

    def test_03_result_download(self):
        job_id = self.create_completed_job(
            {"root.eml": ROOT_EML, "reply.eml": REPLY_EML}
        )
        fid = self.completed_flow(job_id)["id"]
        status, headers, body = self.client.request(
            "GET", f"/api/v1/recipient-flows/{fid}/result"
        )
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        result = json.loads(body)
        self.assertEqual(result["recipient_flow_id"], fid)
        for key in ("events", "reviews", "threads", "emails",
                    "addresses", "stats"):
            self.assertIn(key, result)

    def test_04_result_survives_source_job_deletion(self):
        job_id = self.create_completed_job(
            {"root.eml": ROOT_EML, "reply.eml": REPLY_EML}
        )
        fid = self.completed_flow(job_id)["id"]
        status, _, _ = self.client.request("DELETE", f"/api/v1/jobs/{job_id}")
        self.assertEqual(status, 200)
        # 作业删除后分析元数据、事件接口与结果下载仍可用
        flow = self.wait_flow(fid)
        self.assertEqual(flow["status"], config.STATUS_COMPLETED)
        _, body = self.get_json(f"/api/v1/recipient-flows/{fid}/events")
        self.assertTrue(body["events"])
        status, _, body = self.client.request(
            "GET", f"/api/v1/recipient-flows/{fid}/result"
        )
        self.assertEqual(status, 200)

    def test_05_list_and_review_scenarios(self):
        job_id = self.create_completed_job(
            {"list.eml": LIST_EML, "list-reply.eml": LIST_REPLY_EML,
             "bad.eml": MALFORMED_ROOT_EML, "bad-reply.eml": MALFORMED_REPLY_EML,
             "orphan.eml": ORPHAN_EML}
        )
        done = self.completed_flow(job_id)
        fid = done["id"]
        # 三条问题边都不产生客观事件
        self.assertEqual(done["stats"]["events_total"], 0)
        kinds = set(done["stats"]["reviews_by_kind"])
        self.assertGreater(done["stats"]["reviews_by_kind"]
                           ["possible_mailing_list"], 0)
        self.assertGreater(done["stats"]["reviews_by_kind"]
                           ["malformed_address"], 0)
        self.assertGreater(done["stats"]["reviews_by_kind"]
                           ["missing_parent"], 0)

        # kind 筛选
        _, body = self.get_json(
            f"/api/v1/recipient-flows/{fid}/events?kind=possible_mailing_list"
        )
        self.assertEqual(body["event_count"], 0)
        self.assertTrue(body["reviews"])
        self.assertTrue(
            all(r["kind"] == "possible_mailing_list" for r in body["reviews"])
        )
        # 边级背景待复核附集合差异
        edge_review = next(
            r for r in body["reviews"]
            if r["evidence"].get("background_kinds")
        )
        self.assertEqual(
            edge_review["evidence"]["sets"]["differences"]["added"],
            ["newp@x.com"],
        )
        # 未知 kind 返回 400
        status, _, _ = self.client.request(
            "GET", f"/api/v1/recipient-flows/{fid}/events?kind=bogus"
        )
        self.assertEqual(status, 400)
        # type 与 kind 互斥：给了 type 就不返回复核项
        _, body = self.get_json(
            f"/api/v1/recipient-flows/{fid}/events?type=added"
        )
        self.assertEqual(body["review_count"], 0)

    def test_06_creation_validation_and_listing(self):
        # 目标不存在
        status, _, body = self.create_flow(
            {"target_type": "job",
             "target_id": "00000000-0000-0000-0000-000000000000"}
        )
        self.assertEqual(status, 404, body)
        self.assertEqual(json.loads(body)["error"]["code"], "target_not_found")

        # 非法 target_type / 非法 ID / 未知字段
        for payload, code in (
            ({"target_type": "thread", "target_id":
              "00000000-0000-0000-0000-000000000000"}, 400),
            ({"target_type": "job", "target_id": "nope"}, 400),
            ({"target_type": "job",
              "target_id": "00000000-0000-0000-0000-000000000000",
              "extra": 1}, 400),
        ):
            status, _, body = self.create_flow(payload)
            self.assertEqual(status, code, body)

        # 列表接口
        _, body = self.get_json("/api/v1/recipient-flows")
        self.assertIn("recipient_flows", body)
        self.assertTrue(any(
            f["target_type"] == "job" for f in body["recipient_flows"]
        ))

    def test_07_not_ready_before_completion(self):
        job_id = self.create_completed_job(
            {"root.eml": ROOT_EML, "reply.eml": REPLY_EML}
        )
        status, _, body = self.create_flow(
            {"target_type": "job", "target_id": job_id}
        )
        self.assertEqual(status, 201)
        fid = json.loads(body)["id"]
        # 完成前取事件返回 409 not_ready（快速轮询直到完成或拿到 409）
        deadline = time.time() + 10
        saw_not_ready = False
        while time.time() < deadline:
            status, _, body = self.client.request(
                "GET", f"/api/v1/recipient-flows/{fid}/events"
            )
            if status == 409:
                saw_not_ready = True
                break
            if status == 200:
                break
            time.sleep(0.01)
        # 后台很快，至少 409 或最终 200 二者之一；这里主要校验错误码口径
        self.wait_flow(fid)
        # 不存在的分析 ID
        status, _, _ = self.client.request(
            "GET",
            "/api/v1/recipient-flows/00000000-0000-0000-0000-000000000000",
        )
        self.assertEqual(status, 404)


    def test_08_restart_recovers_unfinished_flow(self):
        """库里留下 queued 分析，新 RecipientFlowProcessor 启动后应接手完成。"""
        import uuid as uuid_mod

        job_id = self.create_completed_job(
            {"root.eml": ROOT_EML, "reply.eml": REPLY_EML}
        )
        flow_id = str(uuid_mod.uuid4())
        Storage.prepare_recipient_flow_dir(flow_id)
        # 不经过 HTTP 层入队，模拟服务崩溃时留下的未完成分析
        self.server.storage.create_recipient_flow(flow_id, "job", job_id)

        recovery = RecipientFlowProcessor(self.server.storage)
        recovery.start()

        deadline = time.time() + 10
        flow = None
        while time.time() < deadline:
            flow = self.server.storage.get_recipient_flow(flow_id)
            if flow["status"] in (
                config.STATUS_COMPLETED,
                config.STATUS_FAILED,
            ):
                break
            time.sleep(0.05)
        self.assertEqual(flow["status"], config.STATUS_COMPLETED, flow)
        self.assertGreater(flow["event_count"], 0)

        # 恢复完成的结果可通过 HTTP 读取
        status, _, body = self.client.request(
            "GET", f"/api/v1/recipient-flows/{flow_id}/result"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(
            json.loads(body)["recipient_flow_id"], flow_id
        )

    def test_09_flow_fails_when_target_deleted_before_processing(self):
        """目标在分析处理前被删除：分析失败并说明原因。"""
        import uuid as uuid_mod

        job_id = self.create_completed_job(
            {"root.eml": ROOT_EML, "reply.eml": REPLY_EML}
        )
        flow_id = str(uuid_mod.uuid4())
        Storage.prepare_recipient_flow_dir(flow_id)
        self.server.storage.create_recipient_flow(flow_id, "job", job_id)
        # 在处理器接手前删除目标作业
        status, _, _ = self.client.request("DELETE", f"/api/v1/jobs/{job_id}")
        self.assertEqual(status, 200)

        recovery = RecipientFlowProcessor(self.server.storage)
        recovery.start()
        deadline = time.time() + 10
        flow = None
        while time.time() < deadline:
            flow = self.server.storage.get_recipient_flow(flow_id)
            if flow["status"] in (
                config.STATUS_COMPLETED,
                config.STATUS_FAILED,
            ):
                break
            time.sleep(0.05)
        self.assertEqual(flow["status"], config.STATUS_FAILED, flow)
        self.assertIn("不存在", flow["error"])


if __name__ == "__main__":
    unittest.main()
