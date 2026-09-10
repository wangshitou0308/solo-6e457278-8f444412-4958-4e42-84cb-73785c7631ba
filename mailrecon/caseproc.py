"""案件合并后台处理流水线。

与作业处理器（processor.py）结构一致：单后台线程顺序消费案件队列，
逐个加载源作业的结果 JSON 并做跨包合并，避免多案件同时加载挤占内存。
"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Any

from . import casemerge, config, jsonio
from .storage import Storage, utc_now


class CaseProcessor:
    """单后台线程顺序处理案件合并队列。"""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._queue: list[str] = []
        self._cond = threading.Condition()
        self._known: set[str] = set()
        self._thread = threading.Thread(
            target=self._run, name="case-processor", daemon=True
        )

    def start(self) -> None:
        # 重启恢复：上次未完成的案件重新入队
        for case in self._storage.unfinished_cases():
            self.enqueue(case["id"])
        self._thread.start()

    def enqueue(self, case_id: str) -> None:
        with self._cond:
            if case_id not in self._known:
                self._known.add(case_id)
                self._queue.append(case_id)
                self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                case_id = self._queue.pop(0)
            self._known.discard(case_id)
            try:
                self._process(case_id)
            except Exception:
                # 兜底：任何意外都落库为 failed，线程本身不能死
                self._storage.fail_case(
                    case_id,
                    "内部错误:\n" + traceback.format_exc(limit=5),
                )

    # ---------------------------------------------------------- 流水线

    def _process(self, case_id: str) -> None:
        case = self._storage.get_case(case_id)
        if case is None:
            return
        job_ids = case["job_ids"]

        self._storage.update_case_progress(
            case_id, status=config.STATUS_PROCESSING, progress=5,
            phase="loading_results",
        )

        # ---- 1. 逐个加载源作业结果（源作业在处理前被删除则案件失败） ----
        job_results: list[dict[str, Any]] = []
        source_jobs: list[dict[str, Any]] = []
        total = len(job_ids)
        for index, job_id in enumerate(job_ids):
            job = self._storage.get_job(job_id)
            if job is None:
                self._storage.fail_case(
                    case_id,
                    f"源作业不存在（可能在案件处理前被删除）: {job_id}",
                )
                return
            if (
                job["status"] != config.STATUS_COMPLETED
                or not job["result_path"]
            ):
                self._storage.fail_case(
                    case_id,
                    f"源作业未完成: {job_id} (当前状态: {job['status']})",
                )
                return
            result_path = Path(job["result_path"])
            if not result_path.is_file():
                self._storage.fail_case(
                    case_id,
                    f"源作业 {job_id} 的结果文件不存在（可能已被清理）",
                )
                return
            try:
                result = jsonio.loads(result_path.read_text("utf-8"))
            except Exception as exc:
                self._storage.fail_case(
                    case_id, f"源作业 {job_id} 的结果文件无法解析: {exc}"
                )
                return
            job_results.append(
                {
                    "job_id": job_id,
                    "original_filename": job["original_filename"],
                    "threads": result.get("threads", []),
                }
            )
            source_jobs.append(
                {
                    "job_id": job_id,
                    "original_filename": job["original_filename"],
                    "email_count": job["email_count"],
                }
            )
            pct = 5 + int(45 * (index + 1) / total)
            self._storage.update_case_progress(
                case_id, progress=min(pct, 50), phase="loading_results"
            )

        # ---- 2. 跨包合并
        self._storage.update_case_progress(
            case_id, progress=60, phase="building_forest"
        )
        merged = casemerge.build_case_forest(job_results)

        # ---- 3. 组装结果并独立落盘（与源作业目录互不影响） -------------
        self._storage.update_case_progress(
            case_id, progress=90, phase="writing_result"
        )
        result = {
            "case_id": case["id"],
            "name": case["name"],
            "status": config.STATUS_COMPLETED,
            "created_at": case["created_at"],
            "completed_at": utc_now(),
            "source_jobs": source_jobs,
            "stats": merged["stats"],
            "threads": merged["roots"],
        }

        case_dir = Storage.case_dir(case_id)
        case_dir.mkdir(parents=True, exist_ok=True)
        result_path = case_dir / "result.json"
        tmp_path = case_dir / "result.json.tmp"
        # 紧凑序列化：跨包拼出的深链上缩进格式体积为 O(深度²)
        with tmp_path.open("w", encoding="utf-8") as fp:
            jsonio.dump(result, fp, ensure_ascii=False, indent=None)
        tmp_path.replace(result_path)

        self._storage.complete_case(
            case_id,
            str(result_path),
            email_count=merged["stats"]["source_emails"],
            thread_count=merged["stats"]["thread_count"],
            stats=merged["stats"],
        )
