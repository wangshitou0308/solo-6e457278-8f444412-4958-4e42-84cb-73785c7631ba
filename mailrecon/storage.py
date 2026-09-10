"""SQLite 元数据持久化与作业/案件落盘文件管理。

作业目录布局::

    DATA_DIR/jobs/<job_id>/
        upload.bin     # 收到的原始 ZIP（字节不动，便于审计/重放）
        result.json    # 处理完成后的会话结果
        extract/       # 处理期间的受控解压目录，处理后立即删除

案件目录布局（与作业目录完全独立，删除源作业不影响案件结果）::

    DATA_DIR/cases/<case_id>/
        result.json    # 跨包合并完成后的会话森林
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


def _parse_stats(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
