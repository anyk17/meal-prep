"""HTTP tests for shopping trip routes, coverage, gating, and error codes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker, RefResolver

from mealplanner.adapters.ortools import OrToolsPlanGenerator
from mealplanner.domain.planner_inputs import PlannerInputs
from mealplanner.domain.solution import MealPlanSolution
from mealplanner.entrypoints.db_cli import import_inputs
from mealplanner.entrypoints.http.app import create_app

ROOT = Path(__file__).resolve().parents[2]
SPEC = yaml.safe_load((ROOT / "docs/migration-to-cloud/openapi.yaml").read_text())
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
LAN = {"REMOTE_ADDR": "192.168.1.20"}
P1, P2, P3 = "plan_2026_10_26", "plan_2026_11_02", "plan_2026_11_09"
BASE = "/api/v1/shopping-trips"

_cache: dict[int, MealPlanSolution] = {}


class CachedGenerator:
    def __init__(self, max_time_seconds: float | None = None) -> None:
        pass

    def generate(self, inputs: PlannerInputs, seed: int) -> MealPlanSolution:
        if seed not in _cache:
            _cache[seed] = OrToolsPlanGenerator().generate(inputs, seed)
        return _cache[seed]


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


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "plans.db"
    import_inputs(path, ROOT / "data")
    return path


@pytest.fixture
def client(db_path):
    app = create_app(db_path=db_path, clock=lambda: NOW, generator=CachedGenerator())
    client = app.test_client()
    for week in ("2026-10-26", "2026-11-02", "2026-11-09"):
        ok(client.post("/api/v1/plans", json={"week_start": week, "seed": 1}), 201, "Plan")
    return client


def make_trip(client, plan_ids=(P1, P2), **extra) -> dict:
    return ok(client.post(BASE, json={"plan_ids": list(plan_ids), **extra}), 201, "ShoppingTrip")


def test_create_get_list(client) -> None:
    assert ok(client.get(BASE), 200, "ShoppingTripList") == {"items": []}
    response = client.post(BASE, json={"plan_ids": [P2, P1], "name": "Big shop"})
    trip = ok(response, 201, "ShoppingTrip")
    assert response.headers["Location"] == f"{BASE}/{trip['trip_id']}"
    assert trip["name"] == "Big shop" and trip["version"] == 1 and trip["status"] == "open"
    assert [w["plan_id"] for w in trip["weeks"]] == [P1, P2] and trip["items"]
    assert ok(client.get(f"{BASE}/{trip['trip_id']}"), 200, "ShoppingTrip") == trip
    assert ok(client.get(BASE), 200, "ShoppingTripList") == {"items": [trip]}


def test_full_lifecycle_and_coverage(client) -> None:
    trip = make_trip(client, [P1])
    url = f"{BASE}/{trip['trip_id']}"
    trip = ok(client.post(f"{url}/weeks", json={"plan_id": P2, "expected_version": 1}), 200, "ShoppingTrip")
    assert trip["version"] == 2 and len(trip["weeks"]) == 2
    trip = ok(client.delete(f"{url}/weeks/{P2}?expected_version=2"), 200, "ShoppingTrip")
    assert trip["version"] == 3 and len(trip["weeks"]) == 1
    trip = ok(client.post(f"{url}/done", json={"expected_version": 3}), 200, "ShoppingTrip")
    assert trip["status"] == "done" and trip["completed_at"]
    cov = ok(client.get("/api/v1/coverage?from=2026-10-26&to=2026-11-15"), 200, "CoverageList")["items"]
    assert [(i["plan_id"], i["coverage"], i["trip_id"]) for i in cov] == [
        (P1, "covered", trip["trip_id"]), (P2, "not_covered", None), (P3, "not_covered", None)
    ]
    trip = ok(client.post(f"{url}/reopen", json={"expected_version": 4}), 200, "ShoppingTrip")
    assert trip["status"] == "open" and trip["version"] == 5
    cov = ok(client.get("/api/v1/coverage?from=2026-10-26&to=2026-10-26"), 200, "CoverageList")["items"]
    assert [(i["plan_id"], i["coverage"]) for i in cov] == [(P1, "on_list")]
    assert client.delete(f"{url}?expected_version=5").status_code == 204
    fail(client.get(url), 404, "trip_not_found")
    cov = ok(client.get("/api/v1/coverage?from=2026-10-26&to=2026-10-26"), 200, "CoverageList")["items"]
    assert cov[0]["coverage"] == "not_covered"
    plans = ok(client.get("/api/v1/plans?from=2026-10-26&to=2026-10-26"), 200, "PlanList")
    assert set(plans["items"][0]) == {"plan_id", "week_start", "week_end", "timezone", "revision",
                                       "snapshot_id", "generated_at", "seed"}  # summaries unchanged


def test_error_codes(client) -> None:
    trip = make_trip(client, [P1])
    url = f"{BASE}/{trip['trip_id']}"
    err = fail(client.post(BASE, json={"plan_ids": [P1]}), 409, "week_already_on_trip")
    assert {"field": "trip_id", "message": trip["trip_id"]} in err["details"]
    fail(client.post(BASE, json={"plan_ids": ["plan_nope"]}), 404, "plan_not_found")
    fail(client.post(f"{url}/weeks", json={"plan_id": "plan_nope", "expected_version": 1}), 404, "plan_not_found")
    fail(client.get(f"{BASE}/trip_missing"), 404, "trip_not_found")
    fail(client.post(f"{BASE}/trip_missing/done", json={"expected_version": 1}), 404, "trip_not_found")
    fail(client.delete(f"{BASE}/trip_missing?expected_version=1"), 404, "trip_not_found")
    err = fail(client.post(f"{url}/done", json={"expected_version": 9}), 409, "stale_version")
    assert err["details"] == [
        {"field": "expected_version", "message": "9"}, {"field": "actual_version", "message": "1"}
    ]
    fail(client.delete(f"{url}?expected_version=9"), 409, "stale_version")
    fail(client.post(f"{url}/reopen", json={"expected_version": 1}), 409, "trip_not_done")
    fail(client.delete(f"{url}/weeks/{P1}?expected_version=1"), 409, "last_week")
    fail(client.delete(f"{url}/weeks/{P2}?expected_version=1"), 404, "week_not_on_trip")
    ok(client.post(f"{url}/done", json={"expected_version": 1}), 200, "ShoppingTrip")
    fail(client.post(f"{url}/done", json={"expected_version": 2}), 409, "trip_not_done")
    fail(client.post(f"{url}/weeks", json={"plan_id": P2, "expected_version": 2}), 409, "trip_done")
    fail(client.delete(f"{url}/weeks/{P1}?expected_version=2"), 409, "trip_done")
    other = make_trip(client, [P2])
    fail(client.post(f"{BASE}/{other['trip_id']}/weeks", json={"plan_id": P1, "expected_version": 1}),
         409, "week_already_on_trip")


def test_invalid_requests(client) -> None:
    trip = make_trip(client, [P1])
    url = f"{BASE}/{trip['trip_id']}"
    bad_bodies = [
        {}, {"plan_ids": []}, {"plan_ids": [P1, P1]}, {"plan_ids": "x"}, {"plan_ids": ["bad id!"]},
        {"plan_ids": [P2], "extra": 1}, {"plan_ids": [P2], "name": 5}, {"plan_ids": [P2], "name": "x" * 81},
    ]
    for body in bad_bodies:
        fail(client.post(BASE, json=body), 400, "invalid_request")
    fail(client.post(BASE, data="not json"), 400, "invalid_request")
    for body in ({}, {"expected_version": 0}, {"expected_version": "1"}, {"expected_version": True},
                 {"expected_version": 1, "x": 1}):
        fail(client.post(f"{url}/done", json=body), 400, "invalid_request")
        fail(client.post(f"{url}/reopen", json=body), 400, "invalid_request")
    fail(client.post(f"{url}/weeks", json={"expected_version": 1}), 400, "invalid_request")
    for query in ("", "?expected_version=0", "?expected_version=x", "?expected_version=1&expected_version=1"):
        fail(client.delete(url + query), 400, "invalid_request")
        fail(client.delete(f"{url}/weeks/{P1}{query}"), 400, "invalid_request")
    fail(client.get(f"{BASE}/bad%20id"), 400, "invalid_request")
    for query in ("", "?from=2026-10-26", "?from=2026-10-27&to=2026-10-26", "?from=nope&to=2026-10-26"):
        fail(client.get("/api/v1/coverage" + query), 400, "invalid_request")
    assert ok(client.get(url), 200, "ShoppingTrip")["version"] == 1  # nothing changed


def test_done_freezes_items_when_plan_changes(client) -> None:
    trip = make_trip(client, [P1])
    url = f"{BASE}/{trip['trip_id']}"
    done = ok(client.post(f"{url}/done", json={"expected_version": 1}), 200, "ShoppingTrip")
    proposal = ok(client.post(f"/api/v1/plans/{P1}/proposals", json={"expected_revision": 1, "seed": 2}), 201, "Proposal")
    ok(client.post(f"/api/v1/plans/{P1}/proposals/{proposal['proposal_id']}/accept",
                   json={"expected_revision": 1}, headers={"Idempotency-Key": "k1"}), 200, "MutationResult")
    after = ok(client.get(url), 200, "ShoppingTrip")
    assert after["items"] == done["items"] and after["version"] == done["version"]
    assert after["weeks"][0]["changed_since_shopping"] is True and after["weeks"][0]["current_revision"] == 2
    cov = ok(client.get("/api/v1/coverage?from=2026-10-26&to=2026-10-26"), 200, "CoverageList")["items"][0]
    assert cov["coverage"] == "covered" and cov["changed_since_shopping"] is True


def test_mutations_forbidden_off_loopback(client) -> None:
    trip = make_trip(client, [P1])
    url = f"{BASE}/{trip['trip_id']}"
    for response in (
        client.post(BASE, json={"plan_ids": [P2]}, environ_base=LAN),
        client.post(f"{url}/weeks", json={"plan_id": P2, "expected_version": 1}, environ_base=LAN),
        client.delete(f"{url}/weeks/{P1}?expected_version=1", environ_base=LAN),
        client.post(f"{url}/done", json={"expected_version": 1}, environ_base=LAN),
        client.post(f"{url}/reopen", json={"expected_version": 1}, environ_base=LAN),
        client.delete(f"{url}?expected_version=1", environ_base=LAN),
    ):
        fail(response, 403, "forbidden")
    assert ok(client.get(url), 200, "ShoppingTrip") == trip


def test_json_mode_is_read_only() -> None:
    client = create_app(week_start="2026-10-19").test_client()
    assert ok(client.get(BASE), 200, "ShoppingTripList") == {"items": []}
    fail(client.get(f"{BASE}/trip_x"), 404, "trip_not_found")
    fail(client.post(BASE, json={"plan_ids": ["plan_2026_10_19"]}), 403, "read_only")
    fail(client.post(f"{BASE}/trip_x/done", json={"expected_version": 1}), 403, "read_only")
    fail(client.delete(f"{BASE}/trip_x?expected_version=1"), 403, "read_only")
    cov = ok(client.get("/api/v1/coverage?from=2026-10-19&to=2026-10-19"), 200, "CoverageList")
    assert [(i["plan_id"], i["coverage"], i["trip_id"]) for i in cov["items"]] == [
        ("plan_2026_10_19", "not_covered", None)
    ]
