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
        self.labels = INPUTS.shelf_lives()  # mutable: tests relabel to prove labels are read live
        self.now = NOW
        clock = lambda: self.now  # noqa: E731
        self.workflow = PlanWorkflowService(self.plans, Reader(), clock, generator=CachedGenerator())
        self.svc = ShoppingTripService(self.trip_repo, self.plans, clock, lambda: self.labels)

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
    assert all(
        c["state"] == "to_buy" and c["revision"] is None and not c["changed_since_shopping"]
        for w in view["weeks"] for c in w["cells"]
    )
    assert view["bought_items"] == []
    assert env.svc.get(view["trip_id"]) == view
    assert env.svc.list()["items"] == [view]
    named = env.svc.create([env.plan(W3)], name="  Big   shop ")
    assert named["name"] == "Big shop"



def test_unnamed_trip_name_follows_its_weeks(env: Env) -> None:
    p1, p2, p3 = env.plan(W1), env.plan(W2), env.plan(W3)
    auto = env.svc.create([p2])
    assert auto["name"] == "Nov 2 - Nov 8"
    auto = env.svc.add_week(auto["trip_id"], p1, auto["version"])
    assert auto["name"] == "Oct 26 - Nov 8"
    auto = env.svc.remove_week(auto["trip_id"], p2, auto["version"])
    assert env.svc.get(auto["trip_id"])["name"] == "Oct 26 - Nov 1"
    named = env.svc.create([p3], name="Big shop")
    assert env.svc.get(named["trip_id"])["name"] == "Big shop"


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
    for item in view["items"]:  # quantity_by_plan sums to quantity and names the contributing plans
        assert list(item["quantity_by_plan"]) == item["plan_ids"]
        assert round(sum(item["quantity_by_plan"].values()), 2) == item["quantity"]
    for pid in (p1, p2):
        per_plan = {
            (i["ingredient_id"], i["unit"]): i["quantity_by_plan"][pid]
            for i in view["items"] if pid in i["quantity_by_plan"]
        }
        assert per_plan == weekly_map(env, pid)


def test_sum_items_keeps_units_separate_and_sections() -> None:
    def line(ing, qty, unit, section="produce"):
        return {"ingredient_id": ing, "display": ing.title(), "quantity": qty, "unit": unit,
                "section": section, "notes": None}
    labels = {"tomato": "fresh", "milk": "fresh"}

    w1 = {"sections": {"produce": [line("tomato", 100.0, "g"), line("tomato", 2.0, "units")]}}
    w2 = {"sections": {"produce": [line("tomato", 50.5, "g")], "dairy": [line("milk", 200.0, "ml", "dairy")]}}
    items = trips.sum_items([("a", w1, trips.GROUPS), ("b", w2, trips.GROUPS)], labels)
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
            "INSERT INTO shopping_trips VALUES ('trip_b', 'x', 'open', 1, 'a', 'a', NULL, 0)"
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO shopping_trip_weeks (trip_id, plan_id, shelf_group, state)"
                " VALUES ('trip_b', ?, 'fresh', 'to_buy')",
                (p1,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO shopping_trip_weeks (trip_id, plan_id, shelf_group, state)"
                " VALUES ('trip_b', ?, 'frozen', 'to_buy')",
                (p2,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # bought needs its frozen fields
            conn.execute(
                "INSERT INTO shopping_trip_weeks (trip_id, plan_id, shelf_group, state)"
                " VALUES ('trip_b', ?, 'fresh', 'bought')",
                (p2,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                "INSERT INTO shopping_trip_weeks (trip_id, plan_id, shelf_group, state)"
                " VALUES ('trip_b', 'plan_missing', 'fresh', 'to_buy')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                "INSERT INTO shopping_trips VALUES ('trip_c', 'x', 'open', 1, 'a', 'a', NULL, 0)"
            )
            conn.execute(
                "INSERT INTO shopping_trip_weeks VALUES ('trip_c', ?, 'fresh', 'bought', 'x', 99, 'sha256:x')",
                (p2,),
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
    assert [(c["state"], c["revision"], c["changed_since_shopping"]) for w in done["weeks"] for c in w["cells"]] == [
        ("bought", 1, False)
    ] * 4
    assert done["weeks"][0]["cells"][0]["snapshot_id"] == old_snapshot
    assert done["items"] == [] and item_map({"items": done["bought_items"]}) == before

    env.new_revision(p1)  # plan edits never touch the trip
    assert env.plans.get_plan(p1).current_revision == 2
    after = env.svc.get(tid)
    assert after["version"] == 2 and after["items"] == []
    assert item_map({"items": after["bought_items"]}) == before  # frozen at revision 1
    week = after["weeks"][0]
    assert week["current_revision"] == 2
    assert [(c["state"], c["revision"], c["changed_since_shopping"]) for c in week["cells"]] == [("bought", 1, True)] * 2
    assert [c["changed_since_shopping"] for c in after["weeks"][1]["cells"]] == [False, False]
    assert weekly_map(env, p1, 2) != weekly_map(env, p1, 1)

    summaries = [
        {"plan_id": p1, "week_start": "2026-10-26", "week_end": "2026-11-01", "revision": 2},
        {"plan_id": p2, "week_start": "2026-11-02", "week_end": "2026-11-08", "revision": 1},
    ]
    cov = {i["plan_id"]: i for i in env.svc.coverage(summaries)["items"]}
    for group in trips.GROUPS:
        g = cov[p1]["groups"][group]
        assert (g["coverage"], g["changed_since_shopping"], g["trip_id"]) == ("bought", True, tid)
        assert cov[p2]["groups"][group]["changed_since_shopping"] is False


def test_editing_weeks_is_allowed_in_any_state_and_done_shortcuts(env: Env) -> None:
    p1, p2, p3 = env.plan(W1), env.plan(W2), env.plan(W3)
    tid = env.svc.create([p1, p2])["trip_id"]
    env.svc.mark_done(tid, 1)
    with pytest.raises(trips.InvalidTransition):  # nothing left to buy
        env.svc.mark_done(tid, 2)
    assert env.svc.get(tid)["version"] == 2
    # adding a week to a done list reopens it; the new cells are to buy, the old stay bought
    added = env.svc.add_week(tid, p3, 2)
    assert added["status"] == "open" and added["completed_at"] is None
    assert [c["state"] for w in added["weeks"] for c in w["cells"]] == ["bought"] * 4 + ["to_buy"] * 2
    assert {pid for i in added["items"] for pid in i["quantity_by_plan"]} == {p3}
    # removing a week works on an open list with bought cells; removing the to-buy week leaves it done
    removed = env.svc.remove_week(tid, p3, 3)
    assert removed["status"] == "done" and removed["completed_at"]
    removed = env.svc.remove_week(tid, p2, 4, group=FR)
    assert removed["status"] == "done"
    with pytest.raises(trips.InvalidTransition):
        env.svc.mark_done(tid, 5)


def test_reopen_follows_current_revisions(env: Env) -> None:
    p1, p2 = env.plan(W1), env.plan(W2)
    tid = env.svc.create([p1, p2])["trip_id"]
    env.svc.mark_done(tid, 1)
    env.new_revision(p1)
    reopened = env.svc.reopen(tid, 2)
    assert reopened["status"] == "open" and reopened["version"] == 3 and reopened["completed_at"] is None
    assert [(c["state"], c["revision"], c["changed_since_shopping"]) for w in reopened["weeks"] for c in w["cells"]] == [
        ("to_buy", None, False)
    ] * 4
    assert reopened["bought_items"] == []
    expected = {
        k: round(weekly_map(env, p1, 2).get(k, 0) + weekly_map(env, p2).get(k, 0), 2)
        for k in weekly_map(env, p1, 2).keys() | weekly_map(env, p2).keys()
    }
    assert item_map(reopened) == expected
    with pytest.raises(trips.InvalidTransition):
        env.svc.reopen(tid, 3)
    # weeks can be changed again, and done can be repeated against the new revision
    env.svc.remove_week(tid, p2, 3)
    redone = env.svc.mark_done(tid, 4)
    assert redone["weeks"][0]["cells"][0]["revision"] == 2
    assert not redone["weeks"][0]["cells"][0]["changed_since_shopping"]


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
    for group in trips.GROUPS:
        assert [
            (i["groups"][group]["coverage"], i["groups"][group]["trip_id"], i["groups"][group]["changed_since_shopping"])
            for i in items
        ] == [("to_buy", open_trip, False), ("bought", done_trip, False), ("not_covered", None, False)]
    assert [set(i["groups"]) for i in items] == [set(trips.GROUPS)] * 3
    assert env.svc.coverage([]) == {"items": []}


def test_persistence_across_instances(tmp_path) -> None:
    env = Env("sqlite", tmp_path)
    p1, p2 = env.plan(W1), env.plan(W2)
    tid = env.svc.create([p1, p2], name="Weekend")["trip_id"]
    env.svc.mark_done(tid, 1)
    reopened = ShoppingTripService(SqliteShoppingTripRepository(env.db), SqlitePlanRepository(env.db), lambda: NOW, INPUTS.shelf_lives)
    view = reopened.get(tid)
    assert view["status"] == "done" and view["name"] == "Weekend" and view["version"] == 2
    assert [c["revision"] for w in view["weeks"] for c in w["cells"]] == [1, 1, 1, 1]


# ---------------------------------------------------------------- shelf-life groups (phase 5b)

LL, FR = trips.LONG_LASTING, trips.FRESH


def group_cov(env: Env, plan_id: str, group: str, rev: int = 1) -> tuple[str, str | None, bool]:
    s = {"plan_id": plan_id, "week_start": "2026-10-26", "week_end": "2026-11-01", "revision": rev}
    g = env.svc.coverage([s])["items"][0]["groups"][group]
    return g["coverage"], g["trip_id"], g["changed_since_shopping"]


def test_default_is_both_groups_and_items_carry_shelf_life(env: Env) -> None:
    p1 = env.plan(W1)
    view = env.svc.create([p1])
    assert [w["groups"] for w in view["weeks"]] == [[LL, FR]]
    assert view["items"] and all(i["shelf_life"] == INPUTS.shelf_lives()[i["ingredient_id"]] for i in view["items"])
    assert {i["shelf_life"] for i in view["items"]} == {LL, FR}


def test_group_coverage_long_lasting_week_two_fresh_not_covered(env: Env) -> None:
    p1, p2 = env.plan(W1), env.plan(W2)
    # Typical month: list A has long-lasting for weeks 1-2 but fresh only for week 1.
    a = env.svc.create([p1], groups=[LL, FR])
    a = env.svc.add_week(a["trip_id"], p2, a["version"], groups=[LL])
    assert [w["groups"] for w in a["weeks"]] == [[LL, FR], [LL]]
    assert {i["shelf_life"] for i in a["items"] if p2 in i["plan_ids"]} == {LL}
    items = env.svc.coverage(
        [
            {"plan_id": p1, "week_start": "2026-10-26", "week_end": "2026-11-01", "revision": 1},
            {"plan_id": p2, "week_start": "2026-11-02", "week_end": "2026-11-08", "revision": 1},
        ]
    )["items"]
    assert items[0]["groups"][LL]["coverage"] == items[0]["groups"][FR]["coverage"] == "to_buy"
    assert items[1]["groups"][LL] == {"coverage": "to_buy", "trip_id": a["trip_id"], "changed_since_shopping": False}
    assert items[1]["groups"][FR] == {"coverage": "not_covered", "trip_id": None, "changed_since_shopping": False}
    # Fresh for week two goes on its own later list; the pair rule is per group.
    b = env.svc.create([p2], groups=[FR])
    assert group_cov(env, p2, FR)[:2] == ("to_buy", b["trip_id"])
    assert group_cov(env, p2, LL)[:2] == ("to_buy", a["trip_id"])
    env.svc.mark_done(b["trip_id"], 1)
    assert group_cov(env, p2, FR, rev=1) == ("bought", b["trip_id"], False)
    assert group_cov(env, p2, LL)[0] == "to_buy"
    env.new_revision(p2)
    assert group_cov(env, p2, FR, rev=2) == ("bought", b["trip_id"], True)
    assert group_cov(env, p2, LL, rev=2) == ("to_buy", a["trip_id"], False)


def test_create_and_add_with_groups_conflicts_per_group(env: Env) -> None:
    p1, p2 = env.plan(W1), env.plan(W2)
    a = env.svc.create([p1], groups=[LL])
    assert [w["groups"] for w in a["weeks"]] == [[LL]]
    assert {i["shelf_life"] for i in a["items"]} == {LL}
    b = env.svc.create([p1], groups=[FR])  # the other group is free
    with pytest.raises(trips.WeekAlreadyOnTrip) as excinfo:
        env.svc.create([p1], groups=[LL, FR])
    assert excinfo.value.plan_id == p1 and excinfo.value.group in (LL, FR)
    with pytest.raises(trips.WeekAlreadyOnTrip):
        env.svc.add_week(b["trip_id"], p1, 1, groups=[LL])  # LL is on trip a
    with pytest.raises(trips.WeekAlreadyOnTrip):
        env.svc.add_week(b["trip_id"], p1, 1, groups=[FR])  # nothing new to add to b
    # A failed create leaves nothing behind.
    assert len(env.svc.list()["items"]) == 2
    env.svc.create([p2])
    with pytest.raises(trips.InvalidTrip):
        env.svc.create([p2], groups=[])
    with pytest.raises(trips.InvalidTrip):
        env.svc.create([p2], groups=["frozen"])


def test_add_week_extends_groups_of_a_week_already_on_the_list(env: Env) -> None:
    p1 = env.plan(W1)
    a = env.svc.create([p1], groups=[FR])
    extended = env.svc.add_week(a["trip_id"], p1, a["version"], groups=[LL])
    assert extended["version"] == 2 and [w["groups"] for w in extended["weeks"]] == [[LL, FR]]
    assert len(extended["weeks"]) == 1


def test_remove_one_group_or_whole_week(env: Env) -> None:
    p1, p2 = env.plan(W1), env.plan(W2)
    a = env.svc.create([p1, p2])
    tid = a["trip_id"]
    only_ll = env.svc.remove_week(tid, p1, 1, group=FR)
    assert only_ll["version"] == 2 and [w["groups"] for w in only_ll["weeks"]] == [[LL], [LL, FR]]
    assert group_cov(env, p1, FR)[0] == "not_covered" and group_cov(env, p1, LL)[0] == "to_buy"
    with pytest.raises(trips.WeekNotOnTrip):
        env.svc.remove_week(tid, p1, 2, group=FR)  # already gone
    gone = env.svc.remove_week(tid, p1, 2, group=LL)  # no group left: the week entry disappears
    assert [w["plan_id"] for w in gone["weeks"]] == [p2]
    with pytest.raises(trips.LastWeek):
        env.svc.remove_week(tid, p2, 3)
    last = env.svc.remove_week(tid, p2, 3, group=FR)
    with pytest.raises(trips.LastWeek):
        env.svc.remove_week(tid, p2, last["version"], group=LL)  # would leave an empty list
    with pytest.raises(trips.WeekNotOnTrip):
        env.svc.remove_week(tid, p2, last["version"], group="frozen")


def test_labels_are_read_from_current_inputs_not_snapshots(env: Env) -> None:
    p1 = env.plan(W1)
    ll_only = env.svc.create([p1], groups=[LL])
    before = {i["ingredient_id"] for i in ll_only["items"]}
    assert "chicken" in before and "rice" in before
    env.labels["chicken"] = FR  # relabel an ingredient: no new plan revision needed
    after = env.svc.get(ll_only["trip_id"])
    assert env.plans.get_plan(p1).current_revision == 1
    ids = {i["ingredient_id"] for i in after["items"]}
    assert "chicken" not in ids and "rice" in ids
    assert all(i["shelf_life"] == LL for i in after["items"])
    # and the bought cells regroup too
    env.svc.mark_done(ll_only["trip_id"], ll_only["version"])
    bought = lambda: {i["ingredient_id"] for i in env.svc.get(ll_only["trip_id"])["bought_items"]}  # noqa: E731
    assert "chicken" not in bought() and "rice" in bought()
    env.labels["chicken"] = LL
    assert "chicken" in bought()


def test_group_items_sum_to_the_full_list(env: Env) -> None:
    p1 = env.plan(W1)
    full = item_map(env.svc.create([p1]))
    assert full == {k: v for k, v in weekly_map(env, p1).items()}
    env.svc.delete(env.svc.list()["items"][0]["trip_id"], 1)
    ll = item_map(env.svc.create([p1], groups=[LL]))
    fresh_id = env.svc.create([p1], groups=[FR])
    assert {**ll, **item_map(fresh_id)} == full and not ll.keys() & item_map(fresh_id).keys()


def test_sqlite_unique_plan_group(tmp_path) -> None:
    env = Env("sqlite", tmp_path)
    p1 = env.plan(W1)
    env.trip_repo.create(trips.new_trip("trip_a", None, [trips.TripWeek(p1, W1, groups=(LL,))], NOW))
    env.trip_repo.create(trips.new_trip("trip_b", None, [trips.TripWeek(p1, W1, groups=(FR,))], NOW))  # other group
    with pytest.raises(trips.WeekAlreadyOnTrip) as excinfo:
        env.trip_repo.create(trips.new_trip("trip_c", None, [trips.TripWeek(p1, W1, groups=(FR, LL))], NOW))
    assert excinfo.value.group in (LL, FR)
    conn = connect(env.db)
    try:
        conn.execute("INSERT INTO shopping_trips VALUES ('trip_x', 'x', 'open', 1, 'a', 'a', NULL, 0)")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO shopping_trip_weeks (trip_id, plan_id, shelf_group, state)"
                " VALUES ('trip_x', ?, 'long_lasting', 'to_buy')",
                (p1,),
            )
    finally:
        conn.close()


def test_memory_and_sqlite_save_group_changes(env: Env) -> None:
    p1 = env.plan(W1)
    a = env.svc.create([p1], groups=[LL])
    env.svc.add_week(a["trip_id"], p1, 1, groups=[FR])
    b = env.svc.create([env.plan(W2)])
    with pytest.raises(trips.WeekAlreadyOnTrip):  # the stored pair is held by trip a
        env.svc.add_week(b["trip_id"], p1, 1, groups=[LL])
    assert env.svc.get(a["trip_id"])["weeks"][0]["groups"] == [LL, FR]


def apply_version(conn, version: int) -> None:
    """Apply exactly one migration (the database must be at version - 1)."""
    from mealplanner.adapters.sqlite import migrations

    _, name, steps = migrations.MIGRATIONS[version - 1]
    with migrations.transaction(conn):
        for step in steps:
            step(conn) if callable(step) else conn.execute(step)
        conn.execute("INSERT INTO schema_version VALUES (?, ?, 'x')", (version, name))


def test_migration_6_backfills_labels_and_expands_memberships(tmp_path) -> None:
    import json

    from mealplanner.adapters.sqlite import migrations

    path = tmp_path / "v5.db"
    conn = migrations.connect(path)
    for version, name, steps in migrations.MIGRATIONS[:5]:
        with migrations.transaction(conn):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            for step in steps:
                conn.execute(step)
            conn.execute("INSERT INTO schema_version VALUES (?, ?, 'x')", (version, name))
    with migrations.transaction(conn):
        for position, (ing, label_free) in enumerate({"chicken": {}, "carrot": {}, "rice": {}}.items()):
            conn.execute(
                "INSERT INTO ingredients VALUES (?, ?, ?)",
                (ing, position, json.dumps({"id": ing, "name": ing, **label_free})),
            )
        for plan_id, week in (("plan_a", "2026-10-26"), ("plan_b", "2026-11-02")):
            conn.execute("INSERT INTO plans VALUES (?, 'default', ?, 'America/Bogota', 1)", (plan_id, week))
            conn.execute(
                "INSERT INTO plan_revisions VALUES (?, 1, 'sha256:s', '{}', '{}', '[]', 'basis', 'generated', 'x')",
                (plan_id,),
            )
        conn.execute("INSERT INTO shopping_trips VALUES ('t_open', 'o', 'open', 1, 'a', 'a', NULL, 0)")
        conn.execute("INSERT INTO shopping_trips VALUES ('t_done', 'd', 'done', 2, 'a', 'a', 'b', 0)")
        conn.execute("INSERT INTO shopping_trip_weeks VALUES ('t_open', 'plan_a', NULL, NULL)")
        conn.execute("INSERT INTO shopping_trip_weeks VALUES ('t_done', 'plan_b', 1, 'sha256:s')")
    apply_version(conn, 6)
    rows = conn.execute(
        "SELECT trip_id, plan_id, shelf_group, covered_revision, covered_snapshot_id"
        " FROM shopping_trip_weeks ORDER BY trip_id, shelf_group"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("t_done", "plan_b", "fresh", 1, "sha256:s"),
        ("t_done", "plan_b", "long_lasting", 1, "sha256:s"),
        ("t_open", "plan_a", "fresh", None, None),
        ("t_open", "plan_a", "long_lasting", None, None),
    ]
    labels = {
        r["ingredient_id"]: json.loads(r["content"])["shelf_life"]
        for r in conn.execute("SELECT ingredient_id, content FROM ingredients")
    }
    assert labels == {"chicken": "long_lasting", "carrot": "fresh", "rice": "long_lasting"}
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute("INSERT INTO shopping_trips VALUES ('t_new', 'n', 'open', 1, 'a', 'a', NULL, 0)")
        conn.execute("INSERT INTO shopping_trip_weeks VALUES ('t_new', 'plan_a', 'fresh', NULL, NULL)")
    conn.close()


def test_migration_6_fails_on_an_ingredient_without_a_shipped_label(tmp_path) -> None:
    import json

    from mealplanner.adapters.sqlite import migrations

    path = tmp_path / "v5.db"
    conn = migrations.connect(path)
    for version, name, steps in migrations.MIGRATIONS[:5]:
        with migrations.transaction(conn):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            for step in steps:
                conn.execute(step)
            conn.execute("INSERT INTO schema_version VALUES (?, ?, 'x')", (version, name))
    with migrations.transaction(conn):
        conn.execute("INSERT INTO ingredients VALUES ('mystery', 0, ?)", (json.dumps({"id": "mystery"}),))
    with pytest.raises(migrations.DatabaseError, match="mystery"):
        migrations.migrate(conn)
    assert migrations.current_version(conn) == 5
    conn.close()


# ---------------------------------------------------------------- bought cells (phase 5c)


def cell_states(view: dict) -> dict[tuple[str, str], str]:
    return {(w["plan_id"], c["group"]): c["state"] for w in view["weeks"] for c in w["cells"]}


def bought_map(view: dict) -> dict[tuple[str, str], float]:
    return {(i["ingredient_id"], i["unit"]): i["quantity"] for i in view["bought_items"]}


def labelled(weekly: dict[tuple[str, str], float], group: str) -> dict[tuple[str, str], float]:
    return {k: v for k, v in weekly.items() if INPUTS.shelf_lives()[k[0]] == group}


def test_mark_a_subset_bought_totals_exclude_bought_cells(env: Env) -> None:
    p1, p2 = env.plan(W1), env.plan(W2)
    trip = env.svc.create([p1, p2])
    tid = trip["trip_id"]
    marked = env.svc.mark_purchases(tid, [(p1, LL), (p2, LL)], 1)
    assert marked["version"] == 2 and marked["status"] == "open" and marked["completed_at"] is None
    assert cell_states(marked) == {(p1, LL): "bought", (p1, FR): "to_buy", (p2, LL): "bought", (p2, FR): "to_buy"}
    # To-buy totals are only the fresh rows; bought totals only the long-lasting rows.
    fresh_total = {
        k: round(labelled(weekly_map(env, p1), FR).get(k, 0) + labelled(weekly_map(env, p2), FR).get(k, 0), 2)
        for k in labelled(weekly_map(env, p1), FR).keys() | labelled(weekly_map(env, p2), FR).keys()
    }
    assert item_map(marked) == fresh_total
    assert {i["shelf_life"] for i in marked["items"]} == {FR}
    assert {i["shelf_life"] for i in marked["bought_items"]} == {LL}
    assert {**item_map(marked), **bought_map(marked)} == item_map(trip)  # nothing lost, nothing counted twice
    assert marked["weeks"][0]["cells"][0]["bought_at"] == NOW.isoformat()
    # bought cells carry their frozen revision and snapshot, to-buy cells none
    bought_cell, to_buy_cell = marked["weeks"][0]["cells"]
    assert (bought_cell["revision"], bought_cell["snapshot_id"]) == (
        1, env.plans.get_revision(p1, 1).snapshot.plan["snapshot_id"]
    )
    assert (to_buy_cell["revision"], to_buy_cell["snapshot_id"], to_buy_cell["bought_at"]) == (None, None, None)
    assert env.svc.get(tid) == marked


def test_bought_items_are_frozen_and_changed_since_shopping_is_per_cell(env: Env) -> None:
    p1, p2 = env.plan(W1), env.plan(W2)
    tid = env.svc.create([p1, p2])["trip_id"]
    marked = env.svc.mark_purchases(tid, [(p1, LL), (p2, FR)], 1)
    frozen_bought = bought_map(marked)
    env.new_revision(p1)  # plan 1 moves to revision 2
    after = env.svc.get(tid)
    assert bought_map(after) == frozen_bought  # frozen at revision 1 for both cells
    w1, w2 = after["weeks"]
    assert w1["current_revision"] == 2
    assert [(c["group"], c["state"], c["changed_since_shopping"]) for c in w1["cells"]] == [
        (LL, "bought", True), (FR, "to_buy", False)
    ]
    assert [(c["group"], c["state"], c["changed_since_shopping"]) for c in w2["cells"]] == [
        (LL, "to_buy", False), (FR, "bought", False)
    ]
    # the to-buy fresh cell of plan 1 follows revision 2 without being re-added or frozen
    expected_fresh_p1 = labelled(weekly_map(env, p1, 2), FR)
    only_p1 = {i["ingredient_id"]: i["quantity_by_plan"].get(p1) for i in after["items"] if p1 in i["quantity_by_plan"]}
    assert only_p1 == {ing: q for (ing, _), q in expected_fresh_p1.items()}
    # buying the changed week's long-lasting cell again is not possible; undo then mark refreezes at 2
    with pytest.raises(trips.InvalidTransition):
        env.svc.mark_purchases(tid, [(p1, LL)], after["version"])
    undone = env.svc.unmark_purchases(tid, [(p1, LL)], after["version"])
    assert undone["weeks"][0]["cells"][0]["changed_since_shopping"] is False
    remarked = env.svc.mark_purchases(tid, [(p1, LL)], undone["version"])
    assert (remarked["weeks"][0]["cells"][0]["revision"], remarked["weeks"][0]["cells"][0]["changed_since_shopping"]) == (2, False)
    cov = {i["plan_id"]: i["groups"] for i in env.svc.coverage([
        {"plan_id": p1, "week_start": "2026-10-26", "week_end": "2026-11-01", "revision": 3},
        {"plan_id": p2, "week_start": "2026-11-02", "week_end": "2026-11-08", "revision": 1},
    ])["items"]}
    assert cov[p1][LL] == {"coverage": "bought", "trip_id": tid, "changed_since_shopping": True}
    assert cov[p1][FR] == {"coverage": "to_buy", "trip_id": tid, "changed_since_shopping": False}
    assert cov[p2][FR]["coverage"] == "bought" and cov[p2][LL]["coverage"] == "to_buy"


def test_undo_returns_cells_to_the_current_revision(env: Env) -> None:
    p1 = env.plan(W1)
    created = env.svc.create([p1])
    tid = created["trip_id"]
    marked = env.svc.mark_purchases(tid, [(p1, LL), (p1, FR)], 1)
    assert marked["status"] == "done" and marked["items"] == []
    env.new_revision(p1)
    undone = env.svc.unmark_purchases(tid, [(p1, FR)], 2)
    assert undone["status"] == "open" and undone["completed_at"] is None
    assert cell_states(undone) == {(p1, LL): "bought", (p1, FR): "to_buy"}
    fresh = undone["weeks"][0]["cells"][1]
    assert (fresh["revision"], fresh["snapshot_id"], fresh["bought_at"]) == (None, None, None)
    assert item_map(undone) == labelled(weekly_map(env, p1, 2), FR)  # current revision again
    assert bought_map(undone) == labelled(weekly_map(env, p1, 1), LL)
    with pytest.raises(trips.InvalidTransition):  # already to buy
        env.svc.unmark_purchases(tid, [(p1, FR)], 3)


def test_status_and_completed_at_are_derived(env: Env) -> None:
    p1, p2 = env.plan(W1), env.plan(W2)
    tid = env.svc.create([p1, p2])["trip_id"]
    step = env.svc.mark_purchases(tid, [(p1, LL), (p1, FR)], 1)
    assert (step["status"], step["completed_at"]) == ("open", None)
    env.now = NOW + timedelta(hours=1)
    step = env.svc.mark_purchases(tid, [(p2, LL)], 2)
    env.now = NOW + timedelta(hours=2)
    step = env.svc.mark_purchases(tid, [(p2, FR)], 3)
    assert step["status"] == "done"
    assert step["completed_at"] == (NOW + timedelta(hours=2)).isoformat()  # latest bought_at
    env.now = NOW + timedelta(hours=3)
    step = env.svc.unmark_purchases(tid, [(p2, LL)], 4)
    assert (step["status"], step["completed_at"]) == ("open", None)
    step = env.svc.mark_purchases(tid, [(p2, LL)], 5)
    assert step["status"] == "done" and step["completed_at"] == (NOW + timedelta(hours=3)).isoformat()
    # removing the only to-buy cell of an open list makes it done (derived), without any new mark
    p3 = env.plan(W3)
    other = env.svc.create([p3])["trip_id"]
    env.svc.mark_purchases(other, [(p3, LL)], 1)
    left = env.svc.remove_week(other, p3, 2, group=FR)
    assert left["status"] == "done" and left["completed_at"] == (NOW + timedelta(hours=3)).isoformat()


def test_purchases_are_all_or_nothing_and_validated(env: Env) -> None:
    p1, p2 = env.plan(W1), env.plan(W2)
    tid = env.svc.create([p1], groups=[LL])["trip_id"]
    with pytest.raises(trips.WeekNotOnTrip):  # fresh is not on the trip
        env.svc.mark_purchases(tid, [(p1, LL), (p1, FR)], 1)
    with pytest.raises(trips.WeekNotOnTrip):
        env.svc.mark_purchases(tid, [(p2, LL)], 1)
    with pytest.raises(trips.InvalidTrip):
        env.svc.mark_purchases(tid, [], 1)
    with pytest.raises(trips.InvalidTrip):
        env.svc.mark_purchases(tid, [(p1, LL), (p1, LL)], 1)
    with pytest.raises(trips.StaleVersion):
        env.svc.mark_purchases(tid, [(p1, LL)], 7)
    with pytest.raises(trips.TripNotFound):
        env.svc.mark_purchases("trip_missing", [(p1, LL)], 1)
    after = env.svc.get(tid)
    assert after["version"] == 1 and cell_states(after) == {(p1, LL): "to_buy"}
    env.svc.mark_purchases(tid, [(p1, LL)], 1)
    with pytest.raises(trips.InvalidTransition):
        env.svc.mark_purchases(tid, [(p1, LL)], 2)
    two = env.svc.create([p2])["trip_id"]
    env.svc.mark_purchases(two, [(p2, LL)], 1)
    with pytest.raises(trips.InvalidTransition):  # one bad cell rejects the whole request
        env.svc.mark_purchases(two, [(p2, FR), (p2, LL)], 2)
    assert cell_states(env.svc.get(two)) == {(p2, LL): "bought", (p2, FR): "to_buy"}


def test_reopen_shortcut_returns_all_bought_cells(env: Env) -> None:
    p1 = env.plan(W1)
    tid = env.svc.create([p1])["trip_id"]
    with pytest.raises(trips.InvalidTransition):  # nothing bought yet
        env.svc.reopen(tid, 1)
    env.svc.mark_purchases(tid, [(p1, LL)], 1)
    done = env.svc.mark_done(tid, 2)  # marks only the remaining to-buy cell
    assert done["status"] == "done"
    assert [c["bought_at"] for c in done["weeks"][0]["cells"]] == [NOW.isoformat()] * 2
    reopened = env.svc.reopen(tid, 3)
    assert reopened["status"] == "open" and cell_states(reopened) == {(p1, LL): "to_buy", (p1, FR): "to_buy"}


def test_quantity_by_plan_sums_to_quantity_for_items_and_bought_items(env: Env) -> None:
    p1, p2 = env.plan(W1, seed=1), env.plan(W2, seed=2)
    tid = env.svc.create([p1, p2])["trip_id"]
    view = env.svc.mark_purchases(tid, [(p1, FR), (p2, LL)], 1)
    for items in (view["items"], view["bought_items"]):
        assert items
        for item in items:
            assert round(sum(item["quantity_by_plan"].values()), 2) == item["quantity"]
            assert set(item["quantity_by_plan"]) == set(item["plan_ids"]) <= {p1, p2}
    assert {i["shelf_life"] for i in view["bought_items"] if p1 in i["quantity_by_plan"]} == {FR}


def test_sum_items_quantity_by_plan_rounds_per_plan() -> None:
    line = lambda q: {"ingredient_id": "x", "display": "X", "quantity": q, "unit": "g", "section": "s"}  # noqa: E731
    week = lambda q: {"sections": {"s": [line(q)]}}  # noqa: E731
    (item,) = trips.sum_items([("a", week(0.004), (FR,)), ("b", week(0.004), (FR,))], {"x": FR})
    assert item["quantity_by_plan"] == {"a": 0.0, "b": 0.0} and item["quantity"] == 0.0
    (item,) = trips.sum_items([("a", week(0.1), (FR,)), ("a", week(0.2), (FR,))], {"x": FR})
    assert item["quantity_by_plan"] == {"a": 0.3} and item["plan_ids"] == ["a"]


def test_merge_cells_keeps_single_pass_order() -> None:
    shopping = {"sections": {"s": []}}
    merged = trips.merge_cells([("a", None, shopping, LL), ("b", None, shopping, FR), ("a", None, shopping, FR), ("a", 1, shopping, FR)])
    assert [(p, g) for p, _, g in merged] == [("a", [LL, FR]), ("b", [FR]), ("a", [FR])]


# ---------------------------------------------------------------- migration 7


def build_v6_database(path: Path):
    """A version-6 database with one open and one done list (real-looking, hand-built)."""
    from mealplanner.adapters.sqlite import migrations

    conn = migrations.connect(path)
    for version, name, steps in migrations.MIGRATIONS[:6]:
        with migrations.transaction(conn):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            for step in steps:
                if callable(step):
                    step(conn)  # no stored ingredients: nothing to label
                else:
                    conn.execute(step)
            conn.execute("INSERT INTO schema_version VALUES (?, ?, 'x')", (version, name))
    with migrations.transaction(conn):
        for plan_id, week, current in (("plan_a", "2026-10-26", 2), ("plan_b", "2026-11-02", 1), ("plan_c", "2026-11-09", 1)):
            conn.execute("INSERT INTO plans VALUES (?, 'default', ?, 'America/Bogota', ?)", (plan_id, week, 1))
            conn.execute(
                "INSERT INTO plan_revisions VALUES (?, 1, ?, '{}', '{}', '[]', 'basis', 'generated', 'x')",
                (plan_id, f"sha256:{plan_id}1"),
            )
            if current == 2:
                conn.execute(
                    "INSERT INTO plan_revisions VALUES (?, 2, ?, '{}', '{}', '[]', 'basis', 'edited', 'x')",
                    (plan_id, f"sha256:{plan_id}2"),
                )
                conn.execute("UPDATE plans SET current_revision = 2 WHERE plan_id = ?", (plan_id,))
        conn.execute("INSERT INTO shopping_trips VALUES ('t_open', 'o', 'open', 3, '2026-10-20T10:00:00+00:00', '2026-10-20T10:00:00+00:00', NULL, 0)")
        conn.execute("INSERT INTO shopping_trips VALUES ('t_done', 'd', 'done', 2, '2026-10-20T10:00:00+00:00', '2026-10-21T09:00:00+00:00', '2026-10-21T09:00:00+00:00', 0)")
        conn.execute("INSERT INTO shopping_trip_weeks VALUES ('t_open', 'plan_c', 'long_lasting', NULL, NULL)")
        conn.execute("INSERT INTO shopping_trip_weeks VALUES ('t_open', 'plan_c', 'fresh', NULL, NULL)")
        # the done list froze plan_a at revision 1 (the plan has since moved to 2) and plan_b at 1
        conn.execute("INSERT INTO shopping_trip_weeks VALUES ('t_done', 'plan_a', 'long_lasting', 1, 'sha256:plan_a1')")
        conn.execute("INSERT INTO shopping_trip_weeks VALUES ('t_done', 'plan_a', 'fresh', 1, 'sha256:plan_a1')")
        conn.execute("INSERT INTO shopping_trip_weeks VALUES ('t_done', 'plan_b', 'fresh', 1, 'sha256:plan_b1')")
    assert migrations.current_version(conn) == 6
    return conn


def test_migration_7_converts_open_and_done_lists(tmp_path) -> None:
    from mealplanner.adapters.sqlite import migrations

    conn = build_v6_database(tmp_path / "v6.db")
    assert migrations.migrate(conn) == [7]
    assert migrations.current_version(conn) == 7
    rows = [
        tuple(r)
        for r in conn.execute(
            "SELECT trip_id, plan_id, shelf_group, state, bought_at, revision, snapshot_id"
            " FROM shopping_trip_weeks ORDER BY trip_id, plan_id, shelf_group"
        )
    ]
    done_at = "2026-10-21T09:00:00+00:00"
    assert rows == [
        ("t_done", "plan_a", "fresh", "bought", done_at, 1, "sha256:plan_a1"),
        ("t_done", "plan_a", "long_lasting", "bought", done_at, 1, "sha256:plan_a1"),
        ("t_done", "plan_b", "fresh", "bought", done_at, 1, "sha256:plan_b1"),
        ("t_open", "plan_c", "fresh", "to_buy", None, None, None),
        ("t_open", "plan_c", "long_lasting", "to_buy", None, None, None),
    ]
    # list rows survive untouched (name, version, created/updated)
    assert [tuple(r) for r in conn.execute("SELECT trip_id, name, version FROM shopping_trips ORDER BY trip_id")] == [
        ("t_done", "d", 2), ("t_open", "o", 3)
    ]
    conn.close()
    # the migrated database reads through the repository: statuses are derived from the cells
    repo = SqliteShoppingTripRepository(tmp_path / "v6.db")
    done, open_ = repo.get("t_done"), repo.get("t_open")
    assert (done.status, open_.status) == ("done", "open")
    assert done.completed_at == datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
    assert open_.completed_at is None
    assert [(w.plan_id, w.groups) for w in done.weeks] == [("plan_a", (LL, FR)), ("plan_b", (FR,))]
    assert all(c.state == trips.BOUGHT and c.revision == 1 for _, c in done.all_cells)
    # saving a derived change writes consistent status/completed_at columns
    repo.save(trips.unmark_bought(done, [("plan_b", FR)], done.version, NOW), done.version)
    check = connect(tmp_path / "v6.db")
    assert tuple(check.execute("SELECT status, completed_at FROM shopping_trips WHERE trip_id = 't_done'").fetchone()) == ("open", None)
    check.close()


def test_migration_7_is_atomic_and_leaves_version_6_data_on_failure(tmp_path) -> None:
    from mealplanner.adapters.sqlite import migrations

    conn = build_v6_database(tmp_path / "v6.db")
    # a done list whose week has no frozen revision falls back to the plan's current revision
    conn.execute("UPDATE shopping_trip_weeks SET covered_revision = NULL, covered_snapshot_id = NULL WHERE plan_id = 'plan_b'")
    assert migrations.migrate(conn) == [7]
    row = conn.execute("SELECT state, revision, snapshot_id FROM shopping_trip_weeks WHERE plan_id = 'plan_b'").fetchone()
    assert tuple(row) == ("bought", 1, "sha256:plan_b1")
    conn.close()
    # a broken step rolls everything back, leaving the version-6 tables intact
    conn = build_v6_database(tmp_path / "v6b.db")
    name, steps = migrations.MIGRATIONS[6][1:]
    broken = (*steps[:-1], "ALTER TABLE does_not_exist RENAME TO x")
    saved = migrations.MIGRATIONS
    try:
        migrations.MIGRATIONS = (*saved[:6], (7, name, broken))
        with pytest.raises(sqlite3.OperationalError):
            migrations.migrate(conn)
    finally:
        migrations.MIGRATIONS = saved
    assert migrations.current_version(conn) == 6
    assert conn.execute("SELECT COUNT(*) FROM shopping_trip_weeks WHERE covered_revision IS NOT NULL").fetchone()[0] == 3
    conn.close()
