"""HTTP API 层：基于标准库 http.server，无任何外部依赖。"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import attachflow, config, identity, jsonio, recipientflow, timing
from .attachproc import AttachmentFlowProcessor
from .caseproc import CaseProcessor
from .identityproc import IdentityProcessor
from .processor import JobProcessor
from .recflowproc import RecipientFlowProcessor
from .storage import Storage
from .timeproc import TimingProcessor

_UUID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
_IDEMPOTENCY_RE = re.compile(r"^[\x21-\x7E]{8,200}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# 超过该嵌套深度的响应放弃缩进，改用紧凑 JSON（避免 O(深度²) 输出）
_PRETTY_MAX_DEPTH = 500


def _json_depth(value: Any) -> int:
    """迭代计算 dict/list 的最大嵌套深度（dict/list 各算一层）。"""
    max_depth = 0
    # (当前容器, 已展开标记)；用栈模拟递归
    stack: list[Any] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            depth += 1
            max_depth = max(max_depth, depth)
            for v in item.values():
                if isinstance(v, (dict, list)):
                    stack.append((v, depth))
        elif isinstance(item, list):
            depth += 1
            max_depth = max(max_depth, depth)
            for v in item:
                if isinstance(v, (dict, list)):
                    stack.append((v, depth))
    return max_depth


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ---------------------------------------------------------------- multipart

def parse_multipart(body: bytes, content_type: str) -> dict[str, dict[str, Any]]:
    """解析 multipart/form-data，返回 ``{字段名: {headers, data}}``。

    只实现本服务需要的子集：二进制文件字段 + 短文本字段。
    """
    m = re.search(r"boundary=([^;]+)", content_type)
    if not m:
        raise ApiError(400, "bad_request", "multipart 请求缺少 boundary")
    boundary = m.group(1).strip().strip('"')
    delim = b"--" + boundary.encode("latin-1")

    fields: dict[str, dict[str, Any]] = {}
    # 用原始字节切分，避免对二进制内容做任何解码
    parts = body.split(delim)
    # 首段为前导 (--boundary 之前)，末段为 "--"，中间为各字段
    for part in parts[1:-1]:
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.strip() in (b"--", b""):
            continue
        if part.endswith(b"\r\n"):
            part = part[:-2]
        header_blob, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            raise ApiError(400, "bad_request", "multipart 字段缺少头部分隔")
        headers: dict[str, str] = {}
        for line in header_blob.decode("latin-1").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        name_match = re.search(r'name="([^"]*)"', disp)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', disp)
        fields[name] = {
            "filename": filename_match.group(1) if filename_match else None,
            "content_type": headers.get("content-type"),
            "data": data,
        }
    return fields


# ---------------------------------------------------------------- 处理器

class Handler(BaseHTTPRequestHandler):
    server_version = "mailrecon/1.0"
    protocol_version = "HTTP/1.1"

    # 方便类型提示；实际在 ApiServer 上注入
    storage: Storage
    processor: JobProcessor
    case_processor: CaseProcessor
    timing_processor: TimingProcessor
    identity_processor: IdentityProcessor
    flow_processor: AttachmentFlowProcessor
    recipient_flow_processor: RecipientFlowProcessor
    create_lock: threading.Lock

    def log_message(self, fmt: str, *args: Any) -> None:
        # 简洁的单行访问日志
        import sys

        sys.stderr.write(
            "%s - - %s\n" % (self.address_string(), fmt % args)
        )

    # ------------------------------------------------------ 响应工具

    def _json(self, status: int, payload: Any) -> None:
        # 深嵌套（典型为超长引用链）时，缩进 JSON 的闭合括号缩进会产生
        # O(深度²) 体积，超过阈值自动降级为紧凑输出。
        indent = 2 if _json_depth(payload) <= _PRETTY_MAX_DEPTH else None
        data = jsonio.dumps(payload, ensure_ascii=False, indent=indent).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    def _file_download(self, path: Path, download_name: str) -> None:
        if not path.is_file():
            self._error(404, "not_found", "结果文件不存在")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{download_name}"',
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body_capped(self) -> bytes:
        length_hdr = self.headers.get("Content-Length")
        if length_hdr is None:
            raise ApiError(411, "length_required", "必须提供 Content-Length")
        try:
            length = int(length_hdr)
        except ValueError:
            raise ApiError(400, "bad_request", "Content-Length 非法")
        if length > config.MAX_UPLOAD_BYTES:
            raise ApiError(
                413,
                "payload_too_large",
                f"上传体积 {length} 字节超过上限 "
                f"{config.MAX_UPLOAD_BYTES} 字节",
            )
        return self.rfile.read(length)

    # ------------------------------------------------------ 路由

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 约定)
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query, keep_blank_values=True)

            if path == "/health":
                self._json(200, {"status": "ok", "version": "1.0.0"})
            elif path == "/api/v1/jobs":
                self._list_jobs()
            elif path == "/api/v1/analyses":
                self._list_analyses()
            elif path == "/api/v1/identity-checks":
                self._list_identity_checks()
            elif path == "/api/v1/attachment-flows":
                self._list_attachment_flows()
            elif path == "/api/v1/recipient-flows":
                self._list_recipient_flows()
            else:
                match = re.fullmatch(r"/api/v1/jobs/([^/]+)", path)
                if match:
                    self._get_job(match.group(1))
                    return
                match = re.fullmatch(r"/api/v1/jobs/([^/]+)/tree", path)
                if match:
                    self._get_tree(match.group(1), query)
                    return
                match = re.fullmatch(r"/api/v1/jobs/([^/]+)/result", path)
                if match:
                    self._get_result(match.group(1))
                    return
                match = re.fullmatch(r"/api/v1/cases/([^/]+)", path)
                if match:
                    self._get_case(match.group(1))
                    return
                match = re.fullmatch(r"/api/v1/cases/([^/]+)/tree", path)
                if match:
                    self._get_case_tree(match.group(1), query)
                    return
                match = re.fullmatch(r"/api/v1/cases/([^/]+)/result", path)
                if match:
                    self._get_case_result(match.group(1))
                    return
                match = re.fullmatch(r"/api/v1/analyses/([^/]+)", path)
                if match:
                    self._get_analysis(match.group(1))
                    return
                match = re.fullmatch(
                    r"/api/v1/analyses/([^/]+)/timeline", path
                )
                if match:
                    self._get_analysis_timeline(match.group(1), query)
                    return
                match = re.fullmatch(
                    r"/api/v1/analyses/([^/]+)/result", path
                )
                if match:
                    self._get_analysis_result(match.group(1))
                    return
                match = re.fullmatch(
                    r"/api/v1/identity-checks/([^/]+)", path
                )
                if match:
                    self._get_identity_check(match.group(1))
                    return
                match = re.fullmatch(
                    r"/api/v1/identity-checks/([^/]+)/findings", path
                )
                if match:
                    self._get_identity_findings(match.group(1), query)
                    return
                match = re.fullmatch(
                    r"/api/v1/identity-checks/([^/]+)/emails", path
                )
                if match:
                    self._get_identity_emails(match.group(1), query)
                    return
                match = re.fullmatch(
                    r"/api/v1/identity-checks/([^/]+)/threads", path
                )
                if match:
                    self._get_identity_threads(match.group(1), query)
                    return
                match = re.fullmatch(
                    r"/api/v1/identity-checks/([^/]+)/result", path
                )
                if match:
                    self._get_identity_result(match.group(1))
                    return
                match = re.fullmatch(
                    r"/api/v1/attachment-flows/([^/]+)", path
                )
                if match:
                    self._get_attachment_flow(match.group(1))
                    return
                match = re.fullmatch(
                    r"/api/v1/attachment-flows/([^/]+)/events", path
                )
                if match:
                    self._get_flow_events(match.group(1), query)
                    return
                match = re.fullmatch(
                    r"/api/v1/attachment-flows/([^/]+)/attachments", path
                )
                if match:
                    self._get_flow_attachments(match.group(1), query)
                    return
                match = re.fullmatch(
                    r"/api/v1/attachment-flows/([^/]+)/result", path
                )
                if match:
                    self._get_flow_result(match.group(1))
                    return
                match = re.fullmatch(
                    r"/api/v1/recipient-flows/([^/]+)", path
                )
                if match:
                    self._get_recipient_flow(match.group(1))
                    return
                match = re.fullmatch(
                    r"/api/v1/recipient-flows/([^/]+)/events", path
                )
                if match:
                    self._get_recipient_flow_events(match.group(1), query)
                    return
                match = re.fullmatch(
                    r"/api/v1/recipient-flows/([^/]+)/threads", path
                )
                if match:
                    self._get_recipient_flow_threads(match.group(1), query)
                    return
                match = re.fullmatch(
                    r"/api/v1/recipient-flows/([^/]+)/addresses", path
                )
                if match:
                    self._get_recipient_flow_addresses(match.group(1), query)
                    return
                match = re.fullmatch(
                    r"/api/v1/recipient-flows/([^/]+)/result", path
                )
                if match:
                    self._get_recipient_flow_result(match.group(1))
                    return
                self._error(404, "not_found", f"未知路径: {self.path}")
        except ApiError as exc:
            self._error(exc.status, exc.code, exc.message)
        except Exception as exc:  # 兜底
            self._error(500, "internal_error", f"内部错误: {exc}")

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/api/v1/jobs":
                self._create_job()
            elif path == "/api/v1/cases":
                self._create_case()
            elif path == "/api/v1/analyses":
                self._create_analysis()
            elif path == "/api/v1/identity-checks":
                self._create_identity_check()
            elif path == "/api/v1/attachment-flows":
                self._create_attachment_flow()
            elif path == "/api/v1/recipient-flows":
                self._create_recipient_flow()
            else:
                self._error(404, "not_found", f"未知路径: {self.path}")
        except ApiError as exc:
            self._error(exc.status, exc.code, exc.message)
        except Exception as exc:
            self._error(500, "internal_error", f"内部错误: {exc}")

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            match = re.fullmatch(r"/api/v1/jobs/([^/]+)", path)
            if not match:
                self._error(404, "not_found", f"未知路径: {self.path}")
                return
            self._delete_job(match.group(1))
        except ApiError as exc:
            self._error(exc.status, exc.code, exc.message)
        except Exception as exc:
            self._error(500, "internal_error", f"内部错误: {exc}")

    # ------------------------------------------------------ 端点实现

    def _public_job(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": job["id"],
            "status": job["status"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "original_filename": job["original_filename"],
            "idempotency_key": job["idempotency_key"],
            "progress": job["progress"],
            "phase": job["phase"],
            "error": job["error"],
            "email_count": job["email_count"],
            "thread_count": job["thread_count"],
            "stats": job["stats"],
        }

    def _require_job(self, job_id: str) -> dict[str, Any]:
        if not _UUID_RE.fullmatch(job_id):
            raise ApiError(400, "bad_request", "作业 ID 格式非法")
        job = self.storage.get_job(job_id)
        if job is None:
            raise ApiError(404, "not_found", f"作业不存在: {job_id}")
        return job

    def _list_jobs(self) -> None:
        jobs = self.storage.list_jobs()
        self._json(200, {"jobs": [self._public_job(j) for j in jobs]})

    def _get_job(self, job_id: str) -> None:
        self._json(200, self._public_job(self._require_job(job_id)))

    def _get_tree(self, job_id: str, query: dict[str, list[str]]) -> None:
        job = self._require_job(job_id)
        if job["status"] != config.STATUS_COMPLETED or not job["result_path"]:
            raise ApiError(
                409,
                "not_ready",
                f"作业尚未完成 (当前状态: {job['status']})",
            )
        result = jsonio.loads(Path(job["result_path"]).read_text("utf-8"))
        payload: dict[str, Any] = {
            "job_id": job["id"],
            "stats": result["stats"],
            "skipped_entries": result["skipped_entries"],
            "threads": result["threads"],
        }
        if query.get("view") == ["compact"]:
            payload["threads"] = _compact_forest(result["threads"])
        self._json(200, payload)

    def _get_result(self, job_id: str) -> None:
        job = self._require_job(job_id)
        if job["status"] != config.STATUS_COMPLETED or not job["result_path"]:
            raise ApiError(
                409,
                "not_ready",
                f"作业尚未完成 (当前状态: {job['status']})",
            )
        download_name = f"{job['id']}.result.json"
        self._file_download(Path(job["result_path"]), download_name)

    def _delete_job(self, job_id: str) -> None:
        job = self._require_job(job_id)
        if job["status"] == config.STATUS_PROCESSING:
            raise ApiError(
                409,
                "job_processing",
                "作业正在处理中，暂时无法删除（可先等待其完成或失败）",
            )
        Storage.cleanup_job_files(job_id)
        self.storage.delete_job(job_id)
        self._json(200, {"id": job_id, "deleted": True})

    def _create_job(self) -> None:
        idem_key = self.headers.get("Idempotency-Key", "").strip() or None
        if idem_key is not None and not _IDEMPOTENCY_RE.fullmatch(idem_key):
            raise ApiError(
                400,
                "bad_request",
                "Idempotency-Key 须为 8-200 个可打印 ASCII 字符",
            )

        content_type = self.headers.get("Content-Type", "")
        body = self._read_body_capped()
        if not body:
            raise ApiError(400, "bad_request", "请求体为空")

        if content_type.lower().startswith("multipart/form-data"):
            fields = parse_multipart(body, content_type)
            file_field = fields.get("file")
            if file_field is None or not file_field["data"]:
                raise ApiError(
                    400, "bad_request", "multipart 请求中缺少非空 file 字段"
                )
            file_data = file_field["data"]
            original_filename = _basename(file_field["filename"])
        elif content_type.lower().split(";")[0].strip() in (
            "application/zip",
            "application/x-zip-compressed",
            "application/octet-stream",
        ):
            file_data = body
            original_filename = _basename(
                self.headers.get("X-Filename") or "package.zip"
            )
        else:
            raise ApiError(
                415,
                "unsupported_media_type",
                "请使用 multipart/form-data 上传 file 字段，"
                "或直接以 application/zip 发送原始 ZIP 字节",
            )

        if len(file_data) > config.MAX_UPLOAD_BYTES:
            raise ApiError(
                413, "payload_too_large", "ZIP 体积超过上传上限"
            )
        token = hashlib.sha256(file_data).hexdigest()

        with self.create_lock:
            if idem_key:
                existing = self.storage.get_job_by_idempotency(idem_key)
                if existing is not None:
                    if existing["idempotency_token"] != token:
                        raise ApiError(
                            409,
                            "idempotency_conflict",
                            "同一 Idempotency-Key 已用于内容不同的上传，"
                            "请更换 Key 后重试",
                        )
                    self._json(
                        200,
                        {
                            **self._public_job(existing),
                            "idempotent_replayed": True,
                        },
                    )
                    return

            job_id = str(uuid.uuid4())
            job_dir = Storage.prepare_job_dir(job_id)
            upload_path = job_dir / "upload.bin"
            upload_path.write_bytes(file_data)
            self.storage.create_job(
                job_id=job_id,
                original_filename=original_filename,
                idempotency_key=idem_key,
                idempotency_token=token,
                upload_path=str(upload_path),
            )

        self.processor.enqueue(job_id)
        job = self.storage.get_job(job_id)
        self._json(201, self._public_job(job))

    # ------------------------------------------------------ 案件端点

    def _public_case(self, case: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": case["id"],
            "name": case["name"],
            "status": case["status"],
            "created_at": case["created_at"],
            "updated_at": case["updated_at"],
            "job_ids": case["job_ids"],
            "progress": case["progress"],
            "phase": case["phase"],
            "error": case["error"],
            "email_count": case["email_count"],
            "thread_count": case["thread_count"],
            "stats": case["stats"],
        }

    def _require_case(self, case_id: str) -> dict[str, Any]:
        if not _UUID_RE.fullmatch(case_id):
            raise ApiError(400, "bad_request", "案件 ID 格式非法")
        case = self.storage.get_case(case_id)
        if case is None:
            raise ApiError(404, "not_found", f"案件不存在: {case_id}")
        return case

    def _create_case(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if content_type.lower().split(";")[0].strip() != "application/json":
            raise ApiError(
                415,
                "unsupported_media_type",
                "创建案件请使用 application/json 请求体",
            )
        body = self._read_body_capped()
        if not body:
            raise ApiError(400, "bad_request", "请求体为空")
        try:
            payload = jsonio.loads(body)
        except Exception:
            raise ApiError(400, "bad_request", "请求体不是合法 JSON")
        if not isinstance(payload, dict):
            raise ApiError(400, "bad_request", "请求体必须是 JSON 对象")

        name = payload.get("name")
        if name is not None:
            if not isinstance(name, str):
                raise ApiError(400, "bad_request", "name 必须是字符串")
            name = name.strip()[:200] or None

        job_ids = payload.get("job_ids")
        if not isinstance(job_ids, list) or not job_ids:
            raise ApiError(400, "bad_request", "job_ids 必须是非空数组")
        if len(job_ids) > config.MAX_CASE_JOBS:
            raise ApiError(
                400,
                "bad_request",
                f"单个案件最多合并 {config.MAX_CASE_JOBS} 个作业，"
                f"实际提交 {len(job_ids)} 个",
            )
        for job_id in job_ids:
            if not isinstance(job_id, str) or not _UUID_RE.fullmatch(job_id):
                raise ApiError(
                    400, "bad_request", f"作业 ID 格式非法: {job_id!r}"
                )
        seen: set[str] = set()
        duplicated: list[str] = []
        for job_id in job_ids:
            if job_id in seen and job_id not in duplicated:
                duplicated.append(job_id)
            seen.add(job_id)
        if duplicated:
            raise ApiError(
                409,
                "duplicate_job",
                "同一作业在同一案件中重复提交: " + ", ".join(duplicated),
            )

        with self.create_lock:
            total_emails = 0
            for job_id in job_ids:
                job = self.storage.get_job(job_id)
                if job is None:
                    raise ApiError(
                        404,
                        "job_not_found",
                        f"源作业不存在或已被删除: {job_id}",
                    )
                if job["status"] != config.STATUS_COMPLETED:
                    raise ApiError(
                        409,
                        "job_not_completed",
                        f"源作业未完成，不能加入案件: {job_id} "
                        f"(当前状态: {job['status']})",
                    )
                total_emails += job["email_count"]
            if total_emails > config.MAX_CASE_EMAILS:
                raise ApiError(
                    409,
                    "case_too_large",
                    f"案件邮件总量 {total_emails} 超过上限 "
                    f"{config.MAX_CASE_EMAILS}",
                )
            case_id = str(uuid.uuid4())
            Storage.prepare_case_dir(case_id)
            self.storage.create_case(case_id, name, job_ids)

        self.case_processor.enqueue(case_id)
        self._json(201, self._public_case(self.storage.get_case(case_id)))

    def _get_case(self, case_id: str) -> None:
        self._json(200, self._public_case(self._require_case(case_id)))

    def _get_case_tree(
        self, case_id: str, query: dict[str, list[str]]
    ) -> None:
        case = self._require_case(case_id)
        if case["status"] != config.STATUS_COMPLETED or not case["result_path"]:
            raise ApiError(
                409,
                "not_ready",
                f"案件尚未完成 (当前状态: {case['status']})",
            )
        result = jsonio.loads(Path(case["result_path"]).read_text("utf-8"))
        payload: dict[str, Any] = {
            "case_id": case["id"],
            "name": case["name"],
            "source_jobs": result["source_jobs"],
            "stats": result["stats"],
            "threads": result["threads"],
        }
        if query.get("view") == ["compact"]:
            payload["threads"] = _compact_forest(
                result["threads"], _compact_case_node
            )
        self._json(200, payload)

    def _get_case_result(self, case_id: str) -> None:
        case = self._require_case(case_id)
        if case["status"] != config.STATUS_COMPLETED or not case["result_path"]:
            raise ApiError(
                409,
                "not_ready",
                f"案件尚未完成 (当前状态: {case['status']})",
            )
        download_name = f"{case['id']}.case-result.json"
        self._file_download(Path(case["result_path"]), download_name)

    # ------------------------------------------------------ 时序核验分析端点

    def _public_analysis(self, analysis: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": analysis["id"],
            "status": analysis["status"],
            "created_at": analysis["created_at"],
            "updated_at": analysis["updated_at"],
            "target_type": analysis["target_type"],
            "target_id": analysis["target_id"],
            "thresholds": analysis["thresholds"],
            "progress": analysis["progress"],
            "phase": analysis["phase"],
            "error": analysis["error"],
            "email_count": analysis["email_count"],
            "finding_count": analysis["finding_count"],
            "stats": analysis["stats"],
        }

    def _require_analysis(self, analysis_id: str) -> dict[str, Any]:
        if not _UUID_RE.fullmatch(analysis_id):
            raise ApiError(400, "bad_request", "分析 ID 格式非法")
        analysis = self.storage.get_analysis(analysis_id)
        if analysis is None:
            raise ApiError(404, "not_found", f"分析不存在: {analysis_id}")
        return analysis

    def _list_analyses(self) -> None:
        analyses = self.storage.list_analyses()
        self._json(200, {"analyses": [self._public_analysis(a) for a in analyses]})

    def _create_analysis(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if content_type.lower().split(";")[0].strip() != "application/json":
            raise ApiError(
                415,
                "unsupported_media_type",
                "创建分析请使用 application/json 请求体",
            )
        body = self._read_body_capped()
        if not body:
            raise ApiError(400, "bad_request", "请求体为空")
        try:
            payload = jsonio.loads(body)
        except Exception:
            raise ApiError(400, "bad_request", "请求体不是合法 JSON")
        if not isinstance(payload, dict):
            raise ApiError(400, "bad_request", "请求体必须是 JSON 对象")

        target_type = payload.get("target_type")
        if target_type not in ("job", "case"):
            raise ApiError(
                400, "bad_request", "target_type 必须是 \"job\" 或 \"case\""
            )
        target_id = payload.get("target_id")
        if not isinstance(target_id, str) or not _UUID_RE.fullmatch(target_id):
            raise ApiError(400, "bad_request", "target_id 必须是作业/案件 UUID")

        thresholds = _parse_thresholds(payload.get("thresholds"))

        with self.create_lock:
            if target_type == "job":
                target = self.storage.get_job(target_id)
                label = "作业"
            else:
                target = self.storage.get_case(target_id)
                label = "案件"
            if target is None:
                raise ApiError(
                    404,
                    "target_not_found",
                    f"目标{label}不存在或已被删除: {target_id}",
                )
            if target["status"] != config.STATUS_COMPLETED:
                raise ApiError(
                    409,
                    "target_not_completed",
                    f"目标{label}未完成，不能创建时序核验分析: {target_id} "
                    f"(当前状态: {target['status']})",
                )
            analysis_id = str(uuid.uuid4())
            Storage.prepare_analysis_dir(analysis_id)
            self.storage.create_analysis(
                analysis_id, target_type, target_id, thresholds
            )

        self.timing_processor.enqueue(analysis_id)
        analysis = self.storage.get_analysis(analysis_id)
        self._json(201, self._public_analysis(analysis))

    def _get_analysis(self, analysis_id: str) -> None:
        self._json(200, self._public_analysis(self._require_analysis(analysis_id)))

    def _completed_analysis_result(
        self, analysis_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        analysis = self._require_analysis(analysis_id)
        if (
            analysis["status"] != config.STATUS_COMPLETED
            or not analysis["result_path"]
        ):
            raise ApiError(
                409,
                "not_ready",
                f"分析尚未完成 (当前状态: {analysis['status']})",
            )
        result = jsonio.loads(Path(analysis["result_path"]).read_text("utf-8"))
        return analysis, result

    def _get_analysis_timeline(
        self, analysis_id: str, query: dict[str, list[str]]
    ) -> None:
        analysis, result = self._completed_analysis_result(analysis_id)

        # ---- 筛选参数解析（非法输入明确拒绝，不静默忽略） ----------------
        time_from = _parse_time_filter(query.get("from"), "from")
        time_to = _parse_time_filter(query.get("to"), "to")
        type_filter = None
        raw_types = query.get("type")
        if raw_types:
            type_filter = raw_types[0]
            if type_filter not in timing.FINDING_TYPES:
                raise ApiError(
                    400,
                    "bad_request",
                    f"未知异常类型: {type_filter!r}，可选: "
                    + ", ".join(timing.FINDING_TYPES),
                )

        findings_by_id = {f["id"]: f for f in result["findings"]}

        entries: list[dict[str, Any]] = []
        for entry in result["timeline"]:
            if time_from is not None or time_to is not None:
                # 无定位时间的条目无法判断是否落在区间内，不猜测，直接排除
                if not entry["time"]:
                    continue
                entry_time = datetime.fromisoformat(entry["time"])
                if time_from is not None and entry_time < time_from:
                    continue
                if time_to is not None and entry_time > time_to:
                    continue
            entry_findings = [
                findings_by_id[fid]
                for fid in entry["finding_ids"]
                if fid in findings_by_id
            ]
            if type_filter is not None:
                entry_findings = [
                    f for f in entry_findings if f["type"] == type_filter
                ]
                if not entry_findings:
                    continue
            entries.append({**entry, "findings": entry_findings})

        self._json(
            200,
            {
                "analysis_id": analysis_id,
                "target_type": analysis["target_type"],
                "target_id": analysis["target_id"],
                "thresholds": analysis["thresholds"],
                "filters": {
                    "from": query.get("from", [None])[0],
                    "to": query.get("to", [None])[0],
                    "type": type_filter,
                },
                "entry_count": len(entries),
                "entries": entries,
            },
        )

    def _get_analysis_result(self, analysis_id: str) -> None:
        analysis, _ = self._completed_analysis_result(analysis_id)
        download_name = f"{analysis['id']}.analysis-result.json"
        self._file_download(Path(analysis["result_path"]), download_name)

    # ------------------------------------------------- 声明身份核验端点

    def _public_identity_check(self, check: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": check["id"],
            "status": check["status"],
            "created_at": check["created_at"],
            "updated_at": check["updated_at"],
            "target_type": check["target_type"],
            "target_id": check["target_id"],
            "progress": check["progress"],
            "phase": check["phase"],
            "error": check["error"],
            "email_count": check["email_count"],
            "finding_count": check["finding_count"],
            "review_count": check["review_count"],
            "stats": check["stats"],
        }

    def _require_identity_check(self, check_id: str) -> dict[str, Any]:
        if not _UUID_RE.fullmatch(check_id):
            raise ApiError(400, "bad_request", "核验 ID 格式非法")
        check = self.storage.get_identity_check(check_id)
        if check is None:
            raise ApiError(404, "not_found", f"核验不存在: {check_id}")
        return check

    def _list_identity_checks(self) -> None:
        checks = self.storage.list_identity_checks()
        self._json(
            200, {"identity_checks": [self._public_identity_check(c) for c in checks]}
        )

    def _create_identity_check(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if content_type.lower().split(";")[0].strip() != "application/json":
            raise ApiError(
                415,
                "unsupported_media_type",
                "创建声明身份核验请使用 application/json 请求体",
            )
        body = self._read_body_capped()
        if not body:
            raise ApiError(400, "bad_request", "请求体为空")
        try:
            payload = jsonio.loads(body)
        except Exception:
            raise ApiError(400, "bad_request", "请求体不是合法 JSON")
        if not isinstance(payload, dict):
            raise ApiError(400, "bad_request", "请求体必须是 JSON 对象")

        target_type = payload.get("target_type")
        if target_type not in ("job", "case"):
            raise ApiError(
                400, "bad_request", "target_type 必须是 \"job\" 或 \"case\""
            )
        target_id = payload.get("target_id")
        if not isinstance(target_id, str) or not _UUID_RE.fullmatch(target_id):
            raise ApiError(400, "bad_request", "target_id 必须是作业/案件 UUID")
        unknown = set(payload) - {"target_type", "target_id"}
        if unknown:
            raise ApiError(
                400,
                "bad_request",
                "未知请求字段: " + ", ".join(sorted(unknown)),
            )

        with self.create_lock:
            if target_type == "job":
                target = self.storage.get_job(target_id)
                label = "作业"
            else:
                target = self.storage.get_case(target_id)
                label = "案件"
            if target is None:
                raise ApiError(
                    404,
                    "target_not_found",
                    f"目标{label}不存在或已被删除: {target_id}",
                )
            if target["status"] != config.STATUS_COMPLETED:
                raise ApiError(
                    409,
                    "target_not_completed",
                    f"目标{label}未完成，不能创建声明身份核验: {target_id} "
                    f"(当前状态: {target['status']})",
                )
            check_id = str(uuid.uuid4())
            Storage.prepare_identity_check_dir(check_id)
            self.storage.create_identity_check(check_id, target_type, target_id)

        self.identity_processor.enqueue(check_id)
        check = self.storage.get_identity_check(check_id)
        self._json(201, self._public_identity_check(check))

    def _get_identity_check(self, check_id: str) -> None:
        self._json(200, self._public_identity_check(self._require_identity_check(check_id)))

    def _completed_identity_result(
        self, check_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        check = self._require_identity_check(check_id)
        if check["status"] != config.STATUS_COMPLETED or not check["result_path"]:
            raise ApiError(
                409,
                "not_ready",
                f"核验尚未完成 (当前状态: {check['status']})",
            )
        result = jsonio.loads(Path(check["result_path"]).read_text("utf-8"))
        return check, result

    def _identity_filters(
        self, query: dict[str, list[str]]
    ) -> tuple[str | None, str | None, str | None]:
        """解析 ?type=&status=&domain= 筛选参数，非法值明确拒绝。"""
        raw_type = query.get("type", [None])[0]
        if raw_type is not None and raw_type not in identity.FINDING_TYPES:
            raise ApiError(
                400,
                "bad_request",
                f"未知发现类型: {raw_type!r}，可选: "
                + ", ".join(identity.FINDING_TYPES),
            )
        raw_status = query.get("status", [None])[0]
        if raw_status is not None and raw_status not in (
            "observed",
            "needs_review",
            "inconclusive",
        ):
            raise ApiError(
                400,
                "bad_request",
                "status 须为 observed / needs_review / inconclusive",
            )
        raw_domain = query.get("domain", [None])[0]
        if "domain" in query and (raw_domain is None or not raw_domain.strip()):
            raise ApiError(
                400, "bad_request", "domain 筛选参数不能为空（须为域名）"
            )
        if raw_domain is not None:
            raw_domain = raw_domain.strip().lower()
            if len(raw_domain) > 253 or any(ch.isspace() for ch in raw_domain):
                raise ApiError(
                    400, "bad_request", "domain 筛选参数非法（须为域名）"
                )
        return raw_type, raw_status, raw_domain

    @staticmethod
    def _finding_domains(finding: dict[str, Any]) -> list[str]:
        """收集一条发现涉及的全部域（小写去重保序），供 domain 筛选。"""
        domains: list[str] = []

        def add(value: Any) -> None:
            if isinstance(value, str) and value:
                low = value.lower()
                if low not in domains:
                    domains.append(low)

        evidence = finding.get("evidence") or {}
        add(evidence.get("from_domain"))
        add(evidence.get("message_id_domain"))
        add(evidence.get("from", {}).get("domain") if isinstance(
            evidence.get("from"), dict
        ) else None)
        for cell in evidence.get("comparisons", []) or []:
            add(cell.get("domain"))
        for dom in evidence.get("domains", []) or []:
            add(dom)
        for sig in evidence.get("signatures", []) or []:
            add(sig.get("d"))
            add(sig.get("i_domain"))
        for item in evidence.get("emails", []) or []:
            add(item.get("message_id_domain"))
        for key in ("reply", "parent"):
            val = evidence.get(key)
            if isinstance(val, dict):
                add(val.get("domain"))
        for dom in evidence.get("ancestor_domains", []) or []:
            add(dom)
        return domains

    def _match_finding(
        self,
        finding: dict[str, Any],
        ftype: str | None,
        fstatus: str | None,
        domain: str | None,
    ) -> bool:
        if ftype is not None and finding["type"] != ftype:
            return False
        if fstatus is not None and finding["status"] != fstatus:
            return False
        if domain is not None:
            domains = self._finding_domains(finding)
            if not any(d == domain or d.endswith("." + domain) for d in domains):
                return False
        return True

    def _get_identity_findings(
        self, check_id: str, query: dict[str, list[str]]
    ) -> None:
        check, result = self._completed_identity_result(check_id)
        ftype, fstatus, domain = self._identity_filters(query)
        findings = [
            f
            for f in result["findings"]
            if self._match_finding(f, ftype, fstatus, domain)
        ]
        flags = result.get("review_flags", [])
        if domain is not None:
            flags = [
                f
                for f in flags
                if self._review_flag_mentions_domain(f, domain)
            ]
        self._json(
            200,
            {
                "identity_check_id": check_id,
                "target_type": check["target_type"],
                "target_id": check["target_id"],
                "filters": {"type": ftype, "status": fstatus, "domain": domain},
                "finding_count": len(findings),
                "findings": findings,
                "review_flag_count": len(flags),
                "review_flags": flags,
            },
        )

    @staticmethod
    def _review_flag_mentions_domain(flag: dict[str, Any], domain: str) -> bool:
        """待复核证据的域匹配（宽松：证据 JSON 中出现该域字符串即可）。"""
        evidence = flag.get("evidence") or {}
        blob = jsonio.dumps(evidence, ensure_ascii=False).lower()
        return domain in blob

    def _get_identity_emails(
        self, check_id: str, query: dict[str, list[str]]
    ) -> None:
        check, result = self._completed_identity_result(check_id)
        ftype, fstatus, domain = self._identity_filters(query)
        # 邮件级检查类型（checks 中可用的键）
        email_level_types = (
            "from_domain_mismatch",
            "message_id_domain_drift",
            "dkim_from_not_covered",
        )
        if ftype is not None and ftype not in email_level_types:
            raise ApiError(
                400,
                "bad_request",
                "邮件汇总接口的 type 须为 "
                + " / ".join(email_level_types),
            )
        reports = result.get("email_reports", [])
        out = []
        for report in reports:
            checks = report.get("checks", {})

            def is_finding_cell(cell: dict[str, Any] | None) -> bool:
                """检查单元是否对应一条实际差异发现（不含无法核验）。

                无法核验（inconclusive）通过 ``status=inconclusive`` 单独
                筛选；类型筛选只返回真正观察到差异/待复核的邮件。
                """
                if not cell or cell.get("status") is None:
                    return False
                if cell.get("status") == "needs_review":
                    return True
                # observed：只有明确差异（match is False）才算发现
                return (
                    cell.get("status") == "observed"
                    and cell.get("match") is False
                )

            if ftype is not None:
                if not is_finding_cell(checks.get(ftype)):
                    continue
            if fstatus is not None:
                cells = checks.values()
                if not any(
                    c.get("status") == fstatus
                    and (is_finding_cell(c) if fstatus == "observed" else True)
                    for c in cells
                ):
                    continue
            if domain is not None:
                mentioned = {
                    str(report.get("from_domain") or "").lower(),
                    str(report.get("message_id_domain") or "").lower(),
                }
                mentioned.update(
                    str(d or "").lower()
                    for d in (report.get("sender_domains") or [])
                    + (report.get("return_path_domains") or [])
                    + (report.get("reply_to_domains") or [])
                )
                mentioned.update(
                    str(dkim.get("d") or "").lower()
                    for dkim in report.get("dkim", [])
                )
                mentioned.update(
                    str(dkim.get("i_domain") or "").lower()
                    for dkim in report.get("dkim", [])
                )
                if not any(
                    d and (d == domain or d.endswith("." + domain))
                    for d in mentioned
                ):
                    continue
            out.append(report)
        self._json(
            200,
            {
                "identity_check_id": check_id,
                "filters": {"type": ftype, "status": fstatus, "domain": domain},
                "email_count": len(out),
                "emails": out,
            },
        )

    def _get_identity_threads(
        self, check_id: str, query: dict[str, list[str]]
    ) -> None:
        check, result = self._completed_identity_result(check_id)
        ftype, _, domain = self._identity_filters(query)
        findings_by_id = {f["id"]: f for f in result["findings"]}
        reports = result.get("thread_reports", [])
        out = []
        for report in reports:
            related = [
                findings_by_id[fid]
                for fid in report.get("finding_ids", [])
                if fid in findings_by_id
            ]
            if ftype is not None:
                related = [f for f in related if f["type"] == ftype]
                if not related:
                    continue
            if domain is not None:
                related = [
                    f
                    for f in related
                    if self._match_finding(f, None, None, domain)
                ]
                if not related:
                    continue
            out.append({**report, "findings": related})
        self._json(
            200,
            {
                "identity_check_id": check_id,
                "filters": {"type": ftype, "domain": domain},
                "thread_count": len(out),
                "threads": out,
            },
        )

    def _get_identity_result(self, check_id: str) -> None:
        check, _ = self._completed_identity_result(check_id)
        download_name = f"{check['id']}.identity-result.json"
        self._file_download(Path(check["result_path"]), download_name)

    # ------------------------------------------------- 附件流转追踪端点

    def _public_flow(self, flow: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": flow["id"],
            "status": flow["status"],
            "created_at": flow["created_at"],
            "updated_at": flow["updated_at"],
            "target_type": flow["target_type"],
            "target_id": flow["target_id"],
            "progress": flow["progress"],
            "phase": flow["phase"],
            "error": flow["error"],
            "email_count": flow["email_count"],
            "event_count": flow["event_count"],
            "review_count": flow["review_count"],
            "stats": flow["stats"],
        }

    def _require_flow(self, flow_id: str) -> dict[str, Any]:
        if not _UUID_RE.fullmatch(flow_id):
            raise ApiError(400, "bad_request", "追踪 ID 格式非法")
        flow = self.storage.get_attachment_flow(flow_id)
        if flow is None:
            raise ApiError(404, "not_found", f"附件流转追踪不存在: {flow_id}")
        return flow

    def _list_attachment_flows(self) -> None:
        flows = self.storage.list_attachment_flows()
        self._json(
            200,
            {"attachment_flows": [self._public_flow(f) for f in flows]},
        )

    def _create_attachment_flow(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if content_type.lower().split(";")[0].strip() != "application/json":
            raise ApiError(
                415,
                "unsupported_media_type",
                "创建附件流转追踪请使用 application/json 请求体",
            )
        body = self._read_body_capped()
        if not body:
            raise ApiError(400, "bad_request", "请求体为空")
        try:
            payload = jsonio.loads(body)
        except Exception:
            raise ApiError(400, "bad_request", "请求体不是合法 JSON")
        if not isinstance(payload, dict):
            raise ApiError(400, "bad_request", "请求体必须是 JSON 对象")

        target_type = payload.get("target_type")
        if target_type not in ("job", "case"):
            raise ApiError(
                400, "bad_request", "target_type 必须是 \"job\" 或 \"case\""
            )
        target_id = payload.get("target_id")
        if not isinstance(target_id, str) or not _UUID_RE.fullmatch(target_id):
            raise ApiError(400, "bad_request", "target_id 必须是作业/案件 UUID")
        unknown = set(payload) - {"target_type", "target_id"}
        if unknown:
            raise ApiError(
                400,
                "bad_request",
                "未知请求字段: " + ", ".join(sorted(unknown)),
            )

        with self.create_lock:
            if target_type == "job":
                target = self.storage.get_job(target_id)
                label = "作业"
            else:
                target = self.storage.get_case(target_id)
                label = "案件"
            if target is None:
                raise ApiError(
                    404,
                    "target_not_found",
                    f"目标{label}不存在或已被删除: {target_id}",
                )
            if target["status"] != config.STATUS_COMPLETED:
                raise ApiError(
                    409,
                    "target_not_completed",
                    f"目标{label}未完成，不能创建附件流转追踪: {target_id} "
                    f"(当前状态: {target['status']})",
                )
            flow_id = str(uuid.uuid4())
            Storage.prepare_attachment_flow_dir(flow_id)
            self.storage.create_attachment_flow(flow_id, target_type, target_id)

        self.flow_processor.enqueue(flow_id)
        flow = self.storage.get_attachment_flow(flow_id)
        self._json(201, self._public_flow(flow))

    def _get_attachment_flow(self, flow_id: str) -> None:
        self._json(200, self._public_flow(self._require_flow(flow_id)))

    def _completed_flow_result(
        self, flow_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        flow = self._require_flow(flow_id)
        if flow["status"] != config.STATUS_COMPLETED or not flow["result_path"]:
            raise ApiError(
                409,
                "not_ready",
                f"追踪尚未完成 (当前状态: {flow['status']})",
            )
        result = jsonio.loads(Path(flow["result_path"]).read_text("utf-8"))
        return flow, result

    def _flow_filters(
        self, query: dict[str, list[str]]
    ) -> tuple[str | None, int | None, str | None, str | None]:
        """解析 ?type=&thread=&filename=&sha256= 筛选参数，非法值明确拒绝。"""
        raw_type = query.get("type", [None])[0]
        if raw_type is not None and raw_type not in attachflow.EVENT_TYPES:
            raise ApiError(
                400,
                "bad_request",
                f"未知事件类型: {raw_type!r}，可选: "
                + ", ".join(attachflow.EVENT_TYPES),
            )
        raw_thread = query.get("thread", [None])[0]
        thread_uid = None
        if raw_thread is not None:
            if not raw_thread.isdigit():
                raise ApiError(
                    400,
                    "bad_request",
                    "thread 筛选参数须为非负整数（会话根节点 uid）",
                )
            thread_uid = int(raw_thread)
        raw_filename = query.get("filename", [None])[0]
        if "filename" in query and (
            raw_filename is None or not raw_filename.strip()
        ):
            raise ApiError(400, "bad_request", "filename 筛选参数不能为空")
        raw_sha256 = query.get("sha256", [None])[0]
        if raw_sha256 is not None:
            if not _SHA256_RE.fullmatch(raw_sha256):
                raise ApiError(
                    400,
                    "bad_request",
                    "sha256 筛选参数须为 64 位十六进制字符串",
                )
            raw_sha256 = raw_sha256.lower()
        return raw_type, thread_uid, raw_filename, raw_sha256

    @staticmethod
    def _match_event(
        event: dict[str, Any],
        ftype: str | None,
        thread_uid: int | None,
        filename: str | None,
        sha256: str | None,
    ) -> bool:
        if ftype is not None and event["type"] != ftype:
            return False
        if thread_uid is not None and event["thread_root_uid"] != thread_uid:
            return False
        if filename is not None and filename not in (
            event.get("filename"),
            event.get("previous_filename"),
        ):
            return False
        if sha256 is not None and sha256 not in (
            event.get("sha256"),
            event.get("previous_sha256"),
        ):
            return False
        return True

    @staticmethod
    def _match_review(
        review: dict[str, Any],
        thread_uid: int | None,
        filename: str | None,
        sha256: str | None,
    ) -> bool:
        if thread_uid is not None and review["thread_root_uid"] != thread_uid:
            return False
        evidence = review.get("evidence") or {}
        if filename is not None and evidence.get("filename") != filename:
            return False
        if sha256 is not None and evidence.get("sha256") != sha256:
            return False
        return True

    def _get_flow_events(
        self, flow_id: str, query: dict[str, list[str]]
    ) -> None:
        flow, result = self._completed_flow_result(flow_id)
        ftype, thread_uid, filename, sha256 = self._flow_filters(query)
        events = [
            e
            for e in result["events"]
            if self._match_event(e, ftype, thread_uid, filename, sha256)
        ]
        # type 是事件专属维度：给出 type 时待复核项不参与（无事件类型）
        if ftype is not None:
            reviews: list[dict[str, Any]] = []
        else:
            reviews = [
                r
                for r in result["reviews"]
                if self._match_review(r, thread_uid, filename, sha256)
            ]
        self._json(
            200,
            {
                "flow_id": flow_id,
                "target_type": flow["target_type"],
                "target_id": flow["target_id"],
                "filters": {
                    "type": ftype,
                    "thread": thread_uid,
                    "filename": filename,
                    "sha256": sha256,
                },
                "event_count": len(events),
                "events": events,
                "review_count": len(reviews),
                "reviews": reviews,
            },
        )

    def _get_flow_attachments(
        self, flow_id: str, query: dict[str, list[str]]
    ) -> None:
        flow, result = self._completed_flow_result(flow_id)
        _, _, filename, sha256 = self._flow_filters(query)
        entries = result["attachments"]
        if sha256 is not None:
            entries = [e for e in entries if e["sha256"] == sha256]
        if filename is not None:
            entries = [e for e in entries if filename in e["names"]]
        self._json(
            200,
            {
                "flow_id": flow_id,
                "target_type": flow["target_type"],
                "target_id": flow["target_id"],
                "filters": {"sha256": sha256, "filename": filename},
                "attachment_count": len(entries),
                "attachments": entries,
            },
        )

    def _get_flow_result(self, flow_id: str) -> None:
        flow, _ = self._completed_flow_result(flow_id)
        download_name = f"{flow['id']}.flow-result.json"
        self._file_download(Path(flow["result_path"]), download_name)

    # ------------------------------------------------- 收件人流转分析端点

    def _public_recipient_flow(self, flow: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": flow["id"],
            "status": flow["status"],
            "created_at": flow["created_at"],
            "updated_at": flow["updated_at"],
            "target_type": flow["target_type"],
            "target_id": flow["target_id"],
            "progress": flow["progress"],
            "phase": flow["phase"],
            "error": flow["error"],
            "email_count": flow["email_count"],
            "event_count": flow["event_count"],
            "review_count": flow["review_count"],
            "stats": flow["stats"],
        }

    def _require_recipient_flow(self, flow_id: str) -> dict[str, Any]:
        if not _UUID_RE.fullmatch(flow_id):
            raise ApiError(400, "bad_request", "分析 ID 格式非法")
        flow = self.storage.get_recipient_flow(flow_id)
        if flow is None:
            raise ApiError(404, "not_found", f"收件人流转分析不存在: {flow_id}")
        return flow

    def _list_recipient_flows(self) -> None:
        flows = self.storage.list_recipient_flows()
        self._json(
            200,
            {"recipient_flows": [self._public_recipient_flow(f) for f in flows]},
        )

    def _create_recipient_flow(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if content_type.lower().split(";")[0].strip() != "application/json":
            raise ApiError(
                415,
                "unsupported_media_type",
                "创建收件人流转分析请使用 application/json 请求体",
            )
        body = self._read_body_capped()
        if not body:
            raise ApiError(400, "bad_request", "请求体为空")
        try:
            payload = jsonio.loads(body)
        except Exception:
            raise ApiError(400, "bad_request", "请求体不是合法 JSON")
        if not isinstance(payload, dict):
            raise ApiError(400, "bad_request", "请求体必须是 JSON 对象")

        target_type = payload.get("target_type")
        if target_type not in ("job", "case"):
            raise ApiError(
                400, "bad_request", "target_type 必须是 \"job\" 或 \"case\""
            )
        target_id = payload.get("target_id")
        if not isinstance(target_id, str) or not _UUID_RE.fullmatch(target_id):
            raise ApiError(400, "bad_request", "target_id 必须是作业/案件 UUID")
        unknown = set(payload) - {"target_type", "target_id"}
        if unknown:
            raise ApiError(
                400,
                "bad_request",
                "未知请求字段: " + ", ".join(sorted(unknown)),
            )

        with self.create_lock:
            if target_type == "job":
                target = self.storage.get_job(target_id)
                label = "作业"
            else:
                target = self.storage.get_case(target_id)
                label = "案件"
            if target is None:
                raise ApiError(
                    404,
                    "target_not_found",
                    f"目标{label}不存在或已被删除: {target_id}",
                )
            if target["status"] != config.STATUS_COMPLETED:
                raise ApiError(
                    409,
                    "target_not_completed",
                    f"目标{label}未完成，不能创建收件人流转分析: {target_id} "
                    f"(当前状态: {target['status']})",
                )
            flow_id = str(uuid.uuid4())
            Storage.prepare_recipient_flow_dir(flow_id)
            self.storage.create_recipient_flow(flow_id, target_type, target_id)

        self.recipient_flow_processor.enqueue(flow_id)
        flow = self.storage.get_recipient_flow(flow_id)
        self._json(201, self._public_recipient_flow(flow))

    def _get_recipient_flow(self, flow_id: str) -> None:
        self._json(
            200, self._public_recipient_flow(self._require_recipient_flow(flow_id))
        )

    def _completed_recipient_flow_result(
        self, flow_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        flow = self._require_recipient_flow(flow_id)
        if flow["status"] != config.STATUS_COMPLETED or not flow["result_path"]:
            raise ApiError(
                409,
                "not_ready",
                f"分析尚未完成 (当前状态: {flow['status']})",
            )
        result = jsonio.loads(Path(flow["result_path"]).read_text("utf-8"))
        return flow, result

    def _recipient_flow_filters(
        self, query: dict[str, list[str]]
    ) -> tuple[str | None, int | None, str | None, str | None]:
        """解析 ?type=&thread=&address=&kind= 筛选参数，非法值明确拒绝。"""
        raw_type = query.get("type", [None])[0]
        if raw_type is not None and raw_type not in recipientflow.EVENT_TYPES:
            raise ApiError(
                400,
                "bad_request",
                f"未知事件类型: {raw_type!r}，可选: "
                + ", ".join(recipientflow.EVENT_TYPES),
            )
        raw_thread = query.get("thread", [None])[0]
        thread_uid = None
        if raw_thread is not None:
            if not raw_thread.isdigit():
                raise ApiError(
                    400,
                    "bad_request",
                    "thread 筛选参数须为非负整数（会话根节点 uid）",
                )
            thread_uid = int(raw_thread)
        raw_address = query.get("address", [None])[0]
        if "address" in query and (
            raw_address is None or not raw_address.strip()
        ):
            raise ApiError(400, "bad_request", "address 筛选参数不能为空")
        if raw_address is not None:
            raw_address = raw_address.strip()
            if _address_filter_key(raw_address) is None:
                raise ApiError(
                    400,
                    "bad_request",
                    "address 筛选参数须为 local-part@domain 形态的地址",
                )
        raw_kind = query.get("kind", [None])[0]
        if raw_kind is not None and raw_kind not in recipientflow.REVIEW_KINDS:
            raise ApiError(
                400,
                "bad_request",
                f"未知待复核类型: {raw_kind!r}，可选: "
                + ", ".join(recipientflow.REVIEW_KINDS),
            )
        return raw_type, thread_uid, raw_address, raw_kind

    @staticmethod
    def _event_mentions_address(event: dict[str, Any], address: str) -> bool:
        """事件是否针对某地址：逐地址事件以其主地址为准。

        匹配口径与引擎一致（忽略显示名、域名小写、local-part 原样）；
        不按集合快照全集匹配，否则同一边上每个地址都会命中所有事件。
        """
        return event.get("address") == address

    @staticmethod
    def _review_mentions_address(review: dict[str, Any], address: str) -> bool:
        evidence = review.get("evidence") or {}
        # 畸形地址待复核：其原始地址归一化后等于筛选地址
        if evidence.get("raw_address"):
            if _address_filter_key(evidence["raw_address"]) == address:
                return True
        for cell in evidence.get("child_malformed", []) + evidence.get(
            "parent_malformed", []
        ):
            if _address_filter_key(cell.get("raw_address", "")) == address:
                return True
        # 父子边背景待复核：地址须出现在差异清单（而非全集快照）中
        differences = (evidence.get("sets") or {}).get("differences")
        if differences is not None:
            for key, addrs in differences.items():
                if key == "role_changed":
                    if any(
                        isinstance(c, dict) and c.get("address") == address
                        for c in addrs
                    ):
                        return True
                elif address in addrs:
                    return True
        # 邮件级列表迹象：提示文本中精确出现该地址 token
        if any(address in _address_tokens(hint) for hint in evidence.get("hints", []) or []):
            return True
        return False

    def _get_recipient_flow_events(
        self, flow_id: str, query: dict[str, list[str]]
    ) -> None:
        flow, result = self._completed_recipient_flow_result(flow_id)
        ftype, thread_uid, address, kind = self._recipient_flow_filters(query)

        def event_matches(event: dict[str, Any]) -> bool:
            if ftype is not None and event["type"] != ftype:
                return False
            if thread_uid is not None and event["thread_root_uid"] != thread_uid:
                return False
            if address is not None and not self._event_mentions_address(
                event, address
            ):
                return False
            return True

        events = [e for e in result["events"] if event_matches(e)]

        # type 是事件专属维度：给出 type 时待复核项不参与（无事件类型）；
        # kind 是待复核专属维度
        reviews = [
            r
            for r in result["reviews"]
            if (thread_uid is None or r["thread_root_uid"] == thread_uid)
            and (kind is None or r["kind"] == kind)
            and (address is None or self._review_mentions_address(r, address))
        ]
        if ftype is not None:
            reviews = []

        self._json(
            200,
            {
                "recipient_flow_id": flow_id,
                "target_type": flow["target_type"],
                "target_id": flow["target_id"],
                "filters": {
                    "type": ftype,
                    "thread": thread_uid,
                    "address": address,
                    "kind": kind,
                },
                "event_count": len(events),
                "events": events,
                "review_count": len(reviews),
                "reviews": reviews,
            },
        )

    def _get_recipient_flow_threads(
        self, flow_id: str, query: dict[str, list[str]]
    ) -> None:
        flow, result = self._completed_recipient_flow_result(flow_id)
        _, thread_uid, address, kind = self._recipient_flow_filters(query)
        events_by_id = {e["id"]: e for e in result["events"]}
        reviews_by_id = {r["id"]: r for r in result["reviews"]}

        threads: list[dict[str, Any]] = []
        for summary in result["threads"]:
            if thread_uid is not None and summary["root_uid"] != thread_uid:
                continue
            events = [
                events_by_id[eid]
                for eid in summary["event_ids"]
                if eid in events_by_id
            ]
            reviews = [
                reviews_by_id[rid]
                for rid in summary["review_ids"]
                if rid in reviews_by_id
            ]
            if address is not None:
                events = [
                    e for e in events
                    if self._event_mentions_address(e, address)
                ]
                reviews = [
                    r for r in reviews
                    if self._review_mentions_address(r, address)
                ]
            if kind is not None:
                reviews = [r for r in reviews if r["kind"] == kind]
            if address is not None or kind is not None:
                if not events and not reviews:
                    continue
            threads.append(
                {
                    **summary,
                    "matched_event_count": len(events),
                    "matched_review_count": len(reviews),
                    "events": events,
                    "reviews": reviews,
                }
            )
        self._json(
            200,
            {
                "recipient_flow_id": flow_id,
                "target_type": flow["target_type"],
                "target_id": flow["target_id"],
                "filters": {
                    "thread": thread_uid,
                    "address": address,
                    "kind": kind,
                },
                "thread_count": len(threads),
                "threads": threads,
            },
        )

    def _get_recipient_flow_addresses(
        self, flow_id: str, query: dict[str, list[str]]
    ) -> None:
        flow, result = self._completed_recipient_flow_result(flow_id)
        raw_address = query.get("address", [None])[0]
        if "address" in query and (
            raw_address is None or not raw_address.strip()
        ):
            raise ApiError(400, "bad_request", "address 筛选参数不能为空")
        address_key = None
        if raw_address is not None:
            address_key = _address_filter_key(raw_address.strip())
            if address_key is None:
                raise ApiError(
                    400,
                    "bad_request",
                    "address 筛选参数须为 local-part@domain 形态的地址",
                )
        entries = result["addresses"]
        if address_key is not None:
            # 与事件口径一致：忽略显示名、域名转小写、local-part 保持原样
            entries = [
                e for e in entries
                if _address_filter_key(e["address"]) == address_key
            ]
        self._json(
            200,
            {
                "recipient_flow_id": flow_id,
                "target_type": flow["target_type"],
                "target_id": flow["target_id"],
                "filters": {"address": raw_address},
                "address_count": len(entries),
                "addresses": entries,
            },
        )

    def _get_recipient_flow_result(self, flow_id: str) -> None:
        flow, _ = self._completed_recipient_flow_result(flow_id)
        download_name = f"{flow['id']}.recipient-flow-result.json"
        self._file_download(Path(flow["result_path"]), download_name)


def _compact_forest(
    roots: list[dict[str, Any]],
    compact_node: Any = None,
) -> list[dict[str, Any]]:
    """迭代式生成紧凑树视图，不受递归深度限制，children 保持原顺序。"""
    if compact_node is None:
        compact_node = _compact_node
    compact_roots: list[dict[str, Any]] = []
    for source_root in roots:
        target_root = compact_node(source_root)
        compact_roots.append(target_root)
        # (源节点, 目标父节点)；逆序压栈以保持 children 顺序
        stack: list[tuple[dict[str, Any], dict[str, Any]]] = [
            (child, target_root)
            for child in reversed(source_root["children"])
        ]
        while stack:
            source, target_parent = stack.pop()
            target = compact_node(source)
            target_parent["children"].append(target)
            for child in reversed(source["children"]):
                stack.append((child, target))
    return compact_roots


def _compact_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": node["uid"],
        "source_file": node["source_file"],
        "message_id": node["message_id"],
        "date": node["date"],
        "from": node["from"],
        "subject": node["subject"],
        "issue_count": len(node["issues"]),
        "attachment_count": len(node["attachments"]),
        "children": [],
    }


def _compact_case_node(node: dict[str, Any]) -> dict[str, Any]:
    """案件合并树的紧凑节点：保留来源与冲突/补链标记，省略正文等明细。"""
    info = node["merge_info"]
    return {
        "uid": node["uid"],
        "message_id": node["message_id"],
        "date": node["date"],
        "from": node["from"],
        "subject": node["subject"],
        "sources": node["sources"],
        "conflict": info["conflict"],
        "relinked": info["relinked_parent"] is not None,
        "issue_count": len(node["issues"]),
        "attachment_count": len(node["attachments"]),
        "children": [],
    }


def _basename(filename: str | None) -> str:
    if not filename:
        return "package.zip"
    # 只保留文件名部分，拒绝客户端给出的任何路径
    clean = filename.replace("\\", "/").split("/")[-1].strip()
    clean = clean or "package.zip"
    return clean[:255]


# 时序核验阈值：键 -> 默认值（来自 config，可被环境变量覆盖）
_THRESHOLD_DEFAULTS = {
    "clock_skew_seconds": config.DEFAULT_CLOCK_SKEW_SECONDS,
    "max_transit_seconds": config.DEFAULT_MAX_TRANSIT_SECONDS,
}


def _parse_thresholds(raw: Any) -> dict[str, int]:
    """校验用户提交的阈值；缺省项用默认值，非法输入明确拒绝。"""
    thresholds = dict(_THRESHOLD_DEFAULTS)
    if raw is None:
        return thresholds
    if not isinstance(raw, dict):
        raise ApiError(400, "bad_request", "thresholds 必须是 JSON 对象")
    for key, value in raw.items():
        if key not in thresholds:
            raise ApiError(
                400,
                "bad_request",
                f"未知阈值项: {key!r}，可选: " + ", ".join(thresholds),
            )
        # bool 是 int 子类，明确排除
        if not isinstance(value, int) or isinstance(value, bool):
            raise ApiError(
                400, "bad_request", f"阈值 {key} 必须是非负整数（秒）"
            )
        if value < 0 or value > config.MAX_THRESHOLD_SECONDS:
            raise ApiError(
                400,
                "bad_request",
                f"阈值 {key} 须在 0 到 {config.MAX_THRESHOLD_SECONDS} 秒之间，"
                f"实际为 {value}",
            )
        thresholds[key] = value
    return thresholds


def _address_filter_key(raw: str) -> str | None:
    """筛选地址归一化：与收件人引擎同口径（域名小写、local-part 原样）。

    形态无法可靠归一化时返回 None（调用方按 400 处理）。
    """
    if not raw or raw.count("@") != 1 or any(ch.isspace() for ch in raw):
        return None
    local, domain = raw.split("@")
    if not local or not domain or local.startswith('"'):
        return None
    if any(
        not label or len(label) > 63
        or label.startswith("-") or label.endswith("-")
        for label in domain.split(".")
    ):
        return None
    return f"{local}@{domain.lower()}"


_ADDR_TOKEN_RE = re.compile(r"[^\s,;()]+@[^\s,;()]+")


def _address_tokens(text: str) -> set[str]:
    """从提示文本中提取地址 token 并归一化（仅用于待复核证据匹配）。"""
    return {
        key
        for token in _ADDR_TOKEN_RE.findall(text)
        if (key := _address_filter_key(token.strip("<>:"))) is not None
    }


def _parse_time_filter(
    raw_values: list[str] | None, name: str
) -> datetime | None:
    """解析时间线筛选的时间边界；缺时区不猜测，直接 400。"""
    if not raw_values:
        return None
    raw = raw_values[0]
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise ApiError(
            400,
            "bad_request",
            f"筛选参数 {name} 不是合法 ISO 8601 时间: {raw!r}",
        ) from None
    if dt.tzinfo is None:
        raise ApiError(
            400,
            "bad_request",
            f"筛选参数 {name} 必须带时区（如 2026-09-01T00:00:00+00:00），"
            "不做时区猜测",
        )
    return dt


# ---------------------------------------------------------------- 服务装配

class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str, port: int) -> None:
        super().__init__((host, port), Handler)
        self.storage = Storage()
        self.processor = JobProcessor(self.storage)
        self.case_processor = CaseProcessor(self.storage)
        self.timing_processor = TimingProcessor(self.storage)
        self.identity_processor = IdentityProcessor(self.storage)
        self.flow_processor = AttachmentFlowProcessor(self.storage)
        self.recipient_flow_processor = RecipientFlowProcessor(self.storage)
        self.create_lock = threading.Lock()
        # 注入给 Handler 实例使用
        Handler.storage = self.storage
        Handler.processor = self.processor
        Handler.case_processor = self.case_processor
        Handler.timing_processor = self.timing_processor
        Handler.identity_processor = self.identity_processor
        Handler.flow_processor = self.flow_processor
        Handler.recipient_flow_processor = self.recipient_flow_processor
        Handler.create_lock = self.create_lock

    def start(self) -> None:
        """启动后台处理线程（serve_forever 由调用方驱动）。"""
        self.processor.start()
        self.case_processor.start()
        self.timing_processor.start()
        self.identity_processor.start()
        self.flow_processor.start()
        self.recipient_flow_processor.start()

    def serve(self) -> None:
        self.processor.start()
        self.case_processor.start()
        self.timing_processor.start()
        self.identity_processor.start()
        self.flow_processor.start()
        self.recipient_flow_processor.start()
        try:
            self.serve_forever()
        finally:
            self.server_close()
            self.storage.close()
