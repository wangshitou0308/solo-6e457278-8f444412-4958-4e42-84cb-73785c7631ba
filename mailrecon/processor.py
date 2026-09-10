"""后台作业处理流水线。"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Any

from . import config, jsonio, mailparser, threads, zipguard
from .storage import Storage, utc_now

_PUBLIC_FIELDS = (
    "uid", "source_file", "raw_sha256", "message_id", "in_reply_to",
    "references", "date", "from", "to", "cc", "subject", "body_text",
    "body_html_present", "attachments", "issues",
)


def _public_forest(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """迭代式深拷贝出对外的节点树（剥掉内部字段），不受递归深度限制。"""
    public_roots: list[dict[str, Any]] = []
    for source_root in roots:
        target_root = _public_node(source_root)
        public_roots.append(target_root)
        # DFS：(源节点, 目标父节点)，逆序压栈以保持 children 原顺序
        stack: list[tuple[dict[str, Any], dict[str, Any]]] = [
            (child, target_root)
            for child in reversed(source_root["children"])
        ]
        while stack:
            source, target_parent = stack.pop()
            target = _public_node(source)
            target_parent["children"].append(target)
            for child in reversed(source["children"]):
                stack.append((child, target))
    return public_roots


def _public_node(node: dict[str, Any]) -> dict[str, Any]:
    return {field: node[field] for field in _PUBLIC_FIELDS} | {"children": []}


class JobProcessor:
    """单后台线程顺序消费作业队列，避免多作业同时解压挤爆磁盘。"""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._queue: list[str] = []
        self._cond = threading.Condition()
        self._known: set[str] = set()
        self._thread = threading.Thread(
            target=self._run, name="job-processor", daemon=True
        )

    def start(self) -> None:
        # 重启恢复：上次未完成的作业重新入队
        for job in self._storage.processing_jobs():
            self.enqueue(job["id"])
        self._thread.start()

    def enqueue(self, job_id: str) -> None:
        with self._cond:
            if job_id not in self._known:
                self._known.add(job_id)
                self._queue.append(job_id)
                self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                job_id = self._queue.pop(0)
            self._known.discard(job_id)
            try:
                self._process(job_id)
            except Exception:
                # 兜底：任何意外都落库为 failed，线程本身不能死
                self._storage.fail_job(
                    job_id,
                    "内部错误:\n" + traceback.format_exc(limit=5),
                )

    # ---------------------------------------------------------- 流水线

    def _process(self, job_id: str) -> None:
        job = self._storage.get_job(job_id)
        if job is None:
            return

        upload_path = Path(job["upload_path"])
        if not upload_path.exists():
            self._storage.fail_job(job_id, "原始上传文件不存在（可能已被删除）")
            return

        job_dir = Storage.job_dir(job_id)
        extract_dir = job_dir / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        self._storage.update_progress(
            job_id, status=config.STATUS_PROCESSING, progress=5,
            phase="validating_zip",
        )

        # ---- 1. 安全校验 + 受控解压（压缩包问题直接判失败）
        try:
            eml_paths = zipguard.safe_extract(upload_path, extract_dir)
        except zipguard.ZipRejected as exc:
            self._storage.fail_job(job_id, f"压缩包被拒绝: {exc}")
            return

        self._storage.update_progress(job_id, progress=15, phase="parsing_eml")

        # ---- 2. 逐封解析（非 .eml 跳过；单封失败不影响整包）
        records: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        total = len(eml_paths)
        for index, path in enumerate(eml_paths):
            rel = path.relative_to(extract_dir).as_posix()
            if path.suffix.lower() != ".eml":
                skipped.append(
                    {"source_file": rel, "reason": "不是 .eml 文件，已跳过"}
                )
            else:
                raw = path.read_bytes()
                record = mailparser.parse_eml(raw, rel)
                records.append(record)
            if total:
                pct = 15 + int(65 * (index + 1) / total)
                self._storage.update_progress(
                    job_id, progress=min(pct, 80), phase="parsing_eml"
                )

        # ---- 3. 线程重建
        self._storage.update_progress(job_id, progress=85, phase="building_threads")
        tree = threads.build_threads(records)

        # ---- 4. 组装结果并落盘
        self._storage.update_progress(job_id, progress=92, phase="writing_result")
        result = self._build_result(job, tree, skipped, total)

        result_path = job_dir / "result.json"
        tmp_path = job_dir / "result.json.tmp"
        # 紧凑序列化：缩进格式在纯引用链上会产生 O(深度²) 的闭合缩进，
        # 万级深链时体积爆炸；紧凑 JSON 为线性大小，仍可被任意工具解析。
        with tmp_path.open("w", encoding="utf-8") as fp:
            jsonio.dump(result, fp, ensure_ascii=False, indent=None)
        tmp_path.replace(result_path)

        # ---- 5. 删除解压目录（原始 upload.bin 保留，结果只在 JSON）
        import shutil

        shutil.rmtree(extract_dir, ignore_errors=True)

        self._storage.complete_job(
            job_id,
            str(result_path),
            email_count=result["stats"]["eml_parsed"],
            thread_count=result["stats"]["thread_count"],
            stats=result["stats"],
        )

    def _build_result(
        self,
        job: dict[str, Any],
        tree: dict[str, Any],
        skipped: list[dict[str, str]],
        total_entries: int,
    ) -> dict[str, Any]:
        roots = _public_forest(tree["roots"])

        # 统计
        total_issues = 0
        attachment_count = 0
        for node in tree["messages"]:
            total_issues += len(node["issues"])
            attachment_count += len(node["attachments"])

        stats = {
            "zip_entries": total_entries,
            "eml_parsed": len(tree["messages"]),
            "non_eml_skipped": len(skipped),
            "thread_count": len(roots),
            "attachment_count": attachment_count,
            "issues_total": total_issues,
            "issues_by_type": tree["issues_summary"],
        }

        return {
            "job_id": job["id"],
            "status": config.STATUS_COMPLETED,
            "original_filename": job["original_filename"],
            "created_at": job["created_at"],
            "completed_at": utc_now(),
            "stats": stats,
            "skipped_entries": skipped,
            "threads": roots,
        }
