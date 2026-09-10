"""邮件传输时序核验的后台分析流水线。

与作业/案件处理器结构一致：单后台线程顺序消费分析队列，逐个加载目标
（作业或案件）的结果 JSON，扁平化会话森林后交给 timing 引擎核验，
结果独立落盘到 ``DATA_DIR/analyses/<analysis_id>/``——删除源作业/案件
不影响已完成分析的读取；服务重启时未完成的分析自动重新入队。
"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Any

from . import config, jsonio, timing
from .storage import Storage, utc_now


class TimingProcessor:
    """单后台线程顺序处理时序核验分析队列。"""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._queue: list[str] = []
        self._cond = threading.Condition()
        self._known: set[str] = set()
        self._thread = threading.Thread(
            target=self._run, name="timing-processor", daemon=True
        )

    def start(self) -> None:
        # 重启恢复：上次未完成的分析重新入队
        for analysis in self._storage.unfinished_analyses():
            self.enqueue(analysis["id"])
        self._thread.start()

    def enqueue(self, analysis_id: str) -> None:
        with self._cond:
            if analysis_id not in self._known:
                self._known.add(analysis_id)
                self._queue.append(analysis_id)
                self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                analysis_id = self._queue.pop(0)
            self._known.discard(analysis_id)
            try:
                self._process(analysis_id)
            except Exception:
                # 兜底：任何意外都落库为 failed，线程本身不能死
                self._storage.fail_analysis(
                    analysis_id,
                    "内部错误:\n" + traceback.format_exc(limit=5),
                )

    # ---------------------------------------------------------- 流水线

    def _process(self, analysis_id: str) -> None:
        analysis = self._storage.get_analysis(analysis_id)
        if analysis is None:
            return
        target_type = analysis["target_type"]
        target_id = analysis["target_id"]

        self._storage.update_analysis_progress(
            analysis_id, status=config.STATUS_PROCESSING, progress=5,
            phase="loading_target",
        )

        # ---- 1. 加载目标结果（目标在处理前被删除则分析失败并说明） ------
        loaded = self._load_target(analysis)
        if loaded is None:
            return  # _load_target 已落库失败原因
        target_meta, threads_forest = loaded

        self._storage.update_analysis_progress(
            analysis_id, progress=30, phase="flattening_threads"
        )

        # ---- 2. 扁平化森林：恢复父子关系与来源描述 ----------------------
        emails = _flatten(threads_forest, target_type, target_id)

        # ---- 3. 时序核验 ------------------------------------------------
        self._storage.update_analysis_progress(
            analysis_id, progress=60, phase="checking_timing"
        )
        outcome = timing.analyze(emails, analysis["thresholds"])

        # ---- 4. 组装结果并独立落盘 --------------------------------------
        self._storage.update_analysis_progress(
            analysis_id, progress=90, phase="writing_result"
        )
        result = {
            "analysis_id": analysis_id,
            "status": config.STATUS_COMPLETED,
            "target_type": target_type,
            "target_id": target_id,
            "target": target_meta,
            "created_at": analysis["created_at"],
            "completed_at": utc_now(),
            "thresholds": analysis["thresholds"],
            "stats": outcome["stats"],
            "findings": outcome["findings"],
            "timeline": outcome["timeline"],
        }

        analysis_dir = Storage.analysis_dir(analysis_id)
        analysis_dir.mkdir(parents=True, exist_ok=True)
        result_path = analysis_dir / "result.json"
        tmp_path = analysis_dir / "result.json.tmp"
        with tmp_path.open("w", encoding="utf-8") as fp:
            jsonio.dump(result, fp, ensure_ascii=False, indent=None)
        tmp_path.replace(result_path)

        self._storage.complete_analysis(
            analysis_id,
            str(result_path),
            email_count=outcome["stats"]["emails"],
            finding_count=outcome["stats"]["findings_total"],
            stats=outcome["stats"],
        )

    def _load_target(
        self, analysis: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """读取目标作业/案件的结果文件；失败时落库原因并返回 None。"""
        analysis_id = analysis["id"]
        target_type = analysis["target_type"]
        target_id = analysis["target_id"]

        if target_type == "job":
            target = self._storage.get_job(target_id)
            label = "作业"
        else:
            target = self._storage.get_case(target_id)
            label = "案件"
        if target is None:
            self._storage.fail_analysis(
                analysis_id,
                f"目标{label}不存在（可能在分析处理前被删除）: {target_id}",
            )
            return None
        if (
            target["status"] != config.STATUS_COMPLETED
            or not target["result_path"]
        ):
            self._storage.fail_analysis(
                analysis_id,
                f"目标{label}未完成: {target_id} "
                f"(当前状态: {target['status']})",
            )
            return None
        result_path = Path(target["result_path"])
        if not result_path.is_file():
            self._storage.fail_analysis(
                analysis_id,
                f"目标{label} {target_id} 的结果文件不存在（可能已被清理）",
            )
            return None
        try:
            result = jsonio.loads(result_path.read_text("utf-8"))
        except Exception as exc:
            self._storage.fail_analysis(
                analysis_id,
                f"目标{label} {target_id} 的结果文件无法解析: {exc}",
            )
            return None

        if target_type == "job":
            meta: dict[str, Any] = {
                "job_id": target_id,
                "original_filename": target["original_filename"],
            }
        else:
            meta = {
                "case_id": target_id,
                "name": target["name"],
                "source_jobs": result.get("source_jobs", []),
            }
        return meta, result.get("threads", [])


def _flatten(
    roots: list[dict[str, Any]], target_type: str, target_id: str
) -> list[dict[str, Any]]:
    """迭代式摊平会话森林，恢复 parent_uid 并附来源描述（uid 顺序）。"""
    emails: list[dict[str, Any]] = []
    # (节点, 父 uid)；逆序压栈保持访问顺序稳定
    stack: list[tuple[dict[str, Any], int | None]] = [
        (root, None) for root in reversed(roots)
    ]
    while stack:
        node, parent_uid = stack.pop()
        if target_type == "job":
            source: dict[str, Any] = {
                "job_id": target_id,
                "source_file": node.get("source_file"),
            }
        else:
            source = {
                "case_id": target_id,
                "sources": node.get("sources", []),
            }
        emails.append(
            {
                "uid": node["uid"],
                "message_id": node.get("message_id"),
                "subject": node.get("subject"),
                "from": node.get("from"),
                "date": node.get("date"),
                "raw_sha256": node.get("raw_sha256"),
                "received": node.get("received") or [],
                "parent_uid": parent_uid,
                "source": source,
            }
        )
        for child in reversed(node.get("children", [])):
            stack.append((child, node["uid"]))
    emails.sort(key=lambda m: m["uid"])
    return emails
