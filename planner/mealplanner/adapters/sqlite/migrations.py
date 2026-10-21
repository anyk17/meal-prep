"""SQLite connection handling, transactions, and schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

BUSY_TIMEOUT_MS = 5000


class DatabaseError(Exception):
    """The database file is missing, unmigrated, or newer than this code."""


# ---------------------------------------------------------------- migrations

# Each migration is (version, name, statements). Statements are separate strings
# because triggers contain semicolons and sqlite3.executescript would commit.
MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "initial schema",
        (
            # -- planner-input catalog (validated pydantic content as JSON per record)
            """CREATE TABLE recipes (
                recipe_id TEXT PRIMARY KEY,
                position INTEGER NOT NULL,
                content TEXT NOT NULL CHECK (json_valid(content))
            )""",
            """CREATE TABLE ingredients (
                ingredient_id TEXT PRIMARY KEY,
                position INTEGER NOT NULL,
                content TEXT NOT NULL CHECK (json_valid(content))
            )""",
            """CREATE TABLE rules (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                content TEXT NOT NULL CHECK (json_valid(content))
            )""",
            """CREATE TABLE pantry (
                ingredient_id TEXT PRIMARY KEY,
                position INTEGER NOT NULL
            )""",
            # -- plans
            """CREATE TABLE plans (
                plan_id TEXT PRIMARY KEY,
                household TEXT NOT NULL DEFAULT 'default',
                week_start TEXT NOT NULL,
                timezone TEXT NOT NULL,
                current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
                UNIQUE (household, week_start),
                FOREIGN KEY (plan_id, current_revision)
                    REFERENCES plan_revisions (plan_id, revision)
                    DEFERRABLE INITIALLY DEFERRED
            )""",
            """CREATE TABLE plan_revisions (
                plan_id TEXT NOT NULL REFERENCES plans (plan_id) DEFERRABLE INITIALLY DEFERRED,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                snapshot_id TEXT NOT NULL,
                plan_json TEXT NOT NULL CHECK (json_valid(plan_json)),
                shopping_json TEXT NOT NULL CHECK (json_valid(shopping_json)),
                pantry_json TEXT NOT NULL CHECK (json_valid(pantry_json)),
                input_basis TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (plan_id, revision)
            )""",
            """CREATE TRIGGER plan_revisions_no_update BEFORE UPDATE ON plan_revisions
               BEGIN SELECT RAISE(ABORT, 'plan_revisions rows are immutable'); END""",
            """CREATE TRIGGER plan_revisions_no_delete BEFORE DELETE ON plan_revisions
               BEGIN SELECT RAISE(ABORT, 'plan_revisions rows are immutable'); END""",
            """CREATE TABLE assignments (
                plan_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                assignment_id TEXT NOT NULL,
                date TEXT NOT NULL,
                meal TEXT NOT NULL CHECK (meal IN ('lunch', 'dinner')),
                recipe_id TEXT NOT NULL,
                variant_id TEXT NOT NULL,
                content TEXT NOT NULL CHECK (json_valid(content)),
                PRIMARY KEY (plan_id, revision, assignment_id),
                UNIQUE (plan_id, revision, date, meal),
                FOREIGN KEY (plan_id, revision) REFERENCES plan_revisions (plan_id, revision)
            )""",
            """CREATE TRIGGER assignments_no_update BEFORE UPDATE ON assignments
               BEGIN SELECT RAISE(ABORT, 'assignments rows are immutable'); END""",
            """CREATE TRIGGER assignments_no_delete BEFORE DELETE ON assignments
               BEGIN SELECT RAISE(ABORT, 'assignments rows are immutable'); END""",
            """CREATE TABLE proposals (
                proposal_id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL REFERENCES plans (plan_id),
                base_revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                body TEXT NOT NULL CHECK (json_valid(body))
            )""",
            """CREATE TABLE retry_records (
                plan_id TEXT NOT NULL REFERENCES plans (plan_id),
                retry_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                result_revision INTEGER NOT NULL,
                changed INTEGER NOT NULL CHECK (changed IN (0, 1)),
                PRIMARY KEY (plan_id, retry_id)
            )""",
        ),
    ),
)
LATEST_VERSION = MIGRATIONS[-1][0]


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with foreign keys, WAL, and a busy timeout.

    Autocommit mode: transactions are explicit (BEGIN IMMEDIATE ... COMMIT).
    """
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _utcnow_text() -> str:
    from datetime import timezone

    return datetime.now(timezone.utc).isoformat()


def current_version(conn: sqlite3.Connection) -> int:
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not has_table:
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"] or 0)


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order, each in its own transaction. Idempotent.

    Returns the versions applied by this call.
    """
    applied: list[int] = []
    if current_version(conn) >= LATEST_VERSION:
        return applied
    for version, name, statements in MIGRATIONS:
        with transaction(conn):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            # Re-check under the write lock: a concurrent process may have applied it.
            if version <= current_version(conn):
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, _utcnow_text()),
            )
            applied.append(version)
    return applied


def open_database(path: Path | str, *, create: bool = True) -> sqlite3.Connection:
    """Connect and migrate. With create=False a missing file raises DatabaseError."""
    path = Path(path)
    if not create and not path.is_file():
        raise DatabaseError(f"database not found: {path}")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        version = current_version(conn)
        if version > LATEST_VERSION:
            raise DatabaseError(
                f"database schema version {version} is newer than supported {LATEST_VERSION}"
            )
        if create:
            migrate(conn)
        elif version != LATEST_VERSION:
            raise DatabaseError(f"database schema version {version} is not {LATEST_VERSION}")
    except BaseException:
        conn.close()
        raise
    return conn
