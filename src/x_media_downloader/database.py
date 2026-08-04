from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .config import DB_PATH, detect_download_dir
from .models import Analysis, Job, JobStatus, Settings, utc_now


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS analyses (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def save_analysis(self, analysis: Analysis) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO analyses (id, payload, created_at) VALUES (?, ?, ?)",
                (analysis.id, analysis.model_dump_json(), analysis.created_at),
            )

    def get_analysis(self, analysis_id: str) -> Analysis | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM analyses WHERE id = ?", (analysis_id,)
            ).fetchone()
        return Analysis.model_validate_json(row["payload"]) if row else None

    def save_job(self, job: Job) -> None:
        job.updated_at = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR REPLACE INTO jobs (id, payload, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)""",
                (job.id, job.model_dump_json(), job.status.value, job.created_at, job.updated_at),
            )

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return Job.model_validate_json(row["payload"]) if row else None

    def list_jobs(self, limit: int = 50) -> list[Job]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Job.model_validate_json(row["payload"]) for row in rows]

    def recover_jobs(self) -> None:
        for job in self.list_jobs(500):
            if job.status == JobStatus.RUNNING:
                job.status = JobStatus.QUEUED
                job.phase = "Recovered after restart"
                job.speed = None
                job.eta = None
                job.worker_pid = None
                job.worker_pgid = None
                job.heartbeat_at = None
                self.save_job(job)

    def get_settings(self) -> Settings:
        defaults = Settings(download_dir=str(detect_download_dir()))
        with self._lock:
            rows = self._connection.execute("SELECT key, value FROM settings").fetchall()
        values = {row["key"]: json.loads(row["value"]) for row in rows}
        return Settings(**{**defaults.model_dump(), **values})

    def save_settings(self, settings: Settings) -> None:
        with self._lock, self._connection:
            for key, value in settings.model_dump().items():
                self._connection.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
