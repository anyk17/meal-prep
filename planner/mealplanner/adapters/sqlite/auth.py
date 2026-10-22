"""SQLite storage for household users and login sessions (migration 5)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from ...domain.auth import SessionRecord, User, UsernameTaken
from .migrations import connect, open_database, transaction


def _user(row: sqlite3.Row) -> User:
    return User(
        row["user_id"], row["username"], row["password_hash"],
        datetime.fromisoformat(row["created_at"]), bool(row["disabled"]),
    )


def _session(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        row["session_hash"], row["user_id"], datetime.fromisoformat(row["created_at"]),
        datetime.fromisoformat(row["last_seen_at"]), datetime.fromisoformat(row["expires_at"]),
    )


class _Base:
    def __init__(self, db_path: Path | str) -> None:
        open_database(db_path).close()  # creates/migrates, like the other SQLite repositories
        self._path = db_path

    def _conn(self) -> closing[sqlite3.Connection]:
        return closing(connect(self._path))


class SqliteUserRepository(_Base):
    def add(self, user: User) -> None:
        try:
            with self._conn() as conn, transaction(conn):
                conn.execute(
                    "INSERT INTO users (user_id, username, password_hash, created_at, disabled) VALUES (?, ?, ?, ?, ?)",
                    (user.user_id, user.username, user.password_hash, user.created_at.isoformat(), int(user.disabled)),
                )
        except sqlite3.IntegrityError as exc:
            raise UsernameTaken(f"username already exists: {user.username}") from exc

    def get_by_username(self, username: str) -> User | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return _user(row) if row else None

    def get_by_id(self, user_id: str) -> User | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return _user(row) if row else None

    def list(self) -> list[User]:
        with self._conn() as conn:
            return [_user(r) for r in conn.execute("SELECT * FROM users ORDER BY created_at, rowid")]

    def set_password_hash(self, user_id: str, password_hash: str) -> None:
        with self._conn() as conn, transaction(conn):
            conn.execute("UPDATE users SET password_hash = ? WHERE user_id = ?", (password_hash, user_id))

    def set_disabled(self, user_id: str, disabled: bool) -> None:
        with self._conn() as conn, transaction(conn):
            conn.execute("UPDATE users SET disabled = ? WHERE user_id = ?", (int(disabled), user_id))

    def delete(self, user_id: str) -> None:
        with self._conn() as conn, transaction(conn):
            conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))  # sessions cascade


class SqliteSessionRepository(_Base):
    def add(self, session: SessionRecord) -> None:
        with self._conn() as conn, transaction(conn):
            conn.execute(
                "INSERT INTO sessions (session_hash, user_id, created_at, last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (session.session_hash, session.user_id, session.created_at.isoformat(),
                 session.last_seen_at.isoformat(), session.expires_at.isoformat()),
            )

    def get(self, session_hash: str) -> SessionRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_hash = ?", (session_hash,)).fetchone()
        return _session(row) if row else None

    def touch(self, session_hash: str, seen_at: datetime) -> None:
        with self._conn() as conn, transaction(conn):
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE session_hash = ?", (seen_at.isoformat(), session_hash))

    def delete(self, session_hash: str) -> None:
        with self._conn() as conn, transaction(conn):
            conn.execute("DELETE FROM sessions WHERE session_hash = ?", (session_hash,))

    def delete_for_user(self, user_id: str) -> None:
        with self._conn() as conn, transaction(conn):
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def delete_expired(self, now: datetime, idle_before: datetime) -> None:
        # Timestamps are tz-aware ISO strings; compare as instants in Python (offsets may differ).
        with self._conn() as conn, transaction(conn):
            for row in conn.execute("SELECT session_hash, last_seen_at, expires_at FROM sessions").fetchall():
                if datetime.fromisoformat(row["expires_at"]) <= now or datetime.fromisoformat(row["last_seen_at"]) < idle_before:
                    conn.execute("DELETE FROM sessions WHERE session_hash = ?", (row["session_hash"],))
