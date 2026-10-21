"""SQLite store tests: migrations, races, rollback, import policy, backup, read API parity."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from mealplanner.entrypoints.http.app import create_app
from mealplanner.entrypoints.db_cli import backup, import_fixture, import_inputs, restore
from mealplanner.entrypoints.db_cli import main as db_main
from mealplanner.adapters.ortools import OrToolsPlanGenerator
from mealplanner.application.generation import generate
from mealplanner.adapters.json_snapshot import JsonPlanReadRepository
from mealplanner.domain.weekly_plan import (
    DuplicateWeek,
    PlanRevision,
    RetryRecord,
    StaleRevision,
    WeeklyPlan,
    stamp_revision,
)
from mealplanner.application.plan_workflows import PlanWorkflowService
from mealplanner.application.plan_reads import PlanReadService
from mealplanner.adapters.sqlite.migrations import (
    LATEST_VERSION,
    DatabaseError,
    connect,
    migrate,
    open_database,
)
from mealplanner.adapters.sqlite.store import (
    InputConflict,
    PlannerInputsMissing,
    SqlitePlannerInputReader,
    SqlitePlanReadAdapter,
    SqlitePlanRepository,
)
from mealplanner.adapters.yaml_inputs import YamlPlannerInputReader

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
WEEK = date(2026, 10, 19)
PLAN_ID = "plan_2026_10_19"
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
YAML_INPUTS = YamlPlannerInputReader(DATA).read()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "meal.db"


@pytest.fixture
def imported(db: Path) -> Path:
    import_inputs(db, DATA)
    assert import_fixture(db, now=NOW) == "imported"
    return db


def _revision(plan_id: str, n: int, base: PlanRevision) -> PlanRevision:
    return PlanRevision(
        plan_id, n, stamp_revision(base.snapshot, n, base.pantry), base.pantry,
        base.input_basis, NOW, "regenerate",
    )  # fmt: skip


def _count(db: Path, sql: str) -> int:
    with closing(connect(db)) as conn:
        return conn.execute(sql).fetchone()[0]


# ------------------------------------------------------------ migrations


def test_migrations_apply_and_rerun_is_noop(db: Path) -> None:
    with closing(connect(db)) as conn:
        assert migrate(conn) == list(range(1, LATEST_VERSION + 1))
        assert migrate(conn) == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
    SqlitePlanRepository(db)  # restart: still a no-op
    assert _count(db, "SELECT COUNT(*) FROM schema_version") == LATEST_VERSION


def test_newer_schema_is_refused(db: Path) -> None:
    open_database(db).close()
    with closing(connect(db)) as conn:
        conn.execute("INSERT INTO schema_version VALUES (999, 'future', 'x')")
    with pytest.raises(DatabaseError):
        open_database(db)


def test_missing_database_is_not_created_by_reader(db: Path) -> None:
    with pytest.raises(DatabaseError):
        SqlitePlanReadAdapter(db)
    assert not db.exists()


# ------------------------------------------------------------ planner inputs


def test_inputs_round_trip_equal_to_yaml(imported: Path) -> None:
    sql_inputs = SqlitePlannerInputReader(imported).read()
    assert sql_inputs == YAML_INPUTS
    assert list(sql_inputs.recipes) == list(YAML_INPUTS.recipes)  # order preserved
    assert list(sql_inputs.ingredients) == list(YAML_INPUTS.ingredients)
    assert sql_inputs.pantry == YAML_INPUTS.pantry


def test_generation_identical_from_both_readers(imported: Path) -> None:
    a = generate(YAML_INPUTS, OrToolsPlanGenerator(), 42, "2026-10-19T00:00:00Z")
    b = generate(SqlitePlannerInputReader(imported).read(), OrToolsPlanGenerator(), 42, "2026-10-19T00:00:00Z")
    assert a.plan.model_dump(mode="json") == b.plan.model_dump(mode="json")
    assert a.shopping_list.model_dump(mode="json") == b.shopping_list.model_dump(mode="json")


def test_empty_database_has_no_inputs(db: Path) -> None:
    open_database(db).close()
    with pytest.raises(PlannerInputsMissing):
        SqlitePlannerInputReader(db).read()


def test_workflow_service_on_sqlite_inputs_and_repo(imported: Path) -> None:
    svc = PlanWorkflowService(
        SqlitePlanRepository(imported),
        SqlitePlannerInputReader(imported),
        lambda: NOW,
        OrToolsPlanGenerator(),
    )
    rev = svc.create_plan(date(2026, 10, 26), seed=42)
    assert rev.input_basis.startswith("sha256:")
    assert SqlitePlanRepository(imported).get_revision(rev.plan_id, 1).snapshot == rev.snapshot


# ------------------------------------------------------------ import policy


def test_import_inputs_is_idempotent(db: Path) -> None:
    first = import_inputs(db, DATA)
    assert first.added and not first.unchanged
    second = import_inputs(db, DATA)
    assert not second.added and not second.replaced and len(second.unchanged) == len(first.added)
    assert SqlitePlannerInputReader(db).read() == YAML_INPUTS


def _edit_recipe(db: Path, recipe_id: str) -> None:
    with closing(connect(db)) as conn:
        conn.execute(
            "UPDATE recipes SET content = json_set(content, '$.name', 'Household edit')"
            " WHERE recipe_id = ?",
            (recipe_id,),
        )


def test_import_refuses_to_overwrite_household_edits(db: Path) -> None:
    import_inputs(db, DATA)
    recipe_id = next(iter(YAML_INPUTS.recipes))
    _edit_recipe(db, recipe_id)
    with pytest.raises(InputConflict) as exc:
        import_inputs(db, DATA)
    assert exc.value.conflicts == (f"recipe:{recipe_id}",)
    assert SqlitePlannerInputReader(db).read().recipes[recipe_id].name == "Household edit"
    report = import_inputs(db, DATA, replace=True)
    assert report.replaced == (f"recipe:{recipe_id}",)
    assert SqlitePlannerInputReader(db).read() == YAML_INPUTS


def test_conflict_import_writes_nothing(db: Path, tmp_path: Path) -> None:
    import_inputs(db, DATA)
    ids = list(YAML_INPUTS.recipes)
    _edit_recipe(db, ids[0])
    with closing(connect(db)) as conn:  # drop one so the import would also add a record
        conn.execute("DELETE FROM recipes WHERE recipe_id = ?", (ids[1],))
    with pytest.raises(InputConflict):
        import_inputs(db, DATA)
    assert _count(db, f"SELECT COUNT(*) FROM recipes WHERE recipe_id = '{ids[1]}'") == 0


def test_database_only_records_survive_reimport(db: Path) -> None:
    import_inputs(db, DATA)
    with closing(connect(db)) as conn:
        conn.execute(
            "INSERT INTO recipes (recipe_id, position, content)"
            " SELECT 'zz_extra', 999, REPLACE(content, json_extract(content,'$.id'), 'zz_extra')"
            " FROM recipes LIMIT 1"
        )
    import_inputs(db, DATA)
    assert _count(db, "SELECT COUNT(*) FROM recipes WHERE recipe_id = 'zz_extra'") == 1


def test_fixture_import_creates_revision_one_and_never_overwrites(db: Path) -> None:
    import_inputs(db, DATA)
    assert import_fixture(db, now=NOW) == "imported"
    repo = SqlitePlanRepository(db)
    first = repo.get_revision(PLAN_ID, 1)
    assert first is not None and first.source == "import"
    # grow the plan to revision 2, then re-import: skipped, pointer and revisions untouched
    repo.append_revision(PLAN_ID, 1, _revision(PLAN_ID, 2, first), None)
    assert import_fixture(db, now=NOW) == "skipped"
    assert repo.get_plan(PLAN_ID).current_revision == 2
    assert repo.get_revision(PLAN_ID, 1) == first
    assert _count(db, "SELECT COUNT(*) FROM plan_revisions") == 2


def test_fixture_requires_inputs(db: Path) -> None:
    with pytest.raises(PlannerInputsMissing):
        import_fixture(db)


def test_cli_import_and_conflict_exit_codes(db: Path, capsys) -> None:
    assert db_main(["import", "--db", str(db), "--fixture"]) == 0
    assert "imported" in capsys.readouterr().out
    assert db_main(["import", "--db", str(db), "--fixture"]) == 0
    assert "skipped" in capsys.readouterr().out
    _edit_recipe(db, next(iter(YAML_INPUTS.recipes)))
    assert db_main(["import", "--db", str(db)]) == 2
    assert "conflict: recipe:" in capsys.readouterr().err
    assert db_main(["import", "--db", str(db), "--replace-inputs"]) == 0


def test_db_inside_site_is_refused(capsys) -> None:
    assert db_main(["import", "--db", str(ROOT / "site/public/x.db")]) == 1
    assert "site/" in capsys.readouterr().err


# ------------------------------------------------------------ read API parity


def test_read_service_matches_json_snapshot_reader(imported: Path) -> None:
    json_repo = JsonPlanReadRepository.from_files(
        ROOT / "site/public/plan.json",
        ROOT / "site/public/shopping_list.json",
        DATA / "pantry.yml",
        WEEK,
    )
    a, b = PlanReadService(json_repo), PlanReadService(SqlitePlanReadAdapter(imported))
    assert a.list_plans(WEEK, WEEK) == b.list_plans(WEEK, WEEK)
    plan = a.get_plan(PLAN_ID)
    assert b.get_plan(PLAN_ID) == plan
    args = (PLAN_ID, plan["revision"], plan["snapshot_id"])
    assert a.get_shopping_list(*args) == b.get_shopping_list(*args)


def test_reads_follow_current_revision_pointer(imported: Path) -> None:
    repo = SqlitePlanRepository(imported)
    repo.append_revision(PLAN_ID, 1, _revision(PLAN_ID, 2, repo.get_revision(PLAN_ID, 1)), None)
    assert PlanReadService(SqlitePlanReadAdapter(imported)).get_plan(PLAN_ID)["revision"] == 2


def test_flask_client_serves_from_sqlite_env(imported: Path, monkeypatch) -> None:
    pytest.importorskip("flask")
    monkeypatch.setenv("MEALPLANNER_DB", str(imported))
    client = create_app().test_client()
    resp = client.get("/api/v1/plans?from=2026-10-19&to=2026-10-25")
    assert resp.status_code == 200
    assert [i["plan_id"] for i in resp.get_json()["items"]] == [PLAN_ID]
    plan = client.get(f"/api/v1/plans/{PLAN_ID}").get_json()
    shop = client.get(
        f"/api/v1/plans/{PLAN_ID}/shopping-list?revision=1&snapshot_id={plan['snapshot_id']}"
    )
    assert shop.status_code == 200 and shop.get_json()["plan_id"] == PLAN_ID


def test_flask_missing_db_reports_unavailable(db: Path) -> None:
    pytest.importorskip("flask")
    resp = create_app(db_path=db).test_client().get("/api/v1/plans?from=2026-10-19&to=2026-10-25")
    assert resp.status_code == 503 and not db.exists()


# ------------------------------------------------------------ races, rollback, restart


def test_two_connections_same_expected_revision_one_wins(imported: Path) -> None:
    a, b = SqlitePlanRepository(imported), SqlitePlanRepository(imported)
    base = a.get_revision(PLAN_ID, 1)
    a.append_revision(PLAN_ID, 1, _revision(PLAN_ID, 2, base), None)
    with pytest.raises(StaleRevision) as exc:
        b.append_revision(PLAN_ID, 1, _revision(PLAN_ID, 2, base), None)
    assert (exc.value.expected, exc.value.actual) == (1, 2)
    assert _count(imported, "SELECT COUNT(*) FROM plan_revisions") == 2


def test_threaded_race_exactly_one_winner(imported: Path) -> None:
    base = SqlitePlanRepository(imported).get_revision(PLAN_ID, 1)
    barrier = threading.Barrier(4)
    results: list[object] = []

    def worker() -> None:
        repo = SqlitePlanRepository(imported)
        barrier.wait()
        try:
            repo.append_revision(PLAN_ID, 1, _revision(PLAN_ID, 2, base), None)
            results.append("ok")
        except StaleRevision as exc:
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("ok") == 1 and len(results) == 4
    assert SqlitePlanRepository(imported).get_plan(PLAN_ID).current_revision == 2


def test_failed_append_rolls_back_completely(imported: Path) -> None:
    repo = SqlitePlanRepository(imported)
    base = repo.get_revision(PLAN_ID, 1)
    # the retry record references a plan that does not exist: fails after the revision
    # and assignments were already inserted inside the same transaction
    bad_retry = RetryRecord("plan_nope", "k", "fp", 2, True)
    with pytest.raises(sqlite3.IntegrityError):
        repo.append_revision(PLAN_ID, 1, _revision(PLAN_ID, 2, base), bad_retry)
    assert repo.get_plan(PLAN_ID).current_revision == 1
    assert repo.get_revision(PLAN_ID, 2) is None
    assert _count(imported, "SELECT COUNT(*) FROM assignments WHERE revision = 2") == 0
    assert _count(imported, "SELECT COUNT(*) FROM retry_records") == 0
    # and the database is still writable afterwards (no leaked transaction/lock)
    repo.append_revision(PLAN_ID, 1, _revision(PLAN_ID, 2, base), None)


def test_duplicate_assignment_slot_rolls_back(imported: Path) -> None:
    repo = SqlitePlanRepository(imported)
    base = repo.get_revision(PLAN_ID, 1)
    plan = {**base.snapshot.plan}
    slots = [dict(s) for s in plan["slots"]]
    slots[1] = {**slots[1], "assignment_id": "dup", "date": slots[0]["date"], "meal": slots[0]["meal"]}
    bad = replace(
        _revision(PLAN_ID, 2, base),
        snapshot=replace(base.snapshot, plan={**plan, "slots": slots, "revision": 2}),
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.append_revision(PLAN_ID, 1, bad, None)
    assert repo.get_plan(PLAN_ID).current_revision == 1
    assert repo.get_revision(PLAN_ID, 2) is None


def test_append_to_unknown_plan_and_gap(imported: Path) -> None:
    repo = SqlitePlanRepository(imported)
    base = repo.get_revision(PLAN_ID, 1)
    with pytest.raises(ValueError):
        repo.append_revision(PLAN_ID, 1, _revision(PLAN_ID, 3, base), None)


def test_retry_replay_survives_restart(imported: Path) -> None:
    def service() -> PlanWorkflowService:  # a new instance == a process restart
        return PlanWorkflowService(
            SqlitePlanRepository(imported),
            SqlitePlannerInputReader(imported),
            lambda: NOW,
            OrToolsPlanGenerator(),
            timezone="America/Bogota",
        )

    first = service()
    plan = first.create_plan(date(2026, 10, 26), seed=1)
    prop = first.propose_regeneration(plan.plan_id, 1, seed=7)
    result = first.accept_proposal(plan.plan_id, prop.proposal_id, 1, "retry-1")
    again = service().accept_proposal(plan.plan_id, prop.proposal_id, 1, "retry-1")
    assert again.replayed and (again.revision, again.snapshot_id, again.changed) == (
        result.revision, result.snapshot_id, result.changed,
    )  # fmt: skip
    assert SqlitePlanRepository(imported).get_retry(plan.plan_id, "retry-1") is not None


def test_revision_and_assignment_rows_are_immutable(imported: Path) -> None:
    with closing(connect(imported)) as conn:
        for sql in (
            "UPDATE plan_revisions SET source = 'x'",
            "DELETE FROM plan_revisions",
            "UPDATE assignments SET meal = 'lunch'",
            "DELETE FROM assignments",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="immutable"):
                conn.execute(sql)
    assert _count(imported, "SELECT COUNT(*) FROM plan_revisions") == 1


def test_assignment_and_week_keys_enforced(imported: Path) -> None:
    repo = SqlitePlanRepository(imported)
    rev = repo.get_revision(PLAN_ID, 1)
    with pytest.raises(DuplicateWeek):
        repo.create_plan(WeeklyPlan("other_id", WEEK, "America/Bogota", 1), rev)
    with closing(connect(imported)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(  # duplicate (plan, revision, date, meal) with a new assignment id
            "INSERT INTO assignments SELECT plan_id, revision, 'other', date, meal, recipe_id,"
            " variant_id, content FROM assignments LIMIT 1"
        )
    with closing(connect(imported)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE plans SET current_revision = 9")  # pointer must reference a revision


# ------------------------------------------------------------ backup / restore


def test_backup_restore_round_trip(imported: Path, tmp_path: Path) -> None:
    repo = SqlitePlanRepository(imported)
    repo.append_revision(PLAN_ID, 1, _revision(PLAN_ID, 2, repo.get_revision(PLAN_ID, 1)), None)
    dest = backup(imported, tmp_path / "bk" / "backup.db")
    with pytest.raises(DatabaseError):
        backup(imported, dest)  # never overwrites a backup
    clean = tmp_path / "restored" / "meal.db"
    restore(dest, clean)
    with pytest.raises(DatabaseError):
        restore(dest, clean)  # existing DB needs --force
    restored = SqlitePlanRepository(clean)
    assert restored.get_plan(PLAN_ID).current_revision == 2
    for n in (1, 2):
        assert restored.get_revision(PLAN_ID, n) == repo.get_revision(PLAN_ID, n)
    assert PlanReadService(SqlitePlanReadAdapter(clean)).get_plan(PLAN_ID)["revision"] == 2
    assert SqlitePlannerInputReader(clean).read() == YAML_INPUTS
    restore(dest, clean, force=True)


def test_restore_rejects_garbage(tmp_path: Path) -> None:
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database" * 100)
    with pytest.raises((DatabaseError, sqlite3.DatabaseError)):
        restore(junk, tmp_path / "out.db")
    assert not (tmp_path / "out.db").exists()
