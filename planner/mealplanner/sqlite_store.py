"""SQLite storage: migrations, plan workflow repository, planner inputs, and read adapter.

Stdlib ``sqlite3`` only. Every operation opens its own short-lived connection
(foreign keys on, WAL, busy timeout), so instances are safe to share between
threads and a "restart" is simply a new instance on the same file.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import Ingredient, Recipe, Rules
from .plan_domain import (
    DuplicateWeek,
    PlanRevision,
    Proposal,
    RetryRecord,
    SlotChange,
    StaleRevision,
    WeeklyPlan,
)
from .planner_inputs import PlannerInputs
from .read_api import JsonObject, PlanNotFound
from .snapshot import PlanSnapshot

DEFAULT_HOUSEHOLD = "default"
BUSY_TIMEOUT_MS = 5000


class DatabaseError(Exception):
    """The database file is missing, unmigrated, or newer than this code."""


class PlannerInputsMissing(DatabaseError):
    """The database holds no planner inputs (run the import first)."""


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


# ---------------------------------------------------------------- (de)serialisation


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dt(value: datetime) -> str:
    return value.isoformat()


def _revision_from_row(row: sqlite3.Row) -> PlanRevision:
    return PlanRevision(
        plan_id=row["plan_id"],
        revision=row["revision"],
        snapshot=PlanSnapshot(
            plan=json.loads(row["plan_json"]), shopping_list=json.loads(row["shopping_json"])
        ),
        pantry=tuple(json.loads(row["pantry_json"])),
        input_basis=row["input_basis"],
        created_at=datetime.fromisoformat(row["created_at"]),
        source=row["source"],
    )


def _insert_revision(conn: sqlite3.Connection, rev: PlanRevision) -> None:
    plan = rev.snapshot.plan
    conn.execute(
        "INSERT INTO plan_revisions (plan_id, revision, snapshot_id, plan_json, shopping_json,"
        " pantry_json, input_basis, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rev.plan_id,
            rev.revision,
            plan["snapshot_id"],
            _dumps(plan),
            _dumps(rev.snapshot.shopping_list),
            _dumps(list(rev.pantry)),
            rev.input_basis,
            rev.source,
            _dt(rev.created_at),
        ),
    )
    conn.executemany(
        "INSERT INTO assignments (plan_id, revision, assignment_id, date, meal, recipe_id,"
        " variant_id, content) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                rev.plan_id,
                rev.revision,
                slot["assignment_id"],
                slot["date"],
                slot["meal"],
                slot["recipe_id"],
                slot["variant_id"],
                _dumps(slot),
            )
            for slot in plan["slots"]
        ],
    )


def _retry_params(retry: RetryRecord) -> tuple[Any, ...]:
    return (
        retry.plan_id,
        retry.retry_id,
        retry.fingerprint,
        retry.result_revision,
        int(retry.changed),
    )


_RETRY_SQL = (
    "INSERT OR REPLACE INTO retry_records"
    " (plan_id, retry_id, fingerprint, result_revision, changed) VALUES (?, ?, ?, ?, ?)"
)


def _change_to_json(change: SlotChange) -> JsonObject:
    return {
        "assignment_id": change.assignment_id,
        "date": change.date,
        "meal": change.meal,
        "kind": change.kind,
        "before": change.before,
        "after": change.after,
    }


def _proposal_body(p: Proposal) -> str:
    return _dumps(
        {
            "base_snapshot_id": p.base_snapshot_id,
            "input_basis": p.input_basis,
            "seed": p.seed,
            "pantry": list(p.pantry),
            "snapshot": {"plan": p.snapshot.plan, "shopping_list": p.snapshot.shopping_list},
            "changes": [_change_to_json(c) for c in p.changes],
        }
    )


def _proposal_from_row(row: sqlite3.Row) -> Proposal:
    body = json.loads(row["body"])
    return Proposal(
        proposal_id=row["proposal_id"],
        plan_id=row["plan_id"],
        base_revision=row["base_revision"],
        base_snapshot_id=body["base_snapshot_id"],
        input_basis=body["input_basis"],
        seed=body["seed"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        pantry=tuple(body["pantry"]),
        snapshot=PlanSnapshot(
            plan=body["snapshot"]["plan"], shopping_list=body["snapshot"]["shopping_list"]
        ),
        changes=tuple(SlotChange(**c) for c in body["changes"]),
    )


def _plan_from_row(row: sqlite3.Row) -> WeeklyPlan:
    return WeeklyPlan(
        plan_id=row["plan_id"],
        week_start=date.fromisoformat(row["week_start"]),
        timezone=row["timezone"],
        current_revision=row["current_revision"],
    )


# ---------------------------------------------------------------- workflow repository


class SqlitePlanRepository:
    """PlanWorkflowRepository backed by a SQLite file (migrated on construction)."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        open_database(self.db_path).close()

    def _conn(self) -> closing[sqlite3.Connection]:
        return closing(connect(self.db_path))

    def create_plan(self, plan: WeeklyPlan, first_revision: PlanRevision) -> None:
        with self._conn() as conn:
            try:
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO plans (plan_id, household, week_start, timezone,"
                        " current_revision) VALUES (?, ?, ?, ?, ?)",
                        (
                            plan.plan_id,
                            DEFAULT_HOUSEHOLD,
                            plan.week_start.isoformat(),
                            plan.timezone,
                            plan.current_revision,
                        ),
                    )
                    _insert_revision(conn, first_revision)
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc) and "plans." in str(exc):
                    raise DuplicateWeek(
                        f"a plan already exists for week {plan.week_start.isoformat()}"
                    ) from exc
                raise

    def get_plan(self, plan_id: str) -> WeeklyPlan | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
        return _plan_from_row(row) if row else None

    def get_plan_by_week(self, week_start: date) -> WeeklyPlan | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM plans WHERE household = ? AND week_start = ?",
                (DEFAULT_HOUSEHOLD, week_start.isoformat()),
            ).fetchone()
        return _plan_from_row(row) if row else None

    def get_revision(self, plan_id: str, revision: int) -> PlanRevision | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM plan_revisions WHERE plan_id = ? AND revision = ?",
                (plan_id, revision),
            ).fetchone()
        return _revision_from_row(row) if row else None

    def list_revisions(self, plan_id: str) -> list[PlanRevision]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM plan_revisions WHERE plan_id = ? ORDER BY revision", (plan_id,)
            ).fetchall()
        return [_revision_from_row(r) for r in rows]

    def append_revision(
        self,
        plan_id: str,
        expected_revision: int,
        new_revision: PlanRevision,
        retry: RetryRecord | None,
    ) -> None:
        if new_revision.plan_id != plan_id or new_revision.revision != expected_revision + 1:
            raise ValueError("revisions must be appended sequentially to the same plan")
        with self._conn() as conn, transaction(conn):
            row = conn.execute(
                "SELECT current_revision FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                raise PlanNotFound(plan_id)
            if row["current_revision"] != expected_revision:
                raise StaleRevision(expected_revision, row["current_revision"])
            _insert_revision(conn, new_revision)
            conn.execute(
                "UPDATE plans SET current_revision = ? WHERE plan_id = ?",
                (new_revision.revision, plan_id),
            )
            if retry is not None:
                conn.execute(_RETRY_SQL, _retry_params(retry))

    def get_retry(self, plan_id: str, retry_id: str) -> RetryRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM retry_records WHERE plan_id = ? AND retry_id = ?",
                (plan_id, retry_id),
            ).fetchone()
        if row is None:
            return None
        return RetryRecord(
            plan_id=row["plan_id"],
            retry_id=row["retry_id"],
            fingerprint=row["fingerprint"],
            result_revision=row["result_revision"],
            changed=bool(row["changed"]),
        )

    def save_retry(self, retry: RetryRecord) -> None:
        with self._conn() as conn, transaction(conn):
            conn.execute(_RETRY_SQL, _retry_params(retry))

    def save_proposal(self, proposal: Proposal) -> None:
        with self._conn() as conn, transaction(conn):
            conn.execute(
                "INSERT OR REPLACE INTO proposals (proposal_id, plan_id, base_revision,"
                " created_at, expires_at, body) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    proposal.proposal_id,
                    proposal.plan_id,
                    proposal.base_revision,
                    _dt(proposal.created_at),
                    _dt(proposal.expires_at),
                    _proposal_body(proposal),
                ),
            )

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        return _proposal_from_row(row) if row else None

    def delete_proposal(self, proposal_id: str) -> None:
        with self._conn() as conn, transaction(conn):
            conn.execute("DELETE FROM proposals WHERE proposal_id = ?", (proposal_id,))

    def count_proposals(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM proposals").fetchone()["n"])


# ---------------------------------------------------------------- read adapter


class SqlitePlanReadAdapter:
    """PlanReadRepository serving each plan's current revision from SQLite."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        open_database(self.db_path, create=False).close()  # fail fast if unusable

    def _rows(self, where: str = "", params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with closing(connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT r.plan_json, r.shopping_json FROM plans p"
                " JOIN plan_revisions r ON r.plan_id = p.plan_id"
                " AND r.revision = p.current_revision" + where,
                params,
            ).fetchall()

    def list_plan_summaries(self) -> list[JsonObject]:
        return [
            PlanSnapshot(json.loads(r["plan_json"]), {}).summary
            for r in self._rows(" ORDER BY p.week_start, p.plan_id")
        ]

    def get_plan(self, plan_id: str) -> JsonObject | None:
        rows = self._rows(" WHERE p.plan_id = ?", (plan_id,))
        return json.loads(rows[0]["plan_json"]) if rows else None

    def get_shopping_list(self, plan_id: str) -> JsonObject | None:
        rows = self._rows(" WHERE p.plan_id = ?", (plan_id,))
        return json.loads(rows[0]["shopping_json"]) if rows else None


# ---------------------------------------------------------------- planner inputs


class SqlitePlannerInputReader:
    """PlannerInputReader over the catalog tables; same PlannerInputs as the YAML reader."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    def read(self) -> PlannerInputs:
        with closing(open_database(self.db_path, create=False)) as conn:
            conn.execute("BEGIN")  # one consistent snapshot across the four tables
            try:
                recipes = conn.execute("SELECT content FROM recipes ORDER BY position").fetchall()
                ingredients = conn.execute(
                    "SELECT ingredient_id, content FROM ingredients ORDER BY position"
                ).fetchall()
                rules = conn.execute("SELECT content FROM rules WHERE id = 1").fetchone()
                pantry = conn.execute(
                    "SELECT ingredient_id FROM pantry ORDER BY position"
                ).fetchall()
            finally:
                conn.execute("COMMIT")
        if rules is None or not recipes or not ingredients:
            raise PlannerInputsMissing("database has no planner inputs; run the import first")
        recipe_models = [Recipe.model_validate_json(r["content"]) for r in recipes]
        return PlannerInputs(
            recipes={r.id: r for r in recipe_models},
            ingredients={
                r["ingredient_id"]: Ingredient.model_validate_json(r["content"])
                for r in ingredients
            },
            rules=Rules.model_validate_json(rules["content"]),
            pantry=tuple(r["ingredient_id"] for r in pantry),
        )


# ---------------------------------------------------------------- input writes


@dataclass(frozen=True)
class InputImportReport:
    added: tuple[str, ...]
    unchanged: tuple[str, ...]
    replaced: tuple[str, ...]
    conflicts: tuple[str, ...]


class InputConflict(Exception):
    """Imported planner inputs differ from household content already in the database."""

    def __init__(self, conflicts: Sequence[str]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__(
            "imported inputs differ from existing database content (use --replace-inputs "
            "to overwrite): " + ", ".join(self.conflicts)
        )


def _canon(model: Any) -> str:
    return _dumps(model.model_dump(mode="json"))


def _merge_table(
    conn: sqlite3.Connection,
    table: str,
    key_col: str,
    label: str,
    incoming: dict[str, Any],
    replace: bool,
    report: dict[str, list[str]],
) -> None:
    existing = {
        r[key_col]: (r["content"], r["position"])
        for r in conn.execute(f"SELECT {key_col}, content, position FROM {table}")
    }
    next_pos = max((p for _, p in existing.values()), default=-1) + 1
    for key, model in incoming.items():
        content = _canon(model)
        name = f"{label}:{key}"
        if key not in existing:
            conn.execute(
                f"INSERT INTO {table} ({key_col}, position, content) VALUES (?, ?, ?)",
                (key, next_pos, content),
            )
            next_pos += 1
            report["added"].append(name)
        elif json.loads(existing[key][0]) == json.loads(content):
            report["unchanged"].append(name)
        elif replace:
            conn.execute(f"UPDATE {table} SET content = ? WHERE {key_col} = ?", (content, key))
            report["replaced"].append(name)
        else:
            report["conflicts"].append(name)


def import_planner_inputs(
    db_path: Path | str, inputs: PlannerInputs, *, replace: bool = False
) -> InputImportReport:
    """Merge validated inputs into the catalog in one transaction.

    New records are added; identical records are skipped; differing records are
    conflicts (nothing is written, InputConflict is raised) unless replace=True.
    Records only present in the database are never touched. The merged catalog
    must itself validate as PlannerInputs, otherwise nothing is written.
    """
    report: dict[str, list[str]] = {"added": [], "unchanged": [], "replaced": [], "conflicts": []}
    with closing(open_database(db_path)) as conn, transaction(conn):
        _merge_table(conn, "recipes", "recipe_id", "recipe", dict(inputs.recipes), replace, report)
        _merge_table(
            conn, "ingredients", "ingredient_id", "ingredient", dict(inputs.ingredients),
            replace, report,
        )  # fmt: skip
        rules_content = _canon(inputs.rules)
        row = conn.execute("SELECT content FROM rules WHERE id = 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO rules (id, content) VALUES (1, ?)", (rules_content,))
            report["added"].append("rules")
        elif json.loads(row["content"]) == json.loads(rules_content):
            report["unchanged"].append("rules")
        elif replace:
            conn.execute("UPDATE rules SET content = ? WHERE id = 1", (rules_content,))
            report["replaced"].append("rules")
        else:
            report["conflicts"].append("rules")
        # Pantry is one logical record: compared as a set, written in YAML order.
        db_pantry = [r["ingredient_id"] for r in conn.execute("SELECT ingredient_id FROM pantry")]
        if not db_pantry and inputs.pantry:
            report["added"].append("pantry")
        elif set(db_pantry) == set(inputs.pantry):
            report["unchanged"].append("pantry")
        elif replace:
            report["replaced"].append("pantry")
        else:
            report["conflicts"].append("pantry")
        if report["conflicts"]:
            raise InputConflict(report["conflicts"])  # rolls back the whole import
        if "pantry" in report["added"] or "pantry" in report["replaced"]:
            conn.execute("DELETE FROM pantry")
            conn.executemany(
                "INSERT INTO pantry (ingredient_id, position) VALUES (?, ?)",
                [(item, i) for i, item in enumerate(inputs.pantry)],
            )
        _validate_catalog(conn)
    return InputImportReport(**{k: tuple(v) for k, v in report.items()})


def _validate_catalog(conn: sqlite3.Connection) -> None:
    recipes = [
        Recipe.model_validate_json(r["content"])
        for r in conn.execute("SELECT content FROM recipes ORDER BY position")
    ]
    PlannerInputs(
        recipes={r.id: r for r in recipes},
        ingredients={
            r["ingredient_id"]: Ingredient.model_validate_json(r["content"])
            for r in conn.execute("SELECT * FROM ingredients ORDER BY position")
        },
        rules=Rules.model_validate_json(
            conn.execute("SELECT content FROM rules WHERE id = 1").fetchone()["content"]
        ),
        pantry=tuple(
            r["ingredient_id"] for r in conn.execute("SELECT * FROM pantry ORDER BY position")
        ),
    )  # raises ValueError if the merged catalog is invalid; transaction rolls back

