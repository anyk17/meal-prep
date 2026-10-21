"""HTTP tests for plan writes, history, capabilities, and authorization."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker, RefResolver

from mealplanner.entrypoints.http.app import create_app
from mealplanner.entrypoints.db_cli import import_inputs
from mealplanner.adapters.ortools import OrToolsPlanGenerator
from mealplanner.domain.solution import MealPlanSolution
from mealplanner.domain.planner_inputs import PlannerInputs

ROOT = Path(__file__).resolve().parents[2]
SPEC = yaml.safe_load((ROOT / "docs/migration-to-cloud/openapi.yaml").read_text())
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)  # Wednesday in Bogota
FUTURE = "2026-10-26"
FUTURE_ID = "plan_2026_10_26"
KEY = {"Idempotency-Key": "key-1"}
LAN = {"REMOTE_ADDR": "192.168.1.20"}

_cache: dict[int, MealPlanSolution] = {}


class CachedGenerator:
    """PlanGenerator that memoises the real OR-Tools solution per seed (speed only)."""

    def __init__(self, max_time_seconds: float | None = None) -> None:
        self.max_time_seconds = max_time_seconds

    def generate(self, inputs: PlannerInputs, seed: int) -> MealPlanSolution:
        if seed not in _cache:
            _cache[seed] = OrToolsPlanGenerator().generate(inputs, seed)
        return _cache[seed]


stub_solver = CachedGenerator()


def check_schema(name: str, instance) -> None:
    validator = Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}"},
        resolver=RefResolver.from_schema(SPEC),
        format_checker=FormatChecker(),
    )
    errors = list(validator.iter_errors(instance))
    assert not errors, [e.message for e in errors]


def ok(response, status: int, schema: str | None = None):
    assert response.status_code == status, response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"
    if schema:
        check_schema(schema, response.get_json())
    return response.get_json()


def fail(response, status: int, code: str):
    assert response.status_code == status, response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"
    body = response.get_json()
    check_schema("ErrorResponse", body)
    assert body["error"]["code"] == code
    return body["error"]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "plans.db"
    import_inputs(path, ROOT / "data")
    return path


@pytest.fixture
def app(db_path, clock):
    return create_app(db_path=db_path, clock=clock, generator=stub_solver)


@pytest.fixture
def client(app):
    return app.test_client()


def make_plan(client, seed: int = 1) -> dict:
    return ok(client.post("/api/v1/plans", json={"week_start": FUTURE, "seed": seed}), 201, "Plan")


def propose(client, revision: int = 1, seed: int | None = 2) -> dict:
    body: dict = {"expected_revision": revision}
    if seed is not None:
        body["seed"] = seed
    return ok(client.post(f"/api/v1/plans/{FUTURE_ID}/proposals", json=body), 201, "Proposal")


def accept_url(proposal: dict) -> str:
    return f"/api/v1/plans/{proposal['plan_id']}/proposals/{proposal['proposal_id']}/accept"


# ---- create



def test_create_without_seed_gives_each_week_its_own_plan(client) -> None:
    """Regression: the route defaulted the seed to 42, so every created week was identical."""
    from mealplanner.domain.month import week_seed

    weeks = [date.fromisoformat(FUTURE) + timedelta(days=7 * i) for i in range(2)]
    plans = [
        ok(client.post("/api/v1/plans", json={"week_start": w.isoformat()}), 201, "Plan") for w in weeks
    ]
    assert [p["seed"] for p in plans] == [week_seed(w) for w in weeks]
    meals = [[s["recipe_id"] for s in p["slots"]] for p in plans]
    assert meals[0] != meals[1]


def test_create_plan_then_duplicate_and_past(client) -> None:
    plan = make_plan(client)
    assert plan["plan_id"] == FUTURE_ID and plan["revision"] == 1
    fail(client.post("/api/v1/plans", json={"week_start": FUTURE}), 409, "duplicate_week")
    fail(client.post("/api/v1/plans", json={"week_start": "2026-10-12"}), 409, "week_not_editable")
    fail(client.post("/api/v1/plans", json={"week_start": "2026-10-27"}), 400, "invalid_week")
    read = ok(client.get(f"/api/v1/plans/{FUTURE_ID}"), 200, "Plan")
    assert read["snapshot_id"] == plan["snapshot_id"]


def test_create_location_header(client) -> None:
    response = client.post("/api/v1/plans", json={"week_start": FUTURE, "seed": 1})
    assert response.headers["Location"] == f"/api/v1/plans/{FUTURE_ID}"


# ---- propose / get / discard / accept / replay / undo


def test_propose_does_not_change_current_revision(client) -> None:
    make_plan(client)
    proposal = propose(client)
    assert proposal["base_revision"] == 1 and proposal["identical"] is False
    assert proposal["plan"]["revision"] == 2 and proposal["changes"]
    current = ok(client.get(f"/api/v1/plans/{FUTURE_ID}"), 200, "Plan")
    assert current["revision"] == 1
    fetched = ok(
        client.get(f"/api/v1/plans/{FUTURE_ID}/proposals/{proposal['proposal_id']}"), 200, "Proposal"
    )
    assert fetched == proposal


def test_discard_proposal(client) -> None:
    make_plan(client)
    proposal = propose(client)
    url = f"/api/v1/plans/{FUTURE_ID}/proposals/{proposal['proposal_id']}"
    response = client.delete(url)
    assert response.status_code == 204 and response.headers["Cache-Control"] == "no-store"
    fail(client.get(url), 404, "proposal_not_found")
    fail(client.delete(url), 404, "proposal_not_found")
    fail(client.post(accept_url(proposal), json={"expected_revision": 1}, headers=KEY), 404, "proposal_not_found")


def test_accept_replay_and_key_reuse(client) -> None:
    make_plan(client)
    proposal = propose(client)
    body = {"expected_revision": 1}
    first = ok(client.post(accept_url(proposal), json=body, headers=KEY), 200, "MutationResult")
    assert first["revision"] == 2 and first["changed"] and not first["replayed"]
    replay = ok(client.post(accept_url(proposal), json=body, headers=KEY), 200, "MutationResult")
    assert replay == {**first, "replayed": True}
    other = propose(client, revision=2, seed=3)
    fail(client.post(accept_url(other), json={"expected_revision": 2}, headers=KEY), 422, "retry_id_reused")
    current = ok(client.get(f"/api/v1/plans/{FUTURE_ID}"), 200, "Plan")
    assert current["revision"] == 2 and current["snapshot_id"] == first["snapshot_id"]


def test_stale_revision_has_expected_and_actual(client) -> None:
    make_plan(client)
    proposal = propose(client)
    ok(client.post(accept_url(proposal), json={"expected_revision": 1}, headers=KEY), 200)
    error = fail(
        client.post(f"/api/v1/plans/{FUTURE_ID}/proposals", json={"expected_revision": 1}),
        409,
        "stale_revision",
    )
    assert error["details"] == [
        {"field": "expected_revision", "message": "1"},
        {"field": "actual_revision", "message": "2"},
    ]
    fail(client.post(accept_url(propose(client, 2, 3)), json={"expected_revision": 1}, headers={"Idempotency-Key": "k2"}), 409, "stale_revision")


def test_expired_proposal(client, clock) -> None:
    make_plan(client)
    proposal = propose(client)
    clock.now = NOW + timedelta(hours=1)
    fail(
        client.get(f"/api/v1/plans/{FUTURE_ID}/proposals/{proposal['proposal_id']}"), 410, "proposal_expired"
    )
    fail(client.post(accept_url(proposal), json={"expected_revision": 1}, headers=KEY), 410, "proposal_expired")


def test_input_basis_changed(client, db_path) -> None:
    import sqlite3

    make_plan(client)
    proposal = propose(client)
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%pantry%'")]
    conn.close()
    if not tables:
        pytest.skip("pantry table not found")
    conn = sqlite3.connect(db_path)
    conn.execute(f"DELETE FROM {tables[0]} WHERE rowid IN (SELECT rowid FROM {tables[0]} LIMIT 1)")
    conn.commit()
    conn.close()
    fail(client.post(accept_url(proposal), json={"expected_revision": 1}, headers=KEY), 409, "input_basis_changed")


def test_undo_restores_revision_one_content(client) -> None:
    first = make_plan(client)
    proposal = propose(client)
    ok(client.post(accept_url(proposal), json={"expected_revision": 1}, headers=KEY), 200)
    fail(client.post(f"/api/v1/plans/{FUTURE_ID}/undo", json={"expected_revision": 1}, headers={"Idempotency-Key": "u0"}), 409, "stale_revision")
    result = ok(
        client.post(f"/api/v1/plans/{FUTURE_ID}/undo", json={"expected_revision": 2}, headers={"Idempotency-Key": "u1"}),
        200,
        "MutationResult",
    )
    assert result["revision"] == 3 and result["changed"]
    current = ok(client.get(f"/api/v1/plans/{FUTURE_ID}"), 200, "Plan")
    assert current["revision"] == 3 and current["slots"] == first["slots"]


def test_nothing_to_undo(client) -> None:
    make_plan(client)
    fail(client.post(f"/api/v1/plans/{FUTURE_ID}/undo", json={"expected_revision": 1}, headers=KEY), 409, "nothing_to_undo")


def test_revisions_list(client) -> None:
    make_plan(client)
    proposal = propose(client)
    ok(client.post(accept_url(proposal), json={"expected_revision": 1}, headers=KEY), 200)
    history = ok(client.get(f"/api/v1/plans/{FUTURE_ID}/revisions"), 200, "RevisionList")
    assert [(r["revision"], r["source"]) for r in history["items"]] == [(1, "create"), (2, "regenerate")]
    fail(client.get("/api/v1/plans/plan_missing/revisions"), 404, "plan_not_found")


def test_started_week_cannot_be_regenerated(client, clock) -> None:
    make_plan(client)
    clock.now = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
    fail(client.post(f"/api/v1/plans/{FUTURE_ID}/proposals", json={"expected_revision": 1}), 409, "week_not_editable")


# ---- validation

@pytest.mark.parametrize(
    "path,body,headers",
    [
        ("/api/v1/plans", {"week_start": FUTURE, "color": "red"}, {}),
        ("/api/v1/plans", {}, {}),
        ("/api/v1/plans", {"week_start": "2026-13-45"}, {}),
        ("/api/v1/plans", {"week_start": FUTURE, "seed": True}, {}),
        ("/api/v1/plans", {"week_start": FUTURE, "seed": -1}, {}),
        ("/api/v1/plans", [1], {}),
        (f"/api/v1/plans/{FUTURE_ID}/proposals", {"expected_revision": 0}, {}),
        (f"/api/v1/plans/{FUTURE_ID}/proposals", {"expected_revision": "1"}, {}),
        (f"/api/v1/plans/{FUTURE_ID}/proposals", {"expected_revision": 1, "x": 1}, {}),
        (f"/api/v1/plans/{FUTURE_ID}/undo", {"expected_revision": 1}, {}),  # key missing
        (f"/api/v1/plans/{FUTURE_ID}/undo", {"expected_revision": 1, "seed": 2}, KEY),
    ],
)
def test_strict_bodies(client, path, body, headers) -> None:
    error = fail(client.post(path, json=body, headers=headers), 400, "invalid_request")
    assert error["details"]


def test_non_json_body_rejected(client) -> None:
    fail(client.post("/api/v1/plans", data="week_start=2026-10-26"), 400, "invalid_request")


def test_bad_ids_rejected(client) -> None:
    fail(client.post("/api/v1/plans/bad id!/proposals", json={"expected_revision": 1}), 400, "invalid_request")
    fail(client.get("/api/v1/plans/p1/proposals/-x"), 400, "invalid_request")


# ---- capabilities and authorization


def test_capabilities(client, tmp_path) -> None:
    assert ok(client.get("/api/v1/capabilities"), 200, "Capabilities") == {"can_edit_plans": True, "can_edit_shopping_trips": True}
    json_app = create_app(week_start="2026-10-19")
    json_client = json_app.test_client()
    assert ok(json_client.get("/api/v1/capabilities"), 200, "Capabilities") == {"can_edit_plans": False, "can_edit_shopping_trips": False}


def test_json_mode_is_read_only(tmp_path) -> None:
    client = create_app(week_start="2026-10-19").test_client()
    fail(client.post("/api/v1/plans", json={"week_start": FUTURE}), 403, "read_only")
    fail(client.get("/api/v1/plans/plan_2026_10_19/revisions"), 403, "read_only")
    fail(client.delete("/api/v1/plans/plan_2026_10_19/proposals/prop_x"), 403, "read_only")
    assert client.get("/api/v1/plans/plan_2026_10_19").status_code == 200


def test_non_loopback_mutations_forbidden(client) -> None:
    make_plan(client)
    proposal = propose(client)
    for response in (
        client.post("/api/v1/plans", json={"week_start": "2026-11-02"}, environ_base=LAN),
        client.post(f"/api/v1/plans/{FUTURE_ID}/proposals", json={"expected_revision": 1}, environ_base=LAN),
        client.delete(f"/api/v1/plans/{FUTURE_ID}/proposals/{proposal['proposal_id']}", environ_base=LAN),
        client.post(accept_url(proposal), json={"expected_revision": 1}, headers=KEY, environ_base=LAN),
        client.post(f"/api/v1/plans/{FUTURE_ID}/undo", json={"expected_revision": 1}, headers=KEY, environ_base=LAN),
    ):
        fail(response, 403, "forbidden")
    current = ok(client.get(f"/api/v1/plans/{FUTURE_ID}"), 200, "Plan")
    assert current["revision"] == 1  # nothing happened


# ---- bounded generation


def test_generation_busy_when_gate_held(app, client) -> None:
    gate = app.config["GENERATION_GATE"]
    assert gate.try_acquire()
    try:
        response = client.post("/api/v1/plans", json={"week_start": FUTURE})
        fail(response, 503, "generation_busy")
        assert response.headers["Retry-After"]
        # reads and non-solver writes are unaffected
        assert client.get("/api/v1/capabilities").status_code == 200
    finally:
        gate.release()
    make_plan(client)  # released: works again


def test_generation_failure_maps_and_releases_gate(db_path, clock) -> None:
    class Broken:
        def generate(self, inputs, seed):
            raise RuntimeError("infeasible")

    client = create_app(db_path=db_path, clock=clock, generator=Broken()).test_client()
    for _ in range(2):  # a second attempt proves the gate was released
        fail(client.post("/api/v1/plans", json={"week_start": FUTURE}), 422, "generation_failed")


def test_solver_time_limit_is_wired(db_path, clock, monkeypatch) -> None:
    seen = {}

    class Spy(CachedGenerator):
        def __init__(self, max_time_seconds=None):
            seen["limit"] = max_time_seconds

    monkeypatch.setattr("mealplanner.adapters.ortools.OrToolsPlanGenerator", Spy)
    monkeypatch.setenv("MEALPLANNER_SOLVER_TIMEOUT_SECONDS", "12")
    client = create_app(db_path=db_path, clock=clock).test_client()
    make_plan(client)
    assert seen["limit"] == 12.0


def test_rule_violating_result_maps_to_rule_violation_and_stores_nothing(db_path, clock) -> None:
    class Violating:
        """Real solution with two lunches swapped: structurally valid, but breaks adjacency rules."""

        def generate(self, inputs, seed):
            solution = CachedGenerator().generate(inputs, 42)
            assignments = dict(solution.assignments)
            assignments["mon_lunch"], assignments["thu_lunch"] = (
                assignments["thu_lunch"],
                assignments["mon_lunch"],
            )
            return MealPlanSolution(assignments=assignments)

    client = create_app(db_path=db_path, clock=clock, generator=Violating()).test_client()
    response = client.post("/api/v1/plans", json={"week_start": FUTURE})
    error = fail(response, 422, "rule_violation")
    assert error["details"], error
    assert all(set(d) == {"field", "message"} for d in error["details"])
    assert client.get(f"/api/v1/plans/{FUTURE_ID}").status_code == 404


# ---- end to end with the real solver and wall clock


def test_end_to_end_real_solver(db_path) -> None:
    client = create_app(db_path=db_path, solver_timeout_seconds=60).test_client()
    monday = date.today() + timedelta(days=(7 - date.today().weekday()))
    week = monday.isoformat()
    plan = ok(client.post("/api/v1/plans", json={"week_start": week}), 201, "Plan")
    proposal = ok(
        client.post(f"/api/v1/plans/{plan['plan_id']}/proposals", json={"expected_revision": 1}), 201, "Proposal"
    )
    result = ok(
        client.post(
            f"/api/v1/plans/{plan['plan_id']}/proposals/{proposal['proposal_id']}/accept",
            json={"expected_revision": 1},
            headers=KEY,
        ),
        200,
        "MutationResult",
    )
    assert result["revision"] == 2
    ok(client.get(f"/api/v1/plans/{plan['plan_id']}/revisions"), 200, "RevisionList")
