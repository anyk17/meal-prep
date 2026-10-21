"""Database administration: import YAML/fixture data, backup, restore.

    python -m mealplanner.entrypoints.db_cli import --db var/mealplanner.db [--fixture] [--replace-inputs]
    python -m mealplanner.entrypoints.db_cli backup --db var/mealplanner.db --dest var/backup.db
    python -m mealplanner.entrypoints.db_cli restore --src var/backup.db --db var/mealplanner.db [--force]
    python -m mealplanner.entrypoints.db_cli migrate [--db var/mealplanner.db]
    python -m mealplanner.entrypoints.db_cli export --months 2026-10,2026-11 --out <dir> [--db var/mealplanner.db]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from ..adapters.publication_files import FilePublicationWriter
from ..adapters.sqlite.migrations import (
    LATEST_VERSION,
    DatabaseError,
    connect,
    current_version,
    migrate as apply_migrations,
)
from ..adapters.sqlite.store import (
    InputConflict,
    InputImportReport,
    SqlitePlannerInputReader,
    SqlitePlanReadAdapter,
    SqlitePlanRepository,
    SqliteShoppingTripRepository,
    import_planner_inputs,
)
from ..application.publication import PublicationError, PublicationResult, PublicationService
from ..application.shopping_trips import ShoppingTripService
from ..adapters.yaml_inputs import YamlPlannerInputReader
from ..domain.snapshot import normalize_snapshot
from ..domain.weekly_plan import (
    DuplicateWeek,
    PlanRevision,
    RuleViolationError,
    WeeklyPlan,
    input_basis,
    require_weekly_rules,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = PROJECT_ROOT / "var" / "mealplanner.db"  # var/ is git-ignored
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_PLAN_PATH = PROJECT_ROOT / "site/public/plan.json"
DEFAULT_SHOPPING_PATH = PROJECT_ROOT / "site/public/shopping_list.json"
DEFAULT_WEEK_START = date(2026, 10, 19)


def resolve_db_path(value: str | Path | None) -> Path:
    """Pick the DB path (argument, MEALPLANNER_DB, then var/) and keep it out of site/."""
    path = Path(value or os.environ.get("MEALPLANNER_DB") or DEFAULT_DB_PATH).resolve()
    if (PROJECT_ROOT / "site") in (path, *path.parents):
        raise DatabaseError("the database must not live inside site/ (it is published)")
    return path


def import_inputs(
    db_path: Path | str, data_dir: Path | str = DEFAULT_DATA_DIR, *, replace: bool = False
) -> InputImportReport:
    """Import YAML planner inputs. See adapters.sqlite.store.import_planner_inputs for the policy."""
    return import_planner_inputs(db_path, YamlPlannerInputReader(data_dir).read(), replace=replace)


def import_fixture(
    db_path: Path | str,
    plan_path: Path | str = DEFAULT_PLAN_PATH,
    shopping_path: Path | str = DEFAULT_SHOPPING_PATH,
    week_start: date = DEFAULT_WEEK_START,
    timezone_name: str = "America/Bogota",
    now: datetime | None = None,
) -> str:
    """Import the JSON fixture as revision 1 of its week. Returns "imported" or "skipped".

    A week that already has a plan is never touched. The fixture must satisfy the weekly
    rules (RuleViolationError otherwise). Requires imported inputs
    (the pantry and input basis come from the database).
    """
    repo = SqlitePlanRepository(db_path)
    if repo.get_plan_by_week(week_start) is not None:
        return "skipped"
    inputs = SqlitePlannerInputReader(db_path).read()
    snapshot = normalize_snapshot(
        json.loads(Path(plan_path).read_text(encoding="utf-8")),
        json.loads(Path(shopping_path).read_text(encoding="utf-8")),
        set(inputs.pantry),
        week_start,
        timezone_name,
    )
    require_weekly_rules(snapshot, inputs)  # RuleViolationError: the fixture is not imported
    plan_id = snapshot.plan["plan_id"]
    revision = PlanRevision(
        plan_id=plan_id,
        revision=1,
        snapshot=snapshot,
        pantry=tuple(inputs.pantry),
        input_basis=input_basis(inputs),
        created_at=now or datetime.now(timezone.utc),
        source="import",
    )
    try:
        repo.create_plan(WeeklyPlan(plan_id, week_start, timezone_name, 1), revision)
    except DuplicateWeek:  # lost a race with another importer
        return "skipped"
    return "imported"


def backup(db_path: Path | str, dest: Path | str) -> Path:
    """Consistent online copy (sqlite3 backup API). Refuses to overwrite dest."""
    dest = Path(dest)
    if dest.exists():
        raise DatabaseError(f"backup destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(db_path)) as src, closing(sqlite3.connect(str(dest))) as dst:
        src.backup(dst)
    return dest


def restore(src: Path | str, db_path: Path | str, *, force: bool = False) -> Path:
    """Restore a backup to db_path (refuses to replace an existing DB without force).

    The backup is integrity-checked first and copied to a temp file that is then
    atomically moved into place, so a failed restore never leaves a partial DB.
    """
    src, db_path = Path(src), Path(db_path)
    if not src.is_file():
        raise DatabaseError(f"backup not found: {src}")
    if db_path.exists() and not force:
        raise DatabaseError(f"{db_path} exists; pass --force to replace it")
    with closing(sqlite3.connect(f"file:{src}?mode=ro", uri=True)) as check:
        check.row_factory = sqlite3.Row
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DatabaseError("backup failed integrity_check")
        version = current_version(check)
        if not 1 <= version <= LATEST_VERSION:
            raise DatabaseError(f"backup has unsupported schema version {version}")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = db_path.with_name(db_path.name + ".restore-tmp")
        tmp.unlink(missing_ok=True)
        with closing(sqlite3.connect(str(tmp))) as out:
            check.backup(out)
    for suffix in ("-wal", "-shm"):  # stale sidecars belong to the replaced DB
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    os.replace(tmp, db_path)
    return db_path


def migrate_database(db_path: Path | str) -> list[int]:
    """Apply pending schema migrations to an existing database; returns the versions applied."""
    path = Path(db_path)
    if not path.is_file():
        raise DatabaseError(f"database not found: {path} (use the import command to create one)")
    with closing(connect(path)) as conn:
        version = current_version(conn)
        if version > LATEST_VERSION:
            raise DatabaseError(
                f"database schema version {version} is newer than supported {LATEST_VERSION}"
            )
        return apply_migrations(conn)


def full_bundle_validator() -> Callable[[Path], Sequence[str]]:
    """The repository's JSON-schema bundle validator (scripts/validate_publication_contract.py).

    Needs a repository checkout and the contract requirements (jsonschema, PyYAML).
    """
    scripts = PROJECT_ROOT / "scripts"
    if not (scripts / "validate_publication_contract.py").is_file():
        raise DatabaseError("scripts/validate_publication_contract.py not found; run from a checkout")
    sys.path.insert(0, str(scripts))
    try:
        import validate_publication_contract as contract
    except ImportError as exc:
        raise DatabaseError(
            f"cannot load the bundle validator ({exc}); install scripts/requirements-contract.txt"
        ) from exc
    checks = contract.api.Checks()
    openapi, schema, _, resolver = contract.validate_schema_documents(checks)
    if checks.errors:
        raise DatabaseError("publication contract files are invalid: " + "; ".join(checks.errors))
    return lambda root: contract.validate_bundle(root, openapi, schema, resolver)


def export_publication(
    db_path: Path | str,
    out_dir: Path | str,
    months: Sequence[str],
    timezone_name: str,
    *,
    clock: Callable[[], datetime] | None = None,
    validate: Callable[[Path], Sequence[str]] | None = None,
) -> PublicationResult:
    """Export the current plans and shopping trips of the selected months into out_dir."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    reader = SqlitePlanReadAdapter(db_path)  # first: DatabaseError for a missing/unmigrated DB, never creates one
    plans = SqlitePlanRepository(db_path)
    service = PublicationService(
        reader,
        ShoppingTripService(SqliteShoppingTripRepository(db_path), plans, clock),
        clock,
        timezone_name,
        FilePublicationWriter(out_dir, validate),
    )
    return service.export(months)


def _print_export(result: PublicationResult, out_dir: Path) -> None:
    print(f"publication_id: {result.publication_id}")
    print(f"exported_at: {result.exported_at}  months: {', '.join(result.selected_months)}")
    print(f"plans included: {len(result.plans)}")
    for plan in result.plans:
        print(f"  {plan['plan_id']}  {plan['week_start']}..{plan['week_end']}  revision {plan['revision']}")
    print(f"trips included: {len(result.trips_included)}")
    for trip in result.trips_included:
        print(f"  {trip['trip_id']}  {trip['status']}  {trip['name']}")
    print(f"trips skipped: {len(result.trips_skipped)}")
    for skipped in result.trips_skipped:
        print(f"  {skipped.trip_id}  {skipped.name}: {skipped.reason}")
    print(
        f"next: copy the contents of {out_dir} into the Pages site's publication/ directory "
        "(index.json last), then commit and push."
    )


def _print_report(report: InputImportReport) -> None:
    print(
        f"inputs: {len(report.added)} added, {len(report.unchanged)} unchanged, "
        f"{len(report.replaced)} replaced"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mealplanner.entrypoints.db_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("import", help="import YAML planner inputs (and optionally the fixture)")
    p.add_argument("--db", help="database path (default: $MEALPLANNER_DB or var/mealplanner.db)")
    p.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    p.add_argument("--replace-inputs", action="store_true", help="overwrite differing records")
    p.add_argument("--fixture", action="store_true", help="also import site/public plan JSON")
    p.add_argument("--plan", default=str(DEFAULT_PLAN_PATH))
    p.add_argument("--shopping", default=str(DEFAULT_SHOPPING_PATH))
    p.add_argument(
        "--week-start",
        default=os.environ.get("MEALPLANNER_WEEK_START", DEFAULT_WEEK_START.isoformat()),
    )
    p.add_argument(
        "--timezone", default=os.environ.get("MEALPLANNER_TIMEZONE", "America/Bogota")
    )
    b = sub.add_parser("backup", help="write a consistent online copy")
    b.add_argument("--db")
    b.add_argument("--dest", required=True)
    r = sub.add_parser("restore", help="restore a backup")
    r.add_argument("--src", required=True)
    r.add_argument("--db")
    r.add_argument("--force", action="store_true")
    m = sub.add_parser("migrate", help="apply pending schema migrations to an existing database")
    m.add_argument("--db")
    e = sub.add_parser("export", help="write a static publication bundle for the selected months")
    e.add_argument("--db")
    e.add_argument("--months", required=True, help="comma-separated YYYY-MM list, e.g. 2026-10,2026-11")
    e.add_argument("--out", required=True, help="output directory (becomes the site's publication/)")
    e.add_argument(
        "--timezone", default=os.environ.get("MEALPLANNER_TIMEZONE", "America/Bogota")
    )
    e.add_argument(
        "--skip-schema-validation",
        action="store_true",
        help="skip the JSON-schema bundle validator (coherence checks still run)",
    )
    args = parser.parse_args(argv)
    try:
        db = resolve_db_path(args.db)
        if args.command == "import":
            _print_report(import_inputs(db, args.data_dir, replace=args.replace_inputs))
            if args.fixture:
                week = date.fromisoformat(args.week_start)
                if week.weekday() != 0:
                    raise DatabaseError("--week-start must be a Monday")
                outcome = import_fixture(db, args.plan, args.shopping, week, args.timezone)
                print(f"fixture {week.isoformat()}: {outcome}")
        elif args.command == "migrate":
            applied = migrate_database(db)
            if applied:
                print(f"migrated {db}: applied versions {', '.join(map(str, applied))}")
            else:
                print(f"{db} is already at schema version {LATEST_VERSION}")
        elif args.command == "export":
            months = [part.strip() for part in args.months.split(",") if part.strip()]
            validator = None if args.skip_schema_validation else full_bundle_validator()
            out_dir = Path(args.out)
            _print_export(export_publication(db, out_dir, months, args.timezone, validate=validator), out_dir)
        elif args.command == "backup":
            print(f"backup written: {backup(db, args.dest)}")
        else:
            print(f"restored: {restore(args.src, db, force=args.force)}")
    except InputConflict as exc:
        print("error: " + str(exc), file=sys.stderr)
        for name in exc.conflicts:
            print(f"  conflict: {name}", file=sys.stderr)
        return 2
    except RuleViolationError as exc:
        print("error: fixture rejected: " + str(exc), file=sys.stderr)
        for violation in exc.violations:
            print(f"  {violation.rule}: {violation.message}", file=sys.stderr)
        return 1
    except PublicationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        for detail in exc.errors:
            print(f"  {detail}", file=sys.stderr)
        return 1
    except (DatabaseError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, DatabaseError) and "schema version" in str(exc):
            print("hint: run `python -m mealplanner.entrypoints.db_cli migrate` first", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
