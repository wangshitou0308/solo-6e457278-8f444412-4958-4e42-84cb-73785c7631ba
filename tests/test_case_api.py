"""案件合并 HTTP API 端到端测试：进程内启动服务，urllib 发请求。"""

from __future__ import annotations

import json
import time
import unittest
import uuid

import tests.support  # noqa: F401
from mailrecon import config
from mailrecon.caseproc import CaseProcessor
from mailrecon.server import ApiServer
from mailrecon.storage import Storage
from tests.test_http_api import ApiClient, make_zip_bytes

ROOT_EML = (
    b"Message-ID: <root@x>\r\nFrom: A <a@x>\r\nTo: B <b@x>\r\n"
    b"Subject: root\r\nDate: Mon, 01 Sep 2026 09:00:00 +0000\r\n\r\nroot body"
)
REPLY_EML = (
    b"Message-ID: <reply@x>\r\nFrom: B <b@x>\r\nTo: A <a@x>\r\n"
    b"Subject: Re: root\r\nDate: Mon, 01 Sep 2026 10:00:00 +0000\r\n"
    b"In-Reply-To: <root@x>\r\nReferences: <root@x>\r\n\r\nreply body"
)
CONFLICT_V1 = (
    b"Message-ID: <dup@x>\r\nFrom: A <a@x>\r\nSubject: v1\r\n"
    b"Date: Mon, 01 Sep 2026 11:00:00 +0000\r\n\r\nversion one"
)
CONFLICT_V2 = (
    b"Message-ID: <dup@x>\r\nFrom: A <a@x>\r\nSubject: v2\r\n"
    b"Date: Mon, 01 Sep 2026 12:00:00 +0000\r\n\r\nversion two, different"
)


class CaseApiTest(unittest.TestCase):
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

    def create_case(self, job_ids, name="测试案件", raw_headers=None):
        payload = json.dumps({"name": name, "job_ids": job_ids}).encode()
        headers = {"Content-Type": "application/json"}
        if raw_headers:
            headers.update(raw_headers)
        return self.client.request("POST", "/api/v1/cases", payload, headers)

    def wait_case(self, case_id: str, timeout: float = 10.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _, body = self.client.request(
                "GET", f"/api/v1/cases/{case_id}"
            )
            self.assertEqual(status, 200, body)
            case = json.loads(body)
            if case["status"] in (
                config.STATUS_COMPLETED,
                config.STATUS_FAILED,
            ):
                return case
            time.sleep(0.05)
        raise AssertionError("案件处理超时")

    # ------------------------------------------------------------ 用例

    def test_01_create_case_merges_and_relinks(self):
        j1 = self.create_completed_job({"reply.eml": REPLY_EML})  # 缺父
        j2 = self.create_completed_job({"root.eml": ROOT_EML})

        status, _, body = self.create_case([j1, j2])
        self.assertEqual(status, 201, body)
        case = json.loads(body)
        self.assertEqual(case["status"], config.STATUS_QUEUED)
        self.assertEqual(case["job_ids"], [j1, j2])
        self.assertEqual(case["name"], "测试案件")

        done = self.wait_case(case["id"])
        self.assertEqual(done["status"], config.STATUS_COMPLETED, done)
        self.assertEqual(done["email_count"], 2)
        self.assertEqual(done["thread_count"], 1)
        stats = done["stats"]
        self.assertEqual(stats["job_count"], 2)
        self.assertEqual(stats["relinked_nodes"], 1)
        self.assertEqual(len(stats["job_contributions"]), 2)

        # 合并树：root@x 为根，reply@x 跨包补链挂上
        status, _, body = self.client.request(
            "GET", f"/api/v1/cases/{case['id']}/tree"
        )
        self.assertEqual(status, 200, body)
        tree = json.loads(body)
        self.assertEqual(tree["case_id"], case["id"])
        self.assertEqual(len(tree["source_jobs"]), 2)
        self.assertEqual(len(tree["threads"]), 1)
        root = tree["threads"][0]
        self.assertEqual(root["message_id"], "<root@x>")
        child = root["children"][0]
        self.assertEqual(child["message_id"], "<reply@x>")
        relink = child["merge_info"]["relinked_parent"]
        self.assertEqual(relink["message_id"], "<root@x>")
        self.assertEqual(relink["resolved_for_jobs"], [j1])
        self.assertEqual(child["sources"][0]["job_id"], j1)

        # compact 视图
        status, _, body = self.client.request(
            "GET", f"/api/v1/cases/{case['id']}/tree?view=compact"
        )
        self.assertEqual(status, 200, body)
        compact = json.loads(body)
        croot = compact["threads"][0]
        self.assertNotIn("body_text", croot)
        self.assertIn("sources", croot)
        self.assertTrue(compact["threads"][0]["children"][0]["relinked"])

    def test_02_duplicate_email_merges_sources(self):
        j1 = self.create_completed_job({"root.eml": ROOT_EML})
        j2 = self.create_completed_job({"backup/root-copy.eml": ROOT_EML})

        status, _, body = self.create_case([j1, j2])
        self.assertEqual(status, 201, body)
        case = json.loads(body)
        done = self.wait_case(case["id"])
        self.assertEqual(done["status"], config.STATUS_COMPLETED, done)
        self.assertEqual(done["stats"]["merged_nodes"], 1)
        self.assertEqual(done["stats"]["duplicates_merged"], 1)

        _, _, body = self.client.request(
            "GET", f"/api/v1/cases/{case['id']}/tree"
        )
        tree = json.loads(body)
        root = tree["threads"][0]
        self.assertEqual(len(root["sources"]), 2)
        self.assertEqual(
            {s["job_id"] for s in root["sources"]}, {j1, j2}
        )
        self.assertEqual(
            {s["source_file"] for s in root["sources"]},
            {"root.eml", "backup/root-copy.eml"},
        )

    def test_03_conflict_kept_side_by_side_and_not_guessed(self):
        j1 = self.create_completed_job({"v1.eml": CONFLICT_V1})
        j2 = self.create_completed_job(
            {"v2.eml": CONFLICT_V2, "child.eml": (
                b"Message-ID: <child@x>\r\nFrom: B <b@x>\r\nSubject: c\r\n"
                b"Date: Mon, 01 Sep 2026 13:00:00 +0000\r\n"
                b"In-Reply-To: <dup@x>\r\nReferences: <dup@x>\r\n\r\nc"
            )}
        )
        status, _, body = self.create_case([j1, j2])
        self.assertEqual(status, 201, body)
        case = json.loads(body)
        done = self.wait_case(case["id"])
        self.assertEqual(done["status"], config.STATUS_COMPLETED, done)
        stats = done["stats"]
        self.assertEqual(stats["conflict_groups"], 1)
        self.assertEqual(stats["conflict_nodes"], 2)
        self.assertEqual(stats["ambiguous_references"], 1)

        _, _, body = self.client.request(
            "GET", f"/api/v1/cases/{case['id']}/tree"
        )
        tree = json.loads(body)
        # 两个冲突版本 + 无法确定父节点的子邮件：三棵独立的树
        self.assertEqual(len(tree["threads"]), 3)
        by_mid = {}
        stack = list(tree["threads"])
        while stack:
            current = stack.pop()
            by_mid[current["message_id"]] = current
            stack.extend(current["children"])
        dup_nodes = [
            n for n in tree["threads"] if n["message_id"] == "<dup@x>"
        ]
        self.assertEqual(len(dup_nodes), 2)
        for n in dup_nodes:
            self.assertTrue(n["merge_info"]["conflict"])
            self.assertEqual(len(n["merge_info"]["conflict_with"]), 1)
        child = by_mid["<child@x>"]
        self.assertIn(child, tree["threads"])  # 未猜测父节点，独立成根
        self.assertTrue(
            any("为避免猜测" in note for note in child["merge_info"]["notes"])
        )

    def test_04_result_download_and_source_jobs_deletable(self):
        j1 = self.create_completed_job({"reply.eml": REPLY_EML})
        j2 = self.create_completed_job({"root.eml": ROOT_EML})
        status, _, body = self.create_case([j1, j2])
        self.assertEqual(status, 201)
        case = json.loads(body)
        self.wait_case(case["id"])

        # 下载结果 JSON
        status, headers, body = self.client.request(
            "GET", f"/api/v1/cases/{case['id']}/result"
        )
        self.assertEqual(status, 200, body)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn("case-result.json", headers["Content-Disposition"])
        result = json.loads(body)
        self.assertEqual(result["case_id"], case["id"])
        self.assertEqual(result["stats"]["relinked_nodes"], 1)
        self.assertEqual(len(result["threads"]), 1)

        # 案件完成后删除全部源作业：结果仍然可读
        for job_id in (j1, j2):
            status, _, body = self.client.request(
                "DELETE", f"/api/v1/jobs/{job_id}"
            )
            self.assertEqual(status, 200, body)
            status, _, _ = self.client.request(
                "GET", f"/api/v1/jobs/{job_id}"
            )
            self.assertEqual(status, 404)

        status, _, body = self.client.request(
            "GET", f"/api/v1/cases/{case['id']}/tree"
        )
        self.assertEqual(status, 200, body)
        tree = json.loads(body)
        self.assertEqual(len(tree["threads"]), 1)
        status, _, body = self.client.request(
            "GET", f"/api/v1/cases/{case['id']}/result"
        )
        self.assertEqual(status, 200, body)

    def test_05_reject_unfinished_source_job(self):
        # 失败的作业（非 ZIP）
        status, _, body = self.client.create_job(b"definitely not a zip")
        self.assertEqual(status, 201)
        job = json.loads(body)
        done = self.client.wait_for(job["id"])
        self.assertEqual(done["status"], config.STATUS_FAILED)

        ok_job = self.create_completed_job({"root.eml": ROOT_EML})
        status, _, body = self.create_case([ok_job, job["id"]])
        self.assertEqual(status, 409, body)
        err = json.loads(body)["error"]
        self.assertEqual(err["code"], "job_not_completed")
        self.assertIn(job["id"], err["message"])
        self.assertIn("failed", err["message"])

    def test_06_reject_deleted_or_missing_source_job(self):
        ok_job = self.create_completed_job({"root.eml": ROOT_EML})
        ghost = "00000000-0000-0000-0000-000000000000"
        status, _, body = self.create_case([ok_job, ghost])
        self.assertEqual(status, 404, body)
        err = json.loads(body)["error"]
        self.assertEqual(err["code"], "job_not_found")
        self.assertIn(ghost, err["message"])

        # 先创建再删除的作业同样被拒绝
        victim = self.create_completed_job({"root.eml": ROOT_EML})
        status, _, _ = self.client.request("DELETE", f"/api/v1/jobs/{victim}")
        self.assertEqual(status, 200)
        status, _, body = self.create_case([ok_job, victim])
        self.assertEqual(status, 404, body)
        self.assertEqual(json.loads(body)["error"]["code"], "job_not_found")

    def test_07_reject_duplicate_job_in_same_case(self):
        ok_job = self.create_completed_job({"root.eml": ROOT_EML})
        status, _, body = self.create_case([ok_job, ok_job])
        self.assertEqual(status, 409, body)
        err = json.loads(body)["error"]
        self.assertEqual(err["code"], "duplicate_job")
        self.assertIn(ok_job, err["message"])

    def test_08_create_case_validation(self):
        ok_job = self.create_completed_job({"root.eml": ROOT_EML})

        # 非 JSON Content-Type
        status, _, _ = self.client.request(
            "POST", "/api/v1/cases", b"{}",
            {"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)

        # 非法 JSON
        status, _, body = self.client.request(
            "POST", "/api/v1/cases", b"{not json",
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400, body)

        # 空 job_ids
        status, _, body = self.create_case([])
        self.assertEqual(status, 400, body)

        # job_ids 元素格式非法
        status, _, body = self.create_case(["not-a-uuid"])
        self.assertEqual(status, 400, body)

        # name 类型非法
        payload = json.dumps({"name": 123, "job_ids": [ok_job]}).encode()
        status, _, body = self.client.request(
            "POST", "/api/v1/cases", payload,
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400, body)

    def test_09_missing_case_and_bad_id(self):
        status, _, _ = self.client.request("GET", "/api/v1/cases/xx")
        self.assertEqual(status, 400)
        fake = "00000000-0000-0000-0000-000000000000"
        status, _, _ = self.client.request("GET", f"/api/v1/cases/{fake}")
        self.assertEqual(status, 404)
        status, _, _ = self.client.request(
            "GET", f"/api/v1/cases/{fake}/tree"
        )
        self.assertEqual(status, 404)
        status, _, _ = self.client.request(
            "GET", f"/api/v1/cases/{fake}/result"
        )
        self.assertEqual(status, 404)

    def test_10_tree_before_ready_returns_409(self):
        j1 = self.create_completed_job({"root.eml": ROOT_EML})
        status, _, body = self.create_case([j1])
        self.assertEqual(status, 201)
        case = json.loads(body)
        # 极快的机器可能已处理完，仅在未完成时断言 409
        status, _, body = self.client.request(
            "GET", f"/api/v1/cases/{case['id']}"
        )
        current = json.loads(body)
        if current["status"] != config.STATUS_COMPLETED:
            status, _, _ = self.client.request(
                "GET", f"/api/v1/cases/{case['id']}/tree"
            )
            self.assertEqual(status, 409)
            status, _, _ = self.client.request(
                "GET", f"/api/v1/cases/{case['id']}/result"
            )
            self.assertEqual(status, 409)
        self.wait_case(case["id"])

    def test_11_restart_recovers_unfinished_case(self):
        """直接在库里留下 queued 案件，新 CaseProcessor 启动后应接手完成。"""
        j1 = self.create_completed_job({"reply.eml": REPLY_EML})
        j2 = self.create_completed_job({"root.eml": ROOT_EML})

        case_id = str(uuid.uuid4())
        Storage.prepare_case_dir(case_id)
        # 不经过 HTTP 层入队，模拟服务崩溃时留下的未完成任务
        self.server.storage.create_case(case_id, "恢复测试", [j1, j2])

        # 模拟重启：新的处理器启动时扫描未完成任务
        recovery = CaseProcessor(self.server.storage)
        recovery.start()

        deadline = time.time() + 10
        while time.time() < deadline:
            case = self.server.storage.get_case(case_id)
            if case["status"] in (
                config.STATUS_COMPLETED,
                config.STATUS_FAILED,
            ):
                break
            time.sleep(0.05)
        self.assertEqual(case["status"], config.STATUS_COMPLETED, case)
        self.assertEqual(case["stats"]["relinked_nodes"], 1)

        # 恢复完成的结果可通过 HTTP 读取
        status, _, body = self.client.request(
            "GET", f"/api/v1/cases/{case_id}/tree"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(json.loads(body)["threads"]), 1)

    def test_12_case_fails_when_source_deleted_before_processing(self):
        """源作业在案件处理前被删除：案件失败并说明原因。"""
        j1 = self.create_completed_job({"root.eml": ROOT_EML})
        case_id = str(uuid.uuid4())
        Storage.prepare_case_dir(case_id)
        self.server.storage.create_case(case_id, None, [j1])
        # 在处理器接手前删除源作业
        status, _, _ = self.client.request("DELETE", f"/api/v1/jobs/{j1}")
        self.assertEqual(status, 200)

        recovery = CaseProcessor(self.server.storage)
        recovery.start()
        deadline = time.time() + 10
        case = None
        while time.time() < deadline:
            case = self.server.storage.get_case(case_id)
            if case["status"] in (
                config.STATUS_COMPLETED,
                config.STATUS_FAILED,
            ):
                break
            time.sleep(0.05)
        self.assertEqual(case["status"], config.STATUS_FAILED, case)
        self.assertIn("源作业不存在", case["error"])


if __name__ == "__main__":
    unittest.main()
