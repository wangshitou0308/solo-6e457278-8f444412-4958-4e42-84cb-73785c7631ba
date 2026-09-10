"""SQLite 元数据持久化与作业/案件落盘文件管理。

作业目录布局::

    DATA_DIR/jobs/<job_id>/
        upload.bin     # 收到的原始 ZIP（字节不动，便于审计/重放）
        result.json    # 处理完成后的会话结果
        extract/       # 处理期间的受控解压目录，处理后立即删除

案件目录布局（与作业目录完全独立，删除源作业不影响案件结果）::

    DATA_DIR/cases/<case_id>/
        result.json    # 跨包合并完成后的会话森林

时序核验分析目录布局（同样独立落盘，删除源作业/案件不影响已完成分析）::

    DATA_DIR/analyses/<analysis_id>/
        result.json    # 时序核验结论与时间线

声明身份核验目录布局（独立落盘，删除源作业/案件不影响已完成核验）::

    DATA_DIR/identity_checks/<check_id>/
        result.json    # 声明身份发现、待复核证据、按邮件/会话汇总

附件流转追踪目录布局（独立落盘，删除源作业/案件不影响已完成追踪）::

    DATA_DIR/attachment_flows/<flow_id>/
        result.json    # 流转事件、待复核项、内容台账与按邮件汇总
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    original_filename TEXT,
    idempotency_key   TEXT,
    idempotency_token TEXT,
    progress          INTEGER NOT NULL DEFAULT 0,
    phase             TEXT,
    error             TEXT,
    email_count       INTEGER NOT NULL DEFAULT 0,
    thread_count      INTEGER NOT NULL DEFAULT 0,
    stats_json        TEXT,
    upload_path       TEXT,
    result_path       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency
    ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS cases (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    name              TEXT,
    progress          INTEGER NOT NULL DEFAULT 0,
    phase             TEXT,
    error             TEXT,
    job_ids_json      TEXT NOT NULL,
    email_count       INTEGER NOT NULL DEFAULT 0,
    thread_count      INTEGER NOT NULL DEFAULT 0,
    stats_json        TEXT,
    result_path       TEXT
);
CREATE TABLE IF NOT EXISTS analyses (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    target_type       TEXT NOT NULL,
    target_id         TEXT NOT NULL,
    thresholds_json   TEXT NOT NULL,
    progress          INTEGER NOT NULL DEFAULT 0,
    phase             TEXT,
    error             TEXT,
    email_count       INTEGER NOT NULL DEFAULT 0,
    finding_count     INTEGER NOT NULL DEFAULT 0,
    stats_json        TEXT,
    result_path       TEXT
);
CREATE TABLE IF NOT EXISTS identity_checks (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    target_type       TEXT NOT NULL,
    target_id         TEXT NOT NULL,
    progress          INTEGER NOT NULL DEFAULT 0,
    phase             TEXT,
    error             TEXT,
    email_count       INTEGER NOT NULL DEFAULT 0,
    finding_count     INTEGER NOT NULL DEFAULT 0,
    review_count      INTEGER NOT NULL DEFAULT 0,
    stats_json        TEXT,
    result_path       TEXT
);
CREATE TABLE IF NOT EXISTS attachment_flows (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    target_type       TEXT NOT NULL,
    target_id         TEXT NOT NULL,
    progress          INTEGER NOT NULL DEFAULT 0,
    phase             TEXT,
    error             TEXT,
    email_count       INTEGER NOT NULL DEFAULT 0,
    event_count       INTEGER NOT NULL DEFAULT 0,
    review_count      INTEGER NOT NULL DEFAULT 0,
    stats_json        TEXT,
    result_path       TEXT
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    """线程安全的 SQLite 封装；工作线程与 HTTP 线程共享一个实例。"""

    def __init__(self, db_path: Path | None = None) -> None:
        db_path = db_path or (config.DATA_DIR / "mailrecon.db")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)

    # ---------------------------------------------------------- 作业目录

    @staticmethod
    def job_dir(job_id: str) -> Path:
        return config.DATA_DIR / "jobs" / job_id

    @classmethod
    def prepare_job_dir(cls, job_id: str) -> Path:
        path = cls.job_dir(job_id)
        path.mkdir(parents=True, exist_ok=True)
        (path / "extract").mkdir(exist_ok=True)
        return path

    @classmethod
    def cleanup_job_files(cls, job_id: str) -> None:
        """删除作业的全部落盘文件（含残留的解压目录）。"""
        import shutil

        path = cls.job_dir(job_id)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    # ---------------------------------------------------------- CRUD

    def create_job(
        self,
        job_id: str,
        original_filename: str | None,
        idempotency_key: str | None,
        idempotency_token: str | None,
        upload_path: str,
    ) -> None:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jobs (id, status, created_at, updated_at,
                                  original_filename, idempotency_key,
                                  idempotency_token, upload_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    config.STATUS_QUEUED,
                    now,
                    now,
                    original_filename,
                    idempotency_key,
                    idempotency_token,
                    upload_path,
                ),
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def get_job_by_idempotency(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def update_progress(
        self,
        job_id: str,
        status: str | None = None,
        progress: int | None = None,
        phase: str | None = None,
    ) -> None:
        sets = ["updated_at = ?"]
        params: list[Any] = [utc_now()]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if progress is not None:
            sets.append("progress = ?")
            params.append(progress)
        if phase is not None:
            sets.append("phase = ?")
            params.append(phase)
        params.append(job_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params
            )

    def complete_job(
        self,
        job_id: str,
        result_path: str,
        email_count: int,
        thread_count: int,
        stats: dict[str, Any],
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs SET status = ?, updated_at = ?, progress = 100,
                                phase = ?, error = NULL, email_count = ?,
                                thread_count = ?, stats_json = ?,
                                result_path = ?
                WHERE id = ?
                """,
                (
                    config.STATUS_COMPLETED,
                    utc_now(),
                    "completed",
                    email_count,
                    thread_count,
                    json.dumps(stats, ensure_ascii=False),
                    result_path,
                    job_id,
                ),
            )

    def fail_job(self, job_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs SET status = ?, updated_at = ?, phase = ?,
                                error = ? WHERE id = ?
                """,
                (config.STATUS_FAILED, utc_now(), "failed", error, job_id),
            )

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM jobs WHERE id = ?", (job_id,)
            )
            return cursor.rowcount > 0

    def processing_jobs(self) -> list[dict[str, Any]]:
        """服务重启后找出卡在 processing/queued 的作业。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status IN (?, ?)",
                (config.STATUS_PROCESSING, config.STATUS_QUEUED),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    # ---------------------------------------------------------- 案件目录

    @staticmethod
    def case_dir(case_id: str) -> Path:
        return config.DATA_DIR / "cases" / case_id

    @classmethod
    def prepare_case_dir(cls, case_id: str) -> Path:
        path = cls.case_dir(case_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---------------------------------------------------------- 案件 CRUD

    def create_case(
        self,
        case_id: str,
        name: str | None,
        job_ids: list[str],
    ) -> None:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cases (id, status, created_at, updated_at,
                                   name, job_ids_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    config.STATUS_QUEUED,
                    now,
                    now,
                    name,
                    json.dumps(job_ids),
                ),
            )

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cases WHERE id = ?", (case_id,)
            ).fetchone()
        return _case_row_to_dict(row) if row else None

    def update_case_progress(
        self,
        case_id: str,
        status: str | None = None,
        progress: int | None = None,
        phase: str | None = None,
    ) -> None:
        sets = ["updated_at = ?"]
        params: list[Any] = [utc_now()]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if progress is not None:
            sets.append("progress = ?")
            params.append(progress)
        if phase is not None:
            sets.append("phase = ?")
            params.append(phase)
        params.append(case_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE cases SET {', '.join(sets)} WHERE id = ?", params
            )

    def complete_case(
        self,
        case_id: str,
        result_path: str,
        email_count: int,
        thread_count: int,
        stats: dict[str, Any],
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE cases SET status = ?, updated_at = ?, progress = 100,
                                 phase = ?, error = NULL, email_count = ?,
                                 thread_count = ?, stats_json = ?,
                                 result_path = ?
                WHERE id = ?
                """,
                (
                    config.STATUS_COMPLETED,
                    utc_now(),
                    "completed",
                    email_count,
                    thread_count,
                    json.dumps(stats, ensure_ascii=False),
                    result_path,
                    case_id,
                ),
            )

    def fail_case(self, case_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE cases SET status = ?, updated_at = ?, phase = ?,
                                 error = ? WHERE id = ?
                """,
                (config.STATUS_FAILED, utc_now(), "failed", error, case_id),
            )

    def unfinished_cases(self) -> list[dict[str, Any]]:
        """服务重启后找出卡在 processing/queued 的案件。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cases WHERE status IN (?, ?)",
                (config.STATUS_PROCESSING, config.STATUS_QUEUED),
            ).fetchall()
        return [_case_row_to_dict(row) for row in rows]

    # ---------------------------------------------------------- 分析目录

    @staticmethod
    def analysis_dir(analysis_id: str) -> Path:
        return config.DATA_DIR / "analyses" / analysis_id

    @classmethod
    def prepare_analysis_dir(cls, analysis_id: str) -> Path:
        path = cls.analysis_dir(analysis_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---------------------------------------------------------- 分析 CRUD

    def create_analysis(
        self,
        analysis_id: str,
        target_type: str,
        target_id: str,
        thresholds: dict[str, int],
    ) -> None:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO analyses (id, status, created_at, updated_at,
                                      target_type, target_id, thresholds_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis_id,
                    config.STATUS_QUEUED,
                    now,
                    now,
                    target_type,
                    target_id,
                    json.dumps(thresholds),
                ),
            )

    def get_analysis(self, analysis_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM analyses WHERE id = ?", (analysis_id,)
            ).fetchone()
        return _analysis_row_to_dict(row) if row else None

    def list_analyses(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM analyses ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_analysis_row_to_dict(row) for row in rows]

    def update_analysis_progress(
        self,
        analysis_id: str,
        status: str | None = None,
        progress: int | None = None,
        phase: str | None = None,
    ) -> None:
        sets = ["updated_at = ?"]
        params: list[Any] = [utc_now()]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if progress is not None:
            sets.append("progress = ?")
            params.append(progress)
        if phase is not None:
            sets.append("phase = ?")
            params.append(phase)
        params.append(analysis_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE analyses SET {', '.join(sets)} WHERE id = ?", params
            )

    def complete_analysis(
        self,
        analysis_id: str,
        result_path: str,
        email_count: int,
        finding_count: int,
        stats: dict[str, Any],
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE analyses SET status = ?, updated_at = ?, progress = 100,
                                    phase = ?, error = NULL, email_count = ?,
                                    finding_count = ?, stats_json = ?,
                                    result_path = ?
                WHERE id = ?
                """,
                (
                    config.STATUS_COMPLETED,
                    utc_now(),
                    "completed",
                    email_count,
                    finding_count,
                    json.dumps(stats, ensure_ascii=False),
                    result_path,
                    analysis_id,
                ),
            )

    def fail_analysis(self, analysis_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE analyses SET status = ?, updated_at = ?, phase = ?,
                                    error = ? WHERE id = ?
                """,
                (
                    config.STATUS_FAILED,
                    utc_now(),
                    "failed",
                    error,
                    analysis_id,
                ),
            )

    def unfinished_analyses(self) -> list[dict[str, Any]]:
        """服务重启后找出卡在 processing/queued 的分析。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM analyses WHERE status IN (?, ?)",
                (config.STATUS_PROCESSING, config.STATUS_QUEUED),
            ).fetchall()
        return [_analysis_row_to_dict(row) for row in rows]

    # ------------------------------------------------------ 身份核验目录

    @staticmethod
    def identity_check_dir(check_id: str) -> Path:
        return config.DATA_DIR / "identity_checks" / check_id

    @classmethod
    def prepare_identity_check_dir(cls, check_id: str) -> Path:
        path = cls.identity_check_dir(check_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------ 身份核验 CRUD

    def create_identity_check(
        self,
        check_id: str,
        target_type: str,
        target_id: str,
    ) -> None:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO identity_checks (id, status, created_at, updated_at,
                                             target_type, target_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    check_id,
                    config.STATUS_QUEUED,
                    now,
                    now,
                    target_type,
                    target_id,
                ),
            )

    def get_identity_check(self, check_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM identity_checks WHERE id = ?", (check_id,)
            ).fetchone()
        return _identity_row_to_dict(row) if row else None

    def list_identity_checks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM identity_checks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_identity_row_to_dict(row) for row in rows]

    def update_identity_check_progress(
        self,
        check_id: str,
        status: str | None = None,
        progress: int | None = None,
        phase: str | None = None,
    ) -> None:
        sets = ["updated_at = ?"]
        params: list[Any] = [utc_now()]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if progress is not None:
            sets.append("progress = ?")
            params.append(progress)
        if phase is not None:
            sets.append("phase = ?")
            params.append(phase)
        params.append(check_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE identity_checks SET {', '.join(sets)} WHERE id = ?",
                params,
            )

    def complete_identity_check(
        self,
        check_id: str,
        result_path: str,
        email_count: int,
        finding_count: int,
        review_count: int,
        stats: dict[str, Any],
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE identity_checks SET status = ?, updated_at = ?,
                    progress = 100, phase = ?, error = NULL, email_count = ?,
                    finding_count = ?, review_count = ?, stats_json = ?,
                    result_path = ?
                WHERE id = ?
                """,
                (
                    config.STATUS_COMPLETED,
                    utc_now(),
                    "completed",
                    email_count,
                    finding_count,
                    review_count,
                    json.dumps(stats, ensure_ascii=False),
                    result_path,
                    check_id,
                ),
            )

    def fail_identity_check(self, check_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE identity_checks SET status = ?, updated_at = ?,
                    phase = ?, error = ? WHERE id = ?
                """,
                (
                    config.STATUS_FAILED,
                    utc_now(),
                    "failed",
                    error,
                    check_id,
                ),
            )

    def unfinished_identity_checks(self) -> list[dict[str, Any]]:
        """服务重启后找出卡在 processing/queued 的身份核验。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM identity_checks WHERE status IN (?, ?)",
                (config.STATUS_PROCESSING, config.STATUS_QUEUED),
            ).fetchall()
        return [_identity_row_to_dict(row) for row in rows]

    # ------------------------------------------------------ 附件流转目录

    @staticmethod
    def attachment_flow_dir(flow_id: str) -> Path:
        return config.DATA_DIR / "attachment_flows" / flow_id

    @classmethod
    def prepare_attachment_flow_dir(cls, flow_id: str) -> Path:
        path = cls.attachment_flow_dir(flow_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------ 附件流转 CRUD

    def create_attachment_flow(
        self,
        flow_id: str,
        target_type: str,
        target_id: str,
    ) -> None:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO attachment_flows (id, status, created_at,
                                              updated_at, target_type,
                                              target_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    flow_id,
                    config.STATUS_QUEUED,
                    now,
                    now,
                    target_type,
                    target_id,
                ),
            )

    def get_attachment_flow(self, flow_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM attachment_flows WHERE id = ?", (flow_id,)
            ).fetchone()
        return _flow_row_to_dict(row) if row else None

    def list_attachment_flows(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM attachment_flows ORDER BY created_at DESC "
                "LIMIT ?",
                (limit,),
            ).fetchall()
        return [_flow_row_to_dict(row) for row in rows]

    def update_attachment_flow_progress(
        self,
        flow_id: str,
        status: str | None = None,
        progress: int | None = None,
        phase: str | None = None,
    ) -> None:
        sets = ["updated_at = ?"]
        params: list[Any] = [utc_now()]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if progress is not None:
            sets.append("progress = ?")
            params.append(progress)
        if phase is not None:
            sets.append("phase = ?")
            params.append(phase)
        params.append(flow_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE attachment_flows SET {', '.join(sets)} WHERE id = ?",
                params,
            )

    def complete_attachment_flow(
        self,
        flow_id: str,
        result_path: str,
        email_count: int,
        event_count: int,
        review_count: int,
        stats: dict[str, Any],
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE attachment_flows SET status = ?, updated_at = ?,
                    progress = 100, phase = ?, error = NULL, email_count = ?,
                    event_count = ?, review_count = ?, stats_json = ?,
                    result_path = ?
                WHERE id = ?
                """,
                (
                    config.STATUS_COMPLETED,
                    utc_now(),
                    "completed",
                    email_count,
                    event_count,
                    review_count,
                    json.dumps(stats, ensure_ascii=False),
                    result_path,
                    flow_id,
                ),
            )

    def fail_attachment_flow(self, flow_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE attachment_flows SET status = ?, updated_at = ?,
                    phase = ?, error = ? WHERE id = ?
                """,
                (
                    config.STATUS_FAILED,
                    utc_now(),
                    "failed",
                    error,
                    flow_id,
                ),
            )

    def unfinished_attachment_flows(self) -> list[dict[str, Any]]:
        """服务重启后找出卡在 processing/queued 的附件流转追踪。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM attachment_flows WHERE status IN (?, ?)",
                (config.STATUS_PROCESSING, config.STATUS_QUEUED),
            ).fetchall()
        return [_flow_row_to_dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["stats"] = _parse_stats(data.pop("stats_json", None))
    return data


def _case_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["stats"] = _parse_stats(data.pop("stats_json", None))
    try:
        data["job_ids"] = json.loads(data.pop("job_ids_json"))
    except (json.JSONDecodeError, TypeError):
        data["job_ids"] = []
    return data


def _analysis_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["stats"] = _parse_stats(data.pop("stats_json", None))
    try:
        data["thresholds"] = json.loads(data.pop("thresholds_json"))
    except (json.JSONDecodeError, TypeError):
        data["thresholds"] = {}
    return data


def _identity_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["stats"] = _parse_stats(data.pop("stats_json", None))
    return data


def _flow_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["stats"] = _parse_stats(data.pop("stats_json", None))
    return data


def _parse_stats(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
