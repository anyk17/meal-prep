"""ShoppingTrip tests: domain functions plus the service on both memory and SQLite stores."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from mealplanner.adapters.memory import InMemoryPlanRepository, InMemoryShoppingTripRepository
from mealplanner.adapters.ortools import OrToolsPlanGenerator
from mealplanner.adapters.sqlite.migrations import connect
from mealplanner.adapters.sqlite.store import SqlitePlanRepository, SqliteShoppingTripRepository
from mealplanner.adapters.yaml_inputs import YamlPlannerInputReader
from mealplanner.application.errors import PlanNotFound
from mealplanner.application.plan_workflows import PlanWorkflowService
from mealplanner.application.shopping_trips import ShoppingTripService
from mealplanner.domain import shopping_trip as trips
from mealplanner.domain.planner_inputs import PlannerInputs
from mealplanner.domain.solution import MealPlanSolution

ROOT = Path(__file__).resolve().parents[2]
INPUTS: PlannerInputs = YamlPlannerInputReader(ROOT / "data").read()
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
W1, W2, W3 = date(2026, 10, 26), date(2026, 11, 2), date(2026, 11, 9)
P1, P2, P3 = "plan_2026_10_26", "plan_2026_11_02", "plan_2026_11_09"

_cache: dict[int, MealPlanSolution] = {}


class CachedGenerator:
    def generate(self, inputs: PlannerInputs, seed: int) -> MealPlanSolution:
        if seed not in _cache:
            _cache[seed] = OrToolsPlanGenerator().generate(inputs, seed)
        return _cache[seed]


class Reader:
    def read(self) -> PlannerInputs:
        return INPUTS


class Env:
    def __init__(self, kind: str, tmp_path: Path) -> None:
        if kind == "memory":
            self.plans = InMemoryPlanRepository()
            self.trip_repo = InMemoryShoppingTripRepository()
        else:
            self.db = tmp_path / "plans.db"
            self.plans = SqlitePlanRepository(self.db)
            self.trip_repo = SqliteShoppingTripRepository(self.db)
        self.kind = kind
        self.now = NOW
        clock = lambda: self.now  # noqa: E731
        self.workflow = PlanWorkflowService(self.plans, Reader(), clock, generator=CachedGenerator())
        self.svc = ShoppingTripService(self.trip_repo, self.plans, clock)

    def plan(self, week: date, seed: int = 1) -> str:
        return self.workflow.create_plan(week, seed=seed).plan_id

    def new_revision(self, plan_id: str) -> None:
        """Accept a regeneration so the plan's current revision moves to 2."""
        current = self.plans.get_plan(plan_id).current_revision
        proposal = self.workflow.propose_regeneration(plan_id, current, seed=2)
        self.workflow.accept_proposal(plan_id, proposal.proposal_id, current, f"k{current}")


@pytest.fixture(params=["memory", "sqlite"])
def env(request, tmp_path) -> Env:
    return Env(request.param, tmp_path)


def item_map(view: dict) -> dict[tuple[str, str], float]:
    return {(i["ingredient_id"], i["unit"]): i["quantity"] for i in view["items"]}


def weekly_map(env: Env, plan_id: str, revision: int | None = None) -> dict[tuple[str, str], float]:
    rev = revision or env.plans.get_plan(plan_id).current_revision
    shopping = env.plans.get_revision(plan_id, rev).snapshot.shopping_list
    return {
        (l["ingredient_id"], l["unit"]): l["quantity"]
        for lines in shopping["sections"].values()
        for l in lines
    }


# ---------------------------------------------------------------- create / read


def test_create_trip(env: Env) -> None:
    p2, p1 = env.plan(W2), env.plan(W1)
    view = env.svc.create([p2, p1])
    assert view["status"] == "open" and view["version"] == 1 and view["completed_at"] is None
    assert [w["plan_id"] for w in view["weeks"]] == [p1, p2]  # sorted by week_start
    assert view["name"] == "Oct 26 - Nov 8"
    assert all(w["coverage"] == "on_list" and not w["changed_since_shopping"] for w in view["weeks"])
    assert env.svc.get(view["trip_id"]) == view
    assert env.svc.list()["items"] == [view]
    named = env.svc.create([env.plan(W3)], name="  Big   shop ")
    assert named["name"] == "Big shop"


def test_create_validation(env: Env) -> None:
    p1 = env.plan(W1)
    with pytest.raises(trips.InvalidTrip):
        env.svc.create([])
    with pytest.raises(trips.InvalidTrip):
        env.svc.create([p1, p1])
    with pytest.raises(trips.InvalidTrip):
        env.svc.create([p1], name="x" * 81)
    with pytest.raises(PlanNotFound):
        env.svc.create([p1, "plan_nope"])
    assert env.svc.list() == {"items": []}  # nothing partially created
    with pytest.raises(trips.TripNotFound):
        env.svc.get("trip_missing")


def test_items_sum_across_weeks(env: Env) -> None:
    p1, p2 = env.plan(W1, seed=1), env.plan(W2, seed=2)
    view = env.svc.create([p1, p2])
    a, b = weekly_map(env, p1), weekly_map(env, p2)
    expected = {k: round(a.get(k, 0) + b.get(k, 0), 2) for k in a.keys() | b.keys()}
    assert item_map(view) == expected
    both = [i for i in view["items"] if i["plan_ids"] == [p1, p2]]
    assert both and all(i["ingredient_id"] and i["display"] and i["section"] for i in view["items"])
    assert view["weeks"][0]["revision"] == 1


def test_sum_items_keeps_units_separate_and_sections() -> None:
    def line(ing, qty, unit, section="produce"):
        return {"ingredient_id": ing, "display": ing.title(), "quantity": qty, "unit": unit,
                "section": section, "notes": None}

    w1 = {"sections": {"produce": [line("tomato", 100.0, "g"), line("tomato", 2.0, "units")]}}
    w2 = {"sections": {"produce": [line("tomato", 50.5, "g")], "dairy": [line("milk", 200.0, "ml", "dairy")]}}
    items = trips.sum_items([("a", w1), ("b", w2)])
    assert {(i["ingredient_id"], i["unit"]): (i["quantity"], i["plan_ids"]) for i in items} == {
        ("tomato", "g"): (150.5, ["a", "b"]),
        ("tomato", "units"): (2.0, ["a"]),
        ("milk", "ml"): (200.0, ["b"]),
    }
    assert [i["section"] for i in items] == ["produce", "produce", "dairy"]


# ---------------------------------------------------------------- add / remove


def test_add_and_remove_week(env: Env) -> None:
    p1, p2, p3 = env.plan(W1), env.plan(W2), env.plan(W3)
    trip = env.svc.create([p2])
    added = env.svc.add_week(trip["trip_id"], p3, 1)
    assert added["version"] == 2 and [w["plan_id"] for w in added["weeks"]] == [p2, p3]
    added = env.svc.add_week(trip["trip_id"], p1, 2)
    assert [w["plan_id"] for w in added["weeks"]] == [p1, p2, p3]
    removed = env.svc.remove_week(trip["trip_id"], p2, 3)
    assert removed["version"] == 4 and [w["plan_id"] for w in removed["weeks"]] == [p1, p3]
    assert item_map(removed) == {
        k: round(weekly_map(env, p1).get(k, 0) + weekly_map(env, p3).get(k, 0), 2)
        for k in weekly_map(env, p1).keys() | weekly_map(env, p3).keys()
    }
    with pytest.raises(trips.WeekAlreadyOnTrip):
        env.svc.add_week(trip["trip_id"], p1, 4)
    with pytest.raises(trips.WeekNotOnTrip):
        env.svc.remove_week(trip["trip_id"], p2, 4)
    with pytest.raises(PlanNotFound):
        env.svc.add_week(trip["trip_id"], "plan_nope", 4)
    env.svc.remove_week(trip["trip_id"], p3, 4)
    with pytest.raises(trips.LastWeek):
        env.svc.remove_week(trip["trip_id"], p1, 5)
    assert env.svc.get(trip["trip_id"])["version"] == 5


def test_stale_version_rejected_everywhere(env: Env) -> None:
    p1, p2 = env.plan(W1), env.plan(W2)
    trip = env.svc.create([p1])
    tid = trip["trip_id"]
    env.svc.add_week(tid, p2, 1)  # version -> 2
    calls = [
        lambda: env.svc.add_week(tid, p2, 1),
        lambda: env.svc.remove_week(tid, p2, 1),
        lambda: env.svc.mark_done(tid, 1),
        lambda: env.svc.reopen(tid, 1),
        lambda: env.svc.delete(tid, 1),
    ]
    for call in calls:
        with pytest.raises(trips.StaleVersion) as exc:
            call()
        assert (exc.value.expected, exc.value.actual) == (1, 2)
    assert env.svc.get(tid)["version"] == 2
    with pytest.raises(trips.TripNotFound):
        env.svc.delete("trip_missing", 1)


# ---------------------------------------------------------------- one trip per week


def test_one_trip_per_week_any_status(env: Env) -> None:
    p1, p2 = env.plan(W1), env.plan(W2)
    first = env.svc.create([p1])
    with pytest.raises(trips.WeekAlreadyOnTrip) as exc:
        env.svc.create([p1, p2])
    assert exc.value.plan_id == p1 and exc.value.trip_id == first["trip_id"]
    assert env.svc.list()["items"] == [first]  # the failed create left no trip behind
    second = env.svc.create([p2])
    with pytest.raises(trips.WeekAlreadyOnTrip):
        env.svc.add_week(second["trip_id"], p1, 1)
    env.svc.mark_done(first["trip_id"], 1)  # done trips keep their weeks
    with pytest.raises(trips.WeekAlreadyOnTrip):
        env.svc.add_week(second["trip_id"], p1, 1)
    with pytest.raises(trips.WeekAlreadyOnTrip):
        env.svc.create([p1])


def test_repository_enforces_one_trip_per_week(env: Env) -> None:
    """Storage rejects the second owner even if the service layer is bypassed."""
    p1 = env.plan(W1)
    week = trips.TripWeek(p1, W1)
    env.trip_repo.create(trips.new_trip("trip_a", None, [week], NOW))
    with pytest.raises(trips.WeekAlreadyOnTrip):
        env.trip_repo.create(trips.new_trip("trip_b", None, [week], NOW))
    assert env.trip_repo.get("trip_b") is None
    p2 = env.plan(W2)
    env.trip_repo.create(trips.new_trip("trip_c", None, [trips.TripWeek(p2, W2)], NOW))
    stolen = trips.add_week(env.trip_repo.get("trip_c"), week, 1, NOW)
    with pytest.raises(trips.WeekAlreadyOnTrip) as exc:
        env.trip_repo.save(stolen, 1)
    assert exc.value.trip_id == "trip_a"
    assert env.trip_repo.get("trip_c").version == 1


def test_sqlite_constraints(tmp_path) -> None:
    env = Env("sqlite", tmp_path)
    p1, p2 = env.plan(W1), env.plan(W2)
    env.trip_repo.create(trips.new_trip("trip_a", None, [trips.TripWeek(p1, W1)], NOW))
    conn = connect(env.db)
    try:
        conn.execute(
            "INSERT INTO shopping_trips VALUES ('trip_b', 'x', 'open', 1, 'a', 'a', NULL)"
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute("INSERT INTO shopping_trip_weeks (trip_id, plan_id) VALUES ('trip_b', ?)", (p1,))
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                "INSERT INTO shopping_trip_weeks (trip_id, plan_id) VALUES ('trip_b', 'plan_missing')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                "INSERT INTO shopping_trips VALUES ('trip_c', 'x', 'open', 1, 'a', 'a', NULL)"
            )
            conn.execute(
                "INSERT INTO shopping_trip_weeks VALUES ('trip_c', ?, 99, 'sha256:x')", (p2,)
            )
    finally:
        conn.close()
    # unknown plan through the repository maps to PlanNotFound
    with pytest.raises(PlanNotFound):
        env.trip_repo.create(trips.new_trip("trip_d", None, [trips.TripWeek("plan_missing", W3)], NOW))


# ---------------------------------------------------------------- done / reopen


def test_done_freezes_revisions(env: Env) -> None:
    p1, p2 = env.plan(W1), env.plan(W2)
    trip = env.svc.create([p1, p2])
    tid = trip["trip_id"]
    before = item_map(trip)
    old_snapshot = env.plans.get_revision(p1, 1).snapshot.plan["snapshot_id"]
    done = env.svc.mark_done(tid, 1)
    assert done["status"] == "done" and done["version"] == 2 and done["completed_at"]
    assert [(w["revision"], w["coverage"], w["changed_since_shopping"]) for w in done["weeks"]] == [
        (1, "covered", False), (1, "covered", False)
    ]
    assert done["weeks"][0]["snapshot_id"] == old_snapshot
    assert item_map(done) == before

    env.new_revision(p1)  # plan edits never touch the trip
    assert env.plans.get_plan(p1).current_revision == 2
    after = env.svc.get(tid)
    assert after["version"] == 2 and item_map(after) == before
    week = after["weeks"][0]
    assert (week["revision"], week["current_revision"]) == (1, 2)
    assert week["coverage"] == "covered" and week["changed_since_shopping"] is True
    assert after["weeks"][1]["changed_since_shopping"] is False
    assert weekly_map(env, p1, 2) != weekly_map(env, p1, 1)

    summaries = [
        {"plan_id": p1, "week_start": "2026-10-26", "week_end": "2026-11-01", "revision": 2},
        {"plan_id": p2, "week_start": "2026-11-02", "week_end": "2026-11-08", "revision": 1},
    ]
    cov = {i["plan_id"]: i for i in env.svc.coverage(summaries)["items"]}
    assert (cov[p1]["coverage"], cov[p1]["changed_since_shopping"], cov[p1]["trip_id"]) == ("covered", True, tid)
    assert cov[p2]["changed_since_shopping"] is False


def test_done_rejects_week_changes_and_repeats(env: Env) -> None:
    p1, p2, p3 = env.plan(W1), env.plan(W2), env.plan(W3)
    trip = env.svc.create([p1, p2])
    tid = trip["trip_id"]
    env.svc.mark_done(tid, 1)
    with pytest.raises(trips.TripDone):
        env.svc.remove_week(tid, p1, 2)
    with pytest.raises(trips.TripDone):
        env.svc.add_week(tid, p3, 2)
    with pytest.raises(trips.TripNotDone):
        env.svc.mark_done(tid, 2)
    assert env.svc.get(tid)["version"] == 2


def test_reopen_follows_current_revisions(env: Env) -> None:
    p1, p2 = env.plan(W1), env.plan(W2)
    tid = env.svc.create([p1, p2])["trip_id"]
    env.svc.mark_done(tid, 1)
    env.new_revision(p1)
    reopened = env.svc.reopen(tid, 2)
    assert reopened["status"] == "open" and reopened["version"] == 3 and reopened["completed_at"] is None
    assert [(w["revision"], w["coverage"], w["changed_since_shopping"]) for w in reopened["weeks"]] == [
        (2, "on_list", False), (1, "on_list", False)
    ]
    expected = {
        k: round(weekly_map(env, p1, 2).get(k, 0) + weekly_map(env, p2).get(k, 0), 2)
        for k in weekly_map(env, p1, 2).keys() | weekly_map(env, p2).keys()
    }
    assert item_map(reopened) == expected
    with pytest.raises(trips.TripNotDone):
        env.svc.reopen(tid, 3)
    # weeks can be changed again, and done can be repeated against the new revision
    env.svc.remove_week(tid, p2, 3)
    redone = env.svc.mark_done(tid, 4)
    assert redone["weeks"][0]["revision"] == 2 and not redone["weeks"][0]["changed_since_shopping"]


def test_trips_never_modify_plans(env: Env) -> None:
    p1 = env.plan(W1)
    before = env.plans.list_revisions(p1)
    tid = env.svc.create([p1])["trip_id"]
    env.svc.mark_done(tid, 1)
    env.svc.reopen(tid, 2)
    env.svc.delete(tid, 3)
    assert env.plans.list_revisions(p1) == before
    assert env.plans.get_plan(p1).current_revision == 1


# ---------------------------------------------------------------- delete / coverage


def test_delete_frees_weeks(env: Env) -> None:
    p1, p2 = env.plan(W1), env.plan(W2)
    tid = env.svc.create([p1, p2])["trip_id"]
    env.svc.mark_done(tid, 1)
    env.svc.delete(tid, 2)  # a done trip can be deleted to undo a mistake
    assert env.svc.list() == {"items": []}
    with pytest.raises(trips.TripNotFound):
        env.svc.get(tid)
    again = env.svc.create([p1, p2])  # both weeks are free again
    assert again["version"] == 1
    assert env.plans.get_plan(p1) is not None


def test_coverage_states(env: Env) -> None:
    p1, p2, p3 = env.plan(W1), env.plan(W2), env.plan(W3)
    open_trip = env.svc.create([p1])["trip_id"]
    done_trip = env.svc.create([p2])["trip_id"]
    env.svc.mark_done(done_trip, 1)

    def summary(pid, week, rev=1):
        return {"plan_id": pid, "week_start": week.isoformat(),
                "week_end": (week + timedelta(days=6)).isoformat(), "revision": rev}

    items = env.svc.coverage([summary(p1, W1), summary(p2, W2), summary(p3, W3)])["items"]
    assert [(i["coverage"], i["trip_id"], i["changed_since_shopping"]) for i in items] == [
        ("on_list", open_trip, False), ("covered", done_trip, False), ("not_covered", None, False)
    ]
    assert env.svc.coverage([]) == {"items": []}


def test_persistence_across_instances(tmp_path) -> None:
    env = Env("sqlite", tmp_path)
    p1, p2 = env.plan(W1), env.plan(W2)
    tid = env.svc.create([p1, p2], name="Weekend")["trip_id"]
    env.svc.mark_done(tid, 1)
    reopened = ShoppingTripService(SqliteShoppingTripRepository(env.db), SqlitePlanRepository(env.db), lambda: NOW)
    view = reopened.get(tid)
    assert view["status"] == "done" and view["name"] == "Weekend" and view["version"] == 2
    assert [w["revision"] for w in view["weeks"]] == [1, 1]
