"""HTTP API 端到端测试：进程内启动服务，urllib 发请求。"""

from __future__ import annotations

import hashlib
import io
import json
import time
import unittest
import urllib.error
import urllib.request
import uuid
import zipfile

import tests.support  # noqa: F401
from mailrecon import config
from mailrecon import jsonio
from mailrecon.server import ApiServer


def make_zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def multipart_body(filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----mailrecontest" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


SAMPLE_ZIP = make_zip_bytes(
    {
        "a.eml": (
            b"Message-ID: <a@x>\r\nFrom: A <a@x>\r\nTo: B <b@x>\r\n"
            b"Subject: root\r\nDate: Mon, 01 Sep 2026 09:00:00 +0000\r\n\r\nroot body"
        ),
        "b.eml": (
            b"Message-ID: <b@x>\r\nFrom: B <b@x>\r\nTo: A <a@x>\r\n"
            b"Subject: Re: root\r\nDate: Mon, 01 Sep 2026 10:00:00 +0000\r\n"
            b"In-Reply-To: <a@x>\r\nReferences: <a@x>\r\n\r\nreply body"
        ),
        "notes.txt": b"not an email",
    }
)

EVIL_TRAVERSAL_ZIP = make_zip_bytes({"../../tmp/x.eml": b"evil"})


class ApiClient:
    def __init__(self, base: str) -> None:
        self.base = base

    def request(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ):
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def create_job(
        self, content: bytes, filename="pkg.zip", idem: str | None = None,
        raw_zip: bool = False,
    ):
        headers = {}
        if idem:
            headers["Idempotency-Key"] = idem
        if raw_zip:
            headers["Content-Type"] = "application/zip"
            headers["X-Filename"] = filename
            data = content
        else:
            data, ctype = multipart_body(filename, content)
            headers["Content-Type"] = ctype
        return self.request("POST", "/api/v1/jobs", data, headers)

    def wait_for(self, job_id: str, timeout: float = 10.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _, body = self.request("GET", f"/api/v1/jobs/{job_id}")
            job = json.loads(body)
            if job["status"] in (config.STATUS_COMPLETED, config.STATUS_FAILED):
                return job
            time.sleep(0.05)
        raise AssertionError("作业处理超时")


class HttpApiTest(unittest.TestCase):
    server: ApiServer
    client: ApiClient

    @classmethod
    def setUpClass(cls) -> None:
        # 端口 0 = 由操作系统分配空闲端口
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

    def test_01_health(self):
        status, _, body = self.client.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_02_create_and_process_job_multipart(self):
        status, headers, body = self.client.create_job(SAMPLE_ZIP, "mails.zip")
        self.assertEqual(status, 201, body)
        job = json.loads(body)
        self.assertEqual(job["original_filename"], "mails.zip")
        self.assertEqual(job["status"], config.STATUS_QUEUED)

        done = self.client.wait_for(job["id"])
        self.assertEqual(done["status"], config.STATUS_COMPLETED, done)
        self.assertEqual(done["email_count"], 2)
        self.assertEqual(done["thread_count"], 1)
        self.assertEqual(done["stats"]["non_eml_skipped"], 1)

    def test_03_raw_zip_upload(self):
        status, _, body = self.client.create_job(SAMPLE_ZIP, raw_zip=True)
        self.assertEqual(status, 201, body)
        job = json.loads(body)
        done = self.client.wait_for(job["id"])
        self.assertEqual(done["status"], config.STATUS_COMPLETED)

    def test_04_tree_endpoint(self):
        _, _, body = self.client.create_job(SAMPLE_ZIP)
        job = json.loads(body)
        self.client.wait_for(job["id"])

        status, _, body = self.client.request(
            "GET", f"/api/v1/jobs/{job['id']}/tree"
        )
        self.assertEqual(status, 200)
        tree = json.loads(body)
        self.assertEqual(len(tree["threads"]), 1)
        root = tree["threads"][0]
        self.assertEqual(root["message_id"], "<a@x>")
        self.assertIn("root body", root["body_text"])
        child = root["children"][0]
        self.assertEqual(child["message_id"], "<b@x>")
        self.assertIn("reply body", child["body_text"])
        self.assertEqual(child["from"]["address"], "b@x")

        # compact 视图
        status, _, body = self.client.request(
            "GET", f"/api/v1/jobs/{job['id']}/tree?view=compact"
        )
        compact = json.loads(body)
        self.assertNotIn("body_text", compact["threads"][0])
        self.assertIn("issue_count", compact["threads"][0])

    def test_05_result_download_is_json_attachment(self):
        _, _, body = self.client.create_job(SAMPLE_ZIP)
        job = json.loads(body)
        self.client.wait_for(job["id"])

        status, headers, body = self.client.request(
            "GET", f"/api/v1/jobs/{job['id']}/result"
        )
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        result = json.loads(body)
        self.assertEqual(result["job_id"], job["id"])
        self.assertIn("threads", result)
        # 附件只有元数据
        for root in result["threads"]:
            for att in root["attachments"]:
                self.assertEqual(
                    set(att.keys()),
                    {"filename", "content_type", "size", "sha256"},
                )

    def test_06_tree_before_ready_returns_409(self):
        _, _, body = self.client.create_job(SAMPLE_ZIP)
        job = json.loads(body)
        # 立刻查询（极快的机器可能已处理完，因此仅在 queued/processing 时断言）
        status, _, body = self.client.request(
            "GET", f"/api/v1/jobs/{job['id']}"
        )
        current = json.loads(body)
        if current["status"] != config.STATUS_COMPLETED:
            status, _, _ = self.client.request(
                "GET", f"/api/v1/jobs/{job['id']}/tree"
            )
            self.assertEqual(status, 409)
        self.client.wait_for(job["id"])

    def test_07_idempotency_same_key_same_content(self):
        key = "idem-" + uuid.uuid4().hex
        s1, _, b1 = self.client.create_job(SAMPLE_ZIP, idem=key)
        self.assertEqual(s1, 201)
        s2, _, b2 = self.client.create_job(SAMPLE_ZIP, idem=key)
        self.assertEqual(s2, 200)
        j1, j2 = json.loads(b1), json.loads(b2)
        self.assertEqual(j1["id"], j2["id"])
        self.assertTrue(j2["idempotent_replayed"])

    def test_08_idempotency_same_key_different_content_conflicts(self):
        key = "idem-conflict-" + uuid.uuid4().hex
        s1, _, _ = self.client.create_job(SAMPLE_ZIP, idem=key)
        self.assertEqual(s1, 201)
        other = make_zip_bytes({"x.eml": b"Message-ID: <z@z>\r\n\r\nother"})
        s2, _, b2 = self.client.create_job(other, idem=key)
        self.assertEqual(s2, 409)
        self.assertEqual(json.loads(b2)["error"]["code"], "idempotency_conflict")

    def test_09_invalid_idempotency_key(self):
        status, _, body = self.client.create_job(SAMPLE_ZIP, idem="a b")
        self.assertEqual(status, 400)

    def test_10_traversal_zip_rejected(self):
        status, _, body = self.client.create_job(EVIL_TRAVERSAL_ZIP)
        self.assertEqual(status, 201)  # 上传受理
        job = json.loads(body)
        done = self.client.wait_for(job["id"])
        self.assertEqual(done["status"], config.STATUS_FAILED)
        self.assertIn("路径穿越", done["error"])

    def test_11_not_a_zip_rejected(self):
        status, _, body = self.client.create_job(b"not a zip at all")
        self.assertEqual(status, 201)
        job = json.loads(body)
        done = self.client.wait_for(job["id"])
        self.assertEqual(done["status"], config.STATUS_FAILED)
        self.assertIn("ZIP", done["error"])

    def test_12_unsupported_media_type(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/v1/jobs",
            data=b'{"x":1}',
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req)
            self.fail("应当 415")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 415)

    def test_13_empty_body(self):
        status, _, _ = self.client.request(
            "POST",
            "/api/v1/jobs",
            b"",
            {"Content-Type": "multipart/form-data; boundary=xx"},
        )
        self.assertEqual(status, 400)

    def test_14_missing_job_and_bad_id(self):
        status, _, _ = self.client.request(
            "GET", "/api/v1/jobs/does-not-exist"
        )
        self.assertEqual(status, 400)  # 格式非法

        fake_id = "00000000-0000-0000-0000-000000000000"
        status, _, body = self.client.request(
            "GET", f"/api/v1/jobs/{fake_id}"
        )
        self.assertEqual(status, 404)

    def test_15_delete_removes_files(self):
        _, _, body = self.client.create_job(SAMPLE_ZIP)
        job = json.loads(body)
        self.client.wait_for(job["id"])

        from mailrecon.storage import Storage

        job_dir = Storage.job_dir(job["id"])
        self.assertTrue(job_dir.exists())
        status, _, body = self.client.request(
            "DELETE", f"/api/v1/jobs/{job['id']}"
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["deleted"])
        self.assertFalse(job_dir.exists())

        status, _, _ = self.client.request("GET", f"/api/v1/jobs/{job['id']}")
        self.assertEqual(status, 404)

    def test_16_list_jobs(self):
        status, _, body = self.client.request("GET", "/api/v1/jobs")
        self.assertEqual(status, 200)
        self.assertIn("jobs", json.loads(body))

    def test_17_original_filename_is_sanitized(self):
        status, _, body = self.client.create_job(
            SAMPLE_ZIP, filename="../../evil.zip"
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["original_filename"], "evil.zip")

    def test_18_deep_chain_job_completes_and_tree_downloads(self):
        """1100 封邮件的单链作业（超过默认递归深度 1000）。

        回归 build_threads / 结果 JSON 序列化 / tree 与 result 接口在
        深引用链上的 RecursionError。
        """
        depth = 1100
        members: dict[str, bytes] = {}
        mids = [f"<deep{i:05d}@x>" for i in range(depth)]

        def eml(i: int) -> bytes:
            lines = [
                f"Message-ID: {mids[i]}",
                "From: A <a@x>",
                "To: B <b@x>",
                f"Subject: deep chain {i}",
                f"Date: Mon, 01 Sep 2026 09:{i % 60:02d}:00 +0000",
            ]
            if i > 0:
                lines.append(f"In-Reply-To: {mids[i - 1]}")
                lines.append(f"References: {mids[i - 1]}")
            lines.append("")
            lines.append(f"body {i}")
            return ("\r\n".join(lines)).encode("utf-8")

        # 逆序放入压缩包，强制解析时沿引用链一路下探
        for i in range(depth - 1, -1, -1):
            members[f"mails/{i:05d}.eml"] = eml(i)

        zip_bytes = make_zip_bytes(members)
        status, _, body = self.client.create_job(
            zip_bytes, filename="deep.zip",
            idem="deep-" + uuid.uuid4().hex,
        )
        self.assertEqual(status, 201, body)
        job = json.loads(body)

        done = self.client.wait_for(job["id"], timeout=30)
        self.assertEqual(done["status"], config.STATUS_COMPLETED, done)
        self.assertEqual(done["email_count"], depth)
        self.assertEqual(done["thread_count"], 1)
        self.assertEqual(
            done["stats"]["issues_by_type"]["reference_cycle"], 0
        )
        self.assertEqual(
            done["stats"]["issues_by_type"]["missing_parent"], 0
        )

        # /tree：沿 children 迭代（测试自身也不递归）走完整条链
        status, headers, body = self.client.request(
            "GET", f"/api/v1/jobs/{job['id']}/tree"
        )
        self.assertEqual(status, 200)
        tree = jsonio.loads(body)
        self.assertEqual(len(tree["threads"]), 1)
        current = tree["threads"][0]
        count = 0
        while True:
            count += 1
            if current["children"]:
                self.assertEqual(len(current["children"]), 1)
                current = current["children"][0]
            else:
                break
        self.assertEqual(count, depth)
        self.assertEqual(current["message_id"], mids[-1])
        self.assertIn(f"body {depth - 1}", current["body_text"])

        # compact 视图同样可用
        status, _, body = self.client.request(
            "GET", f"/api/v1/jobs/{job['id']}/tree?view=compact"
        )
        self.assertEqual(status, 200)
        compact = jsonio.loads(body)
        current = compact["threads"][0]
        count = 0
        while current["children"]:
            self.assertEqual(len(current["children"]), 1)
            current = current["children"][0]
            count += 1
        self.assertEqual(count, depth - 1)

        # /result：下载 JSON 附件并可解析
        status, headers, body = self.client.request(
            "GET", f"/api/v1/jobs/{job['id']}/result"
        )
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        result = jsonio.loads(body)
        self.assertEqual(result["job_id"], job["id"])
        self.assertEqual(result["stats"]["eml_parsed"], depth)
        current = result["threads"][0]
        count = 0
        while current["children"]:
            current = current["children"][0]
            count += 1
        self.assertEqual(count, depth - 1)


class UploadLimitTest(unittest.TestCase):
    """MAX_UPLOAD_BYTES 在 HTTP 层生效。"""

    def test_upload_size_limit(self):
        server = ApiServer("127.0.0.1", 0)
        port = server.server_address[1]
        import threading

        server.start()
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            original = config.MAX_UPLOAD_BYTES
            object.__setattr__(config, "MAX_UPLOAD_BYTES", 100)
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/v1/jobs",
                    data=b"x" * 200,
                    method="POST",
                    headers={"Content-Type": "application/zip"},
                )
                try:
                    urllib.request.urlopen(req)
                    self.fail("应被 413 拒绝")
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code, 413)
            finally:
                object.__setattr__(config, "MAX_UPLOAD_BYTES", original)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
