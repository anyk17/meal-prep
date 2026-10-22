"""SQLite connection handling, transactions, and schema migrations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

BUSY_TIMEOUT_MS = 5000

# Migration 6 backfills ingredient shelf_life labels from the shipped YAML (in the container
# /app/data); the project root is four levels above this file (…/planner/mealplanner/adapters/sqlite).
INGREDIENT_LABELS_PATH = Path(__file__).resolve().parents[4] / "data" / "ingredients.yml"
SHELF_LIVES = ("long_lasting", "fresh")


class DatabaseError(Exception):
    """The database file is missing, unmigrated, or newer than this code."""


# ---------------------------------------------------------------- migrations

Step = str | Callable[[sqlite3.Connection], None]


def _backfill_shelf_life(conn: sqlite3.Connection) -> None:
    """Add shelf_life to stored ingredients from data/ingredients.yml, failing if any stays unlabeled."""
    import yaml

    rows = conn.execute("SELECT ingredient_id, content FROM ingredients").fetchall()
    if not rows:
        return
    try:
        with open(INGREDIENT_LABELS_PATH, encoding="utf-8") as f:
            shipped = yaml.safe_load(f) or {}
    except OSError as exc:
        raise DatabaseError(
            f"migration 6 needs {INGREDIENT_LABELS_PATH} to label stored ingredients: {exc}"
        ) from exc
    unlabeled: list[str] = []
    for row in rows:
        content = json.loads(row["content"])
        if content.get("shelf_life") in SHELF_LIVES:
            continue
        label = (shipped.get(row["ingredient_id"]) or {}).get("shelf_life")
        if label not in SHELF_LIVES:
            unlabeled.append(row["ingredient_id"])
            continue
        content["shelf_life"] = label
        conn.execute(
            "UPDATE ingredients SET content = ? WHERE ingredient_id = ?",
            (
                json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                row["ingredient_id"],
            ),
        )
    if unlabeled:
        raise DatabaseError(
            "migration 6 cannot label stored ingredients missing from "
            f"{INGREDIENT_LABELS_PATH}: {', '.join(sorted(unlabeled))}"
        )


# Each migration is (version, name, steps). A step is an SQL statement (separate strings
# because triggers contain semicolons and sqlite3.executescript would commit) or a callable
# that receives the connection inside the migration's transaction.
MIGRATIONS: tuple[tuple[int, str, tuple[Step, ...]], ...] = (
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
    (
        2,
        "shopping trips",
        (
            """CREATE TABLE shopping_trips (
                trip_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('open', 'done')),
                version INTEGER NOT NULL CHECK (version >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                CHECK ((status = 'done') = (completed_at IS NOT NULL))
            )""",
            # plan_id is UNIQUE: a week belongs to at most one trip, whatever its status.
            # covered_* are set exactly when the trip is done and point at immutable revisions.
            """CREATE TABLE shopping_trip_weeks (
                trip_id TEXT NOT NULL REFERENCES shopping_trips (trip_id) ON DELETE CASCADE,
                plan_id TEXT NOT NULL UNIQUE REFERENCES plans (plan_id),
                covered_revision INTEGER,
                covered_snapshot_id TEXT,
                PRIMARY KEY (trip_id, plan_id),
                CHECK ((covered_revision IS NULL) = (covered_snapshot_id IS NULL)),
                FOREIGN KEY (plan_id, covered_revision)
                    REFERENCES plan_revisions (plan_id, revision)
            )""",
        ),
    ),
    (
        3,
        "shopping trip names follow their weeks unless chosen",
        (
            # name keeps the displayed name; existing trips were created unnamed, so 0.
            "ALTER TABLE shopping_trips ADD COLUMN name_is_custom INTEGER NOT NULL DEFAULT 0"
            " CHECK (name_is_custom IN (0, 1))",
        ),
    ),
    (
        4,
        "publication jobs",
        (
            # Private record of publish attempts (never part of the public bundle). plan_tokens holds
            # the exported plan/revision/snapshot tokens, trips, content hash and app revision.
            """CREATE TABLE publication_jobs (
                job_id TEXT PRIMARY KEY,
                months TEXT NOT NULL CHECK (json_valid(months)),
                status TEXT NOT NULL CHECK (status IN ('running', 'pushed', 'live', 'failed', 'unchanged')),
                publication_id TEXT,
                commit_sha TEXT,
                run_url TEXT,
                error TEXT,
                plan_tokens TEXT NOT NULL CHECK (json_valid(plan_tokens)),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                deployed_at TEXT
            )""",
        ),
    ),
    (
        5,
        "household login",
        (
            # username is stored NFKC-normalized and lowercased; sessions keep only the SHA-256 of the cookie token.
            """CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1))
            )""",
            """CREATE TABLE sessions (
                session_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )""",
            "CREATE INDEX sessions_user ON sessions (user_id)",
        ),
    ),
    (
        6,
        "long-lasting and fresh shopping: ingredient shelf life, trip membership per group",
        (
            _backfill_shelf_life,
            # Membership becomes (trip, plan, group); (plan, group) is on at most one trip.
            # Existing rows expand to both groups and keep their frozen covered revisions.
            """CREATE TABLE shopping_trip_weeks_v6 (
                trip_id TEXT NOT NULL REFERENCES shopping_trips (trip_id) ON DELETE CASCADE,
                plan_id TEXT NOT NULL REFERENCES plans (plan_id),
                shelf_group TEXT NOT NULL CHECK (shelf_group IN ('long_lasting', 'fresh')),
                covered_revision INTEGER,
                covered_snapshot_id TEXT,
                PRIMARY KEY (trip_id, plan_id, shelf_group),
                UNIQUE (plan_id, shelf_group),
                CHECK ((covered_revision IS NULL) = (covered_snapshot_id IS NULL)),
                FOREIGN KEY (plan_id, covered_revision)
                    REFERENCES plan_revisions (plan_id, revision)
            )""",
            """INSERT INTO shopping_trip_weeks_v6
                   (trip_id, plan_id, shelf_group, covered_revision, covered_snapshot_id)
               SELECT w.trip_id, w.plan_id, g.shelf_group, w.covered_revision, w.covered_snapshot_id
               FROM shopping_trip_weeks w
               CROSS JOIN (SELECT 'long_lasting' AS shelf_group UNION ALL SELECT 'fresh') g""",
            "DROP TABLE shopping_trip_weeks",
            "ALTER TABLE shopping_trip_weeks_v6 RENAME TO shopping_trip_weeks",
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
            for step in statements:
                if callable(step):
                    step(conn)
                else:
                    conn.execute(step)
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
