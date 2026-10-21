"""SQLite storage for publication job records (private; see migration 4)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from ...application.publishing import FAILED, RUNNING, PublishInProgress, PublishJob
from .migrations import connect, open_database, transaction

_COLUMNS = (
    "job_id, months, status, publication_id, commit_sha, run_url, error, plan_tokens,"
    " started_at, finished_at, deployed_at"
)


def _text(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_values(job: PublishJob) -> tuple[object, ...]:
    return (
        job.job_id,
        json.dumps(list(job.months)),
        job.status,
        job.publication_id,
        job.commit_sha,
        job.run_url,
        job.error,
        json.dumps(job.tokens, sort_keys=True),
        _text(job.started_at),
        _text(job.finished_at),
        _text(job.deployed_at),
    )


def _job(row: sqlite3.Row) -> PublishJob:
    return PublishJob(
        job_id=row["job_id"],
        months=tuple(json.loads(row["months"])),
        status=row["status"],
        publication_id=row["publication_id"],
        commit_sha=row["commit_sha"],
        run_url=row["run_url"],
        error=row["error"],
        tokens=json.loads(row["plan_tokens"]),
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=_when(row["finished_at"]),
        deployed_at=_when(row["deployed_at"]),
    )


class SqlitePublishJobRepository:
    """Implements PublishJobRepository. One short-lived connection per call."""

    STALE_ERROR = "The publication did not finish (the app stopped or timed out)."

    def __init__(self, db_path: Path | str) -> None:
        open_database(db_path).close()  # creates/migrates, like the other SQLite repositories
        self._path = db_path

    def _conn(self) -> closing[sqlite3.Connection]:
        return closing(connect(self._path))

    def start(self, job: PublishJob, stale_before: datetime) -> None:
        with self._conn() as conn, transaction(conn):
            for row in conn.execute("SELECT job_id, started_at FROM publication_jobs WHERE status = ?", (RUNNING,)):
                if datetime.fromisoformat(row["started_at"]) < stale_before:
                    conn.execute(
                        "UPDATE publication_jobs SET status = ?, error = ?, finished_at = ? WHERE job_id = ?",
                        (FAILED, self.STALE_ERROR, _text(job.started_at), row["job_id"]),
                    )
                else:
                    raise PublishInProgress("A publication is already running.")
            conn.execute(
                f"INSERT INTO publication_jobs ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _row_values(job),
            )

    def save(self, job: PublishJob) -> None:
        values = _row_values(job)
        with self._conn() as conn, transaction(conn):
            conn.execute(
                "UPDATE publication_jobs SET months = ?, status = ?, publication_id = ?, commit_sha = ?,"
                " run_url = ?, error = ?, plan_tokens = ?, started_at = ?, finished_at = ?, deployed_at = ?"
                " WHERE job_id = ?",
                (*values[1:], values[0]),
            )

    def get(self, job_id: str) -> PublishJob | None:
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM publication_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _job(row) if row else None

    def latest(self) -> PublishJob | None:
        with self._conn() as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM publication_jobs ORDER BY rowid DESC LIMIT 1").fetchone()
        return _job(row) if row else None

    def last_with_status(self, statuses: tuple[str, ...]) -> PublishJob | None:
        marks = ", ".join("?" for _ in statuses)
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM publication_jobs WHERE status IN ({marks}) ORDER BY rowid DESC LIMIT 1",
                statuses,
            ).fetchone()
        return _job(row) if row else None
