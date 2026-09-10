"""HTTP API 层：基于标准库 http.server，无任何外部依赖。"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import config, jsonio
from .caseproc import CaseProcessor
from .processor import JobProcessor
from .storage import Storage

_UUID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
_IDEMPOTENCY_RE = re.compile(r"^[\x21-\x7E]{8,200}$")

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
            query = parse_qs(parsed.query)

            if path == "/health":
                self._json(200, {"status": "ok", "version": "1.0.0"})
            elif path == "/api/v1/jobs":
                self._list_jobs()
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


# ---------------------------------------------------------------- 服务装配

class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str, port: int) -> None:
        super().__init__((host, port), Handler)
        self.storage = Storage()
        self.processor = JobProcessor(self.storage)
        self.case_processor = CaseProcessor(self.storage)
        self.create_lock = threading.Lock()
        # 注入给 Handler 实例使用
        Handler.storage = self.storage
        Handler.processor = self.processor
        Handler.case_processor = self.case_processor
        Handler.create_lock = self.create_lock

    def start(self) -> None:
        """启动后台处理线程（serve_forever 由调用方驱动）。"""
        self.processor.start()
        self.case_processor.start()

    def serve(self) -> None:
        self.processor.start()
        self.case_processor.start()
        try:
            self.serve_forever()
        finally:
            self.server_close()
            self.storage.close()
