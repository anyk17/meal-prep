"""Phase 4b: publication export (selection, trips, writer atomicity, validator, db_cli)."""

from __future__ import annotations

import itertools
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from mealplanner.adapters import publication_files
from mealplanner.adapters.ortools import OrToolsPlanGenerator
from mealplanner.adapters.publication_files import FilePublicationWriter
from mealplanner.adapters.sqlite.migrations import LATEST_VERSION, connect, current_version
from mealplanner.adapters.sqlite.store import (
    SqlitePlannerInputReader,
    SqlitePlanReadAdapter,
    SqlitePlanRepository,
    SqliteShoppingTripRepository,
)
from mealplanner.application.plan_workflows import PlanWorkflowService
from mealplanner.application.publication import (
    PublicationError,
    PublicationService,
    check_bundle,
    overlaps_months,
)
from mealplanner.application.shopping_trips import ShoppingTripService
from mealplanner.domain import shopping_trip as trips
from mealplanner.domain.planner_inputs import PlannerInputs
from mealplanner.domain.solution import MealPlanSolution
from mealplanner.entrypoints import db_cli
from mealplanner.entrypoints.db_cli import full_bundle_validator, import_inputs

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
TZ = "America/Bogota"
W_OCT26, W_NOV02, W_NOV30, W_DEC28 = (date(2026, 10, 26), date(2026, 11, 2), date(2026, 11, 30), date(2026, 12, 28))
P_OCT26, P_NOV02, P_NOV30, P_DEC28 = "plan_2026_10_26", "plan_2026_11_02", "plan_2026_11_30", "plan_2026_12_28"

_cache: dict[int, MealPlanSolution] = {}


class CachedGenerator:
    def generate(self, inputs: PlannerInputs, seed: int) -> MealPlanSolution:
        if seed not in _cache:
            _cache[seed] = OrToolsPlanGenerator().generate(inputs, seed)
        return _cache[seed]


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.db = tmp_path / "plans.db"
        import_inputs(self.db, ROOT / "data")
        self.plans = SqlitePlanRepository(self.db)
        self.trip_repo = SqliteShoppingTripRepository(self.db)
        self.workflow = PlanWorkflowService(
            self.plans, SqlitePlannerInputReader(self.db), lambda: NOW, generator=CachedGenerator()
        )
        self.trips = ShoppingTripService(
            self.trip_repo, self.plans, lambda: NOW, SqlitePlannerInputReader(self.db).shelf_lives
        )
        self.out = tmp_path / "out"
        self._counter = itertools.count(1)

    def plan(self, week: date) -> str:
        return self.workflow.create_plan(week, seed=1).plan_id

    def new_revision(self, plan_id: str) -> None:
        current = self.plans.get_plan(plan_id).current_revision
        proposal = self.workflow.propose_regeneration(plan_id, current, seed=2)
        self.workflow.accept_proposal(plan_id, proposal.proposal_id, current, f"k{current}")

    def service(self, validate=None, read=None) -> PublicationService:
        return PublicationService(
            read or SqlitePlanReadAdapter(self.db),
            self.trips,
            lambda: NOW,
            TZ,
            FilePublicationWriter(self.out, validate),
            id_factory=lambda: f"{next(self._counter):06x}",
        )


@pytest.fixture
def env(tmp_path) -> Env:
    e = Env(tmp_path)
    for week in (W_OCT26, W_NOV02, W_NOV30, W_DEC28):
        e.plan(week)
    return e


def contract():
    sys.path.insert(0, str(ROOT / "scripts"))
    import validate_publication_contract as vpc

    return vpc


@pytest.fixture(scope="module")
def full_validator():
    return full_bundle_validator()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def published(env: Env, result) -> Path:
    return env.out / "publications" / result.publication_id


def plan_ids(result) -> list[str]:
    return [p["plan_id"] for p in result.plans]


# ---------------------------------------------------------------- selection


def test_overlap_rule() -> None:
    sun, mon = date(2026, 11, 1), date(2026, 10, 26)
    assert overlaps_months(mon, sun, ["2026-10"]) and overlaps_months(mon, sun, ["2026-11"])
    assert not overlaps_months(mon, sun, ["2026-12", "2026-09"])
    assert overlaps_months(date(2026, 12, 28), date(2027, 1, 3), ["2027-01"])


def test_month_overlap_selection_including_boundaries(env: Env, full_validator) -> None:
    cases = {
        ("2026-10",): [P_OCT26],  # week ends Sunday Nov 1
        ("2026-11",): [P_OCT26, P_NOV02, P_NOV30],  # Oct 26 week and Nov 30 week cross months
        ("2026-12",): [P_NOV30, P_DEC28],
        ("2027-01",): [P_DEC28],  # Dec 28 - Jan 3 crosses the year
        ("2027-01", "2026-10", "2026-10"): [P_OCT26, P_DEC28],  # unsorted, duplicated
        ("2026-09",): [],
    }
    for months, expected in cases.items():
        env.out = env.out.parent / ("out_" + "_".join(months).replace("-", ""))
        result = env.service(full_validator).export(months)
        assert plan_ids(result) == expected, months
        manifest = read_json(published(env, result) / "manifest.json")
        assert manifest["selected_months"] == sorted(set(months))
        assert [e["summary"]["plan_id"] for e in manifest["plans"]] == expected
        assert manifest["timezone"] == TZ and manifest["shopping_trips"] == []


def test_invalid_months_rejected(env: Env) -> None:
    for bad in ([], ["2026-13"], ["2026-1"], ["next"]):
        with pytest.raises(PublicationError):
            env.service().export(bad)
    assert not env.out.exists()


def test_export_files_layout_and_ids(env: Env, full_validator) -> None:
    result = env.service(full_validator).export(["2026-10"])
    assert re.fullmatch(r"pub_20261021T150000Z_[0-9a-f]{6}", result.publication_id)
    assert result.exported_at == "2026-10-21T15:00:00Z"
    index = read_json(env.out / "index.json")
    assert index == {
        "schema_version": 1,
        "publication_id": result.publication_id,
        "manifest_path": f"publications/{result.publication_id}/manifest.json",
    }
    base = published(env, result)
    names = sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())
    assert names == ["manifest.json", f"plans/{P_OCT26}/plan.json", f"plans/{P_OCT26}/shopping-list.json"]
    assert not list(env.out.glob(".staging-*")) and not list(env.out.glob(".index-*"))


# ---------------------------------------------------------------- trips


def test_trip_inclusion_and_skip(env: Env, full_validator) -> None:
    a = env.trips.create([P_OCT26, P_NOV02])["trip_id"]
    b = env.trips.create([P_NOV30, P_DEC28])["trip_id"]
    result = env.service(full_validator).export(["2026-11"])
    assert [t["trip_id"] for t in result.trips_included] == [a]
    assert [(s.trip_id, P_DEC28 in s.reason) for s in result.trips_skipped] == [(b, True)]
    base = published(env, result)
    manifest = read_json(base / "manifest.json")
    assert manifest["shopping_trips"] == [{"trip_id": a, "path": f"shopping-trips/{a}.json"}]
    assert not (base / "shopping-trips" / f"{b}.json").exists()
    trip = read_json(base / "shopping-trips" / f"{a}.json")
    assert trip == env.trips.get(a)
    assert trip["status"] == "open"
    shopping = [read_json(base / f"plans/{p}/shopping-list.json") for p in (P_OCT26, P_NOV02)]
    assert trip["items"] == trips.sum_items(
        [(p, sl, trips.GROUPS) for p, sl in zip((P_OCT26, P_NOV02), shopping)],
        SqlitePlannerInputReader(env.db).shelf_lives(),
    )

    both = env.service(full_validator)
    env.out = env.out.parent / "out_all"
    result = both.export(["2026-11", "2026-12"])
    assert sorted(t["trip_id"] for t in result.trips_included) == sorted([a, b])
    assert result.trips_skipped == ()


def test_trips_sorted_by_id(env: Env, full_validator) -> None:
    ids = [env.trips.create([p])["trip_id"] for p in (P_OCT26, P_NOV02, P_NOV30)]
    result = env.service(full_validator).export(["2026-11"])
    manifest = read_json(published(env, result) / "manifest.json")
    assert [t["trip_id"] for t in manifest["shopping_trips"]] == sorted(ids)


def test_done_trip_is_frozen_to_covered_revision(env: Env, full_validator) -> None:
    trip = env.trips.create([P_OCT26, P_NOV02])
    tid = trip["trip_id"]
    frozen_items = trip["items"]
    env.trips.mark_done(tid, 1)
    env.new_revision(P_OCT26)  # the plan moves on; the done trip does not
    result = env.service(full_validator).export(["2026-10", "2026-11"])
    base = published(env, result)
    manifest = read_json(base / "manifest.json")
    revisions = {e["summary"]["plan_id"]: e["summary"]["revision"] for e in manifest["plans"]}
    assert revisions[P_OCT26] == 2
    out = read_json(base / "shopping-trips" / f"{tid}.json")
    assert out["status"] == "done" and out["items"] == [] and out["bought_items"] == frozen_items
    week = out["weeks"][0]
    assert (week["plan_id"], week["current_revision"]) == (P_OCT26, 2)
    assert [(c["state"], c["revision"], c["changed_since_shopping"]) for c in week["cells"]] == [
        ("bought", 1, True), ("bought", 1, True)
    ]


def test_open_trip_follows_current_revision(env: Env, full_validator) -> None:
    tid = env.trips.create([P_OCT26])["trip_id"]
    env.new_revision(P_OCT26)
    result = env.service(full_validator).export(["2026-10"])
    out = read_json(published(env, result) / "shopping-trips" / f"{tid}.json")
    assert out["weeks"][0]["current_revision"] == 2
    assert all(c["state"] == "to_buy" and c["revision"] is None for c in out["weeks"][0]["cells"])


def test_check_bundle_catches_incoherence(env: Env) -> None:
    env.trips.create([P_OCT26])
    bundle, _ = env.service().build(["2026-10"])
    assert check_bundle(bundle.index, bundle.files) == []
    import copy

    files = copy.deepcopy(dict(bundle.files))
    trip_file = next(k for k in files if k.startswith("shopping-trips/"))
    files[trip_file]["weeks"][0]["current_revision"] += 1
    assert any("current_revision" in e for e in check_bundle(bundle.index, files))
    files = copy.deepcopy(dict(bundle.files))
    files["shopping-trips/stray.json"] = {}
    assert any("unreferenced" in e for e in check_bundle(bundle.index, files))
    files = copy.deepcopy(dict(bundle.files))
    del files[f"plans/{P_OCT26}/plan.json"]
    assert any("missing" in e for e in check_bundle(bundle.index, files))


def test_unstable_reads_fail_after_retries(env: Env) -> None:
    class Flaky(SqlitePlanReadAdapter):
        calls = 0

        def list_plan_summaries(self):
            summaries = super().list_plan_summaries()
            type(self).calls += 1
            if type(self).calls % 2 == 0:  # the second listing of every attempt differs
                for s in summaries:
                    s["revision"] += 1
            return summaries

    with pytest.raises(PublicationError, match="kept changing"):
        env.service(read=Flaky(env.db)).export(["2026-10"])
    assert not (env.out / "index.json").exists()


# ---------------------------------------------------------------- writer atomicity


def test_failure_before_index_swap_keeps_old_index(env: Env, full_validator) -> None:
    first = env.service(full_validator).export(["2026-10"])
    index_bytes = (env.out / "index.json").read_bytes()

    failing = env.service(lambda root: ["injected validation failure"])
    with pytest.raises(PublicationError, match="failed validation") as info:
        failing.export(["2026-11"])
    assert info.value.errors == ["injected validation failure"]
    assert (env.out / "index.json").read_bytes() == index_bytes
    assert sorted(p.name for p in (env.out / "publications").iterdir()) == [first.publication_id]
    assert not [p for p in env.out.iterdir() if p.name.startswith(".")]


def test_failure_at_index_swap_removes_new_directory(env: Env, full_validator, monkeypatch) -> None:
    first = env.service(full_validator).export(["2026-10"])
    index_bytes = (env.out / "index.json").read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(publication_files.os, "replace", boom)
    with pytest.raises(OSError):
        env.service(full_validator).export(["2026-11"])
    monkeypatch.undo()
    assert (env.out / "index.json").read_bytes() == index_bytes
    assert sorted(p.name for p in (env.out / "publications").iterdir()) == [first.publication_id]
    assert not [p for p in env.out.iterdir() if p.name.startswith(".")]


def test_success_activates_new_and_keeps_previous(env: Env, full_validator) -> None:
    first = env.service(full_validator).export(["2026-10"])
    second = env.service(full_validator).export(["2026-11"])
    assert read_json(env.out / "index.json")["publication_id"] == second.publication_id
    assert sorted(p.name for p in (env.out / "publications").iterdir()) == sorted(
        [first.publication_id, second.publication_id]
    )


def test_existing_publication_directory_is_refused(env: Env) -> None:
    bundle, _ = env.service().build(["2026-10"])
    writer = FilePublicationWriter(env.out)
    writer.write(bundle)
    with pytest.raises(PublicationError, match="already exists"):
        writer.write(bundle)


def test_written_bundle_passes_contract_validator(env: Env) -> None:
    env.trips.create([P_OCT26, P_NOV02])
    env.service().export(["2026-10", "2026-11"])
    vpc = contract()
    openapi, schema, _, resolver = vpc.validate_schema_documents(vpc.api.Checks())
    assert vpc.validate_bundle(env.out, openapi, schema, resolver) == []


def test_export_does_not_change_the_database(env: Env) -> None:
    env.trips.create([P_OCT26])
    with connect(env.db) as conn:
        before = [tuple(r) for r in conn.execute("SELECT plan_id, current_revision FROM plans ORDER BY 1")]
        trips_before = [tuple(r) for r in conn.execute("SELECT * FROM shopping_trips")]
    env.service().export(["2026-10"])
    with connect(env.db) as conn:
        assert before == [tuple(r) for r in conn.execute("SELECT plan_id, current_revision FROM plans ORDER BY 1")]
        assert trips_before == [tuple(r) for r in conn.execute("SELECT * FROM shopping_trips")]


# ---------------------------------------------------------------- db_cli


def test_cli_migrate(tmp_path, capsys) -> None:
    db = tmp_path / "old.db"
    assert db_cli.main(["migrate", "--db", str(db)]) == 1  # never creates a database
    assert not db.exists()
    sqlite3.connect(db).close()  # an empty, unmigrated file
    assert db_cli.main(["migrate", "--db", str(db)]) == 0
    assert "applied versions" in capsys.readouterr().out
    with connect(db) as conn:
        assert current_version(conn) == LATEST_VERSION
    assert db_cli.main(["migrate", "--db", str(db)]) == 0
    assert "already at schema version" in capsys.readouterr().out


def test_cli_export_smoke(env: Env, tmp_path, capsys) -> None:
    env.trips.create([P_OCT26, P_NOV02])
    env.trips.create([P_NOV30, P_DEC28])
    out = tmp_path / "cli_out"
    code = db_cli.main(
        ["export", "--db", str(env.db), "--months", "2026-10,2026-11", "--out", str(out)]
    )
    text = capsys.readouterr().out
    assert code == 0, text
    index = read_json(out / "index.json")
    assert f"publication_id: {index['publication_id']}" in text
    assert "plans included: 3" in text and "trips included: 1" in text and "trips skipped: 1" in text
    assert P_DEC28 in text and "publication/" in text
    vpc = contract()
    openapi, schema, _, resolver = vpc.validate_schema_documents(vpc.api.Checks())
    assert vpc.validate_bundle(out, openapi, schema, resolver) == []


def test_cli_export_errors(tmp_path, capsys) -> None:
    db = tmp_path / "unmigrated.db"
    sqlite3.connect(db).close()
    out = tmp_path / "o"
    assert db_cli.main(["export", "--db", str(db), "--months", "2026-10", "--out", str(out)]) == 1
    assert "migrate" in capsys.readouterr().err and not out.exists()
    missing = tmp_path / "missing.db"
    assert db_cli.main(["export", "--db", str(missing), "--months", "2026-10", "--out", str(out)]) == 1
    assert not missing.exists()
    capsys.readouterr()
    assert db_cli.main(["migrate", "--db", str(db)]) == 0
    assert db_cli.main(["export", "--db", str(db), "--months", "2026-13", "--out", str(out)]) == 1
    assert "YYYY-MM" in capsys.readouterr().err
