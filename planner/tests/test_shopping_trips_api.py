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

from auth_helpers import logged_in_client

ROOT = Path(__file__).resolve().parents[2]
SPEC = yaml.safe_load((ROOT / "docs/migration-to-cloud/openapi.yaml").read_text())
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
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
    client = logged_in_client(app)
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
    for group in ("long_lasting", "fresh"):
        assert [(i["plan_id"], i["groups"][group]["coverage"], i["groups"][group]["trip_id"]) for i in cov] == [
            (P1, "bought", trip["trip_id"]), (P2, "not_covered", None), (P3, "not_covered", None)
        ]
    trip = ok(client.post(f"{url}/reopen", json={"expected_version": 4}), 200, "ShoppingTrip")
    assert trip["status"] == "open" and trip["version"] == 5
    cov = ok(client.get("/api/v1/coverage?from=2026-10-26&to=2026-10-26"), 200, "CoverageList")["items"]
    assert [(i["plan_id"], i["groups"]["fresh"]["coverage"]) for i in cov] == [(P1, "to_buy")]
    assert client.delete(f"{url}?expected_version=5").status_code == 204
    fail(client.get(url), 404, "trip_not_found")
    cov = ok(client.get("/api/v1/coverage?from=2026-10-26&to=2026-10-26"), 200, "CoverageList")["items"]
    assert {g["coverage"] for g in cov[0]["groups"].values()} == {"not_covered"}
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
    fail(client.post(f"{url}/reopen", json={"expected_version": 1}), 409, "invalid_transition")
    fail(client.delete(f"{url}/weeks/{P1}?expected_version=1"), 409, "last_week")
    fail(client.delete(f"{url}/weeks/{P2}?expected_version=1"), 404, "week_not_on_trip")
    ok(client.post(f"{url}/done", json={"expected_version": 1}), 200, "ShoppingTrip")
    fail(client.post(f"{url}/done", json={"expected_version": 2}), 409, "invalid_transition")
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
    assert after["items"] == [] and after["bought_items"] == done["bought_items"]
    assert [c["changed_since_shopping"] for c in after["weeks"][0]["cells"]] == [True, True]
    assert after["weeks"][0]["current_revision"] == 2
    cov = ok(client.get("/api/v1/coverage?from=2026-10-26&to=2026-10-26"), 200, "CoverageList")["items"][0]
    assert {g: (v["coverage"], v["changed_since_shopping"]) for g, v in cov["groups"].items()} == {
        "long_lasting": ("bought", True), "fresh": ("bought", True)
    }


def test_mutations_need_login(db_path, client) -> None:
    anonymous = create_app(db_path=db_path, clock=lambda: NOW).test_client()
    trip = make_trip(client, [P1])
    url = f"{BASE}/{trip['trip_id']}"
    for response in (
        anonymous.post(BASE, json={"plan_ids": [P2]}),
        anonymous.post(f"{url}/weeks", json={"plan_id": P2, "expected_version": 1}),
        anonymous.delete(f"{url}/weeks/{P1}?expected_version=1"),
        anonymous.post(f"{url}/done", json={"expected_version": 1}),
        anonymous.post(f"{url}/reopen", json={"expected_version": 1}),
        anonymous.post(f"{url}/purchases", json={"expected_version": 1, "cells": [{"plan_id": P1, "group": "fresh"}]}),
        anonymous.post(f"{url}/purchases/undo", json={"expected_version": 1, "cells": [{"plan_id": P1, "group": "fresh"}]}),
        anonymous.delete(f"{url}?expected_version=1"),
    ):
        fail(response, 401, "unauthenticated")
    assert ok(anonymous.get(url), 200, "ShoppingTrip") == trip


def test_json_mode_is_read_only() -> None:
    client = logged_in_client(create_app(week_start="2026-10-19"))
    assert ok(client.get(BASE), 200, "ShoppingTripList") == {"items": []}
    fail(client.get(f"{BASE}/trip_x"), 404, "trip_not_found")
    fail(client.post(BASE, json={"plan_ids": ["plan_2026_10_19"]}), 403, "read_only")
    fail(client.post(f"{BASE}/trip_x/done", json={"expected_version": 1}), 403, "read_only")
    fail(client.post(f"{BASE}/trip_x/purchases", json={"expected_version": 1, "cells": []}), 403, "read_only")
    fail(client.delete(f"{BASE}/trip_x?expected_version=1"), 403, "read_only")
    cov = ok(client.get("/api/v1/coverage?from=2026-10-19&to=2026-10-19"), 200, "CoverageList")
    assert [
        (i["plan_id"], g["coverage"], g["trip_id"]) for i in cov["items"] for g in i["groups"].values()
    ] == [("plan_2026_10_19", "not_covered", None)] * 2


# ---------------------------------------------------------------- shelf-life groups (phase 5b)


def coverage_of(client, plan_id: str, week_start: str) -> dict:
    items = ok(client.get(f"/api/v1/coverage?from={week_start}&to={week_start}"), 200, "CoverageList")["items"]
    return next(i for i in items if i["plan_id"] == plan_id)["groups"]


def test_create_with_groups_items_and_coverage(client) -> None:
    trip = make_trip(client, [P1], groups=["long_lasting"])
    assert [w["groups"] for w in trip["weeks"]] == [["long_lasting"]]
    assert trip["items"] and {i["shelf_life"] for i in trip["items"]} == {"long_lasting"}
    groups = coverage_of(client, P1, "2026-10-26")
    assert groups["long_lasting"] == {"coverage": "to_buy", "trip_id": trip["trip_id"], "changed_since_shopping": False}
    assert groups["fresh"] == {"coverage": "not_covered", "trip_id": None, "changed_since_shopping": False}
    # default is both groups on a new week
    both = make_trip(client, [P2])
    assert [w["groups"] for w in both["weeks"]] == [["long_lasting", "fresh"]]
    assert {i["shelf_life"] for i in both["items"]} == {"long_lasting", "fresh"}


def test_long_lasting_covers_week_two_while_fresh_does_not(client) -> None:
    trip = make_trip(client, [P1])
    url = f"{BASE}/{trip['trip_id']}"
    trip = ok(client.post(f"{url}/weeks", json={"plan_id": P2, "groups": ["long_lasting"], "expected_version": 1}),
              200, "ShoppingTrip")
    assert [(w["plan_id"], w["groups"]) for w in trip["weeks"]] == [
        (P1, ["long_lasting", "fresh"]), (P2, ["long_lasting"])
    ]
    week2 = coverage_of(client, P2, "2026-11-02")
    assert week2["long_lasting"]["coverage"] == "to_buy" and week2["fresh"]["coverage"] == "not_covered"
    # the second list covers fresh for week two; the first week still belongs to the first list
    second = make_trip(client, [P2], groups=["fresh"])
    week2 = coverage_of(client, P2, "2026-11-02")
    assert (week2["long_lasting"]["trip_id"], week2["fresh"]["trip_id"]) == (trip["trip_id"], second["trip_id"])
    # extending a week already on the list with a group held elsewhere conflicts and names the group
    err = fail(client.post(f"{url}/weeks", json={"plan_id": P2, "groups": ["fresh"], "expected_version": 2}),
               409, "week_already_on_trip")
    assert {"field": "group", "message": "fresh"} in err["details"]


def test_add_week_groups_extend_and_validate(client) -> None:
    trip = make_trip(client, [P1], groups=["fresh"])
    url = f"{BASE}/{trip['trip_id']}"
    extended = ok(client.post(f"{url}/weeks", json={"plan_id": P1, "groups": ["long_lasting"], "expected_version": 1}),
                  200, "ShoppingTrip")
    assert extended["weeks"][0]["groups"] == ["long_lasting", "fresh"]
    for bad in ([], ["frozen"], ["fresh", "fresh"], "fresh"):
        fail(client.post(f"{url}/weeks", json={"plan_id": P2, "groups": bad, "expected_version": 2}),
             400, "invalid_request")
        fail(client.post(BASE, json={"plan_ids": [P2], "groups": bad}), 400, "invalid_request")


def test_delete_week_with_group_query(client) -> None:
    trip = make_trip(client, [P1, P2])
    url = f"{BASE}/{trip['trip_id']}"
    trip = ok(client.delete(f"{url}/weeks/{P1}?expected_version=1&group=fresh"), 200, "ShoppingTrip")
    assert [(w["plan_id"], w["groups"]) for w in trip["weeks"]] == [
        (P1, ["long_lasting"]), (P2, ["long_lasting", "fresh"])
    ]
    assert coverage_of(client, P1, "2026-10-26")["fresh"]["coverage"] == "not_covered"
    fail(client.delete(f"{url}/weeks/{P1}?expected_version=2&group=fresh"), 404, "week_not_on_trip")
    fail(client.delete(f"{url}/weeks/{P1}?expected_version=2&group=frozen"), 400, "invalid_request")
    trip = ok(client.delete(f"{url}/weeks/{P1}?expected_version=2&group=long_lasting"), 200, "ShoppingTrip")
    assert [w["plan_id"] for w in trip["weeks"]] == [P2]  # entry disappears with its last group
    fail(client.delete(f"{url}/weeks/{P2}?expected_version=3"), 409, "last_week")
    trip = ok(client.delete(f"{url}/weeks/{P2}?expected_version=3&group=fresh"), 200, "ShoppingTrip")
    fail(client.delete(f"{url}/weeks/{P2}?expected_version=4&group=long_lasting"), 409, "last_week")
    assert trip["weeks"][0]["groups"] == ["long_lasting"]


# ---------------------------------------------------------------- purchases (phase 5c)


def cells(*pairs: tuple[str, str]) -> list[dict]:
    return [{"plan_id": plan_id, "group": group} for plan_id, group in pairs]


def test_mark_and_undo_purchases(client) -> None:
    trip = make_trip(client)
    url = f"{BASE}/{trip['trip_id']}"
    marked = ok(
        client.post(f"{url}/purchases", json={"expected_version": 1, "cells": cells((P1, "long_lasting"), (P2, "long_lasting"))}),
        200, "ShoppingTrip",
    )
    assert marked["version"] == 2 and marked["status"] == "open" and marked["completed_at"] is None
    assert [[(c["group"], c["state"]) for c in w["cells"]] for w in marked["weeks"]] == [
        [("long_lasting", "bought"), ("fresh", "to_buy")]
    ] * 2
    assert {i["shelf_life"] for i in marked["items"]} == {"fresh"}
    assert {i["shelf_life"] for i in marked["bought_items"]} == {"long_lasting"}
    for item in marked["items"] + marked["bought_items"]:
        assert round(sum(item["quantity_by_plan"].values()), 2) == item["quantity"]
    groups = coverage_of(client, P1, "2026-10-26")
    assert (groups["long_lasting"]["coverage"], groups["fresh"]["coverage"]) == ("bought", "to_buy")
    # a plan edit flags only the bought cell and leaves bought_items frozen
    proposal = ok(client.post(f"/api/v1/plans/{P1}/proposals", json={"expected_revision": 1, "seed": 2}), 201, "Proposal")
    ok(client.post(f"/api/v1/plans/{P1}/proposals/{proposal['proposal_id']}/accept",
                   json={"expected_revision": 1}, headers={"Idempotency-Key": "k5c"}), 200, "MutationResult")
    after = ok(client.get(url), 200, "ShoppingTrip")
    assert after["bought_items"] == marked["bought_items"]
    assert [c["changed_since_shopping"] for c in after["weeks"][0]["cells"]] == [True, False]
    assert [c["changed_since_shopping"] for c in after["weeks"][1]["cells"]] == [False, False]
    assert coverage_of(client, P1, "2026-10-26")["long_lasting"]["changed_since_shopping"] is True
    undone = ok(
        client.post(f"{url}/purchases/undo", json={"expected_version": 2, "cells": cells((P1, "long_lasting"))}),
        200, "ShoppingTrip",
    )
    assert undone["version"] == 3
    assert [(c["state"], c["revision"], c["bought_at"]) for c in undone["weeks"][0]["cells"]] == [("to_buy", None, None)] * 2


def test_purchases_make_the_list_done_and_adding_a_week_reopens_it(client) -> None:
    trip = make_trip(client, [P1])
    url = f"{BASE}/{trip['trip_id']}"
    both = cells((P1, "long_lasting"), (P1, "fresh"))
    done = ok(client.post(f"{url}/purchases", json={"expected_version": 1, "cells": both}), 200, "ShoppingTrip")
    assert done["status"] == "done" and done["completed_at"] and done["items"] == []
    added = ok(client.post(f"{url}/weeks", json={"plan_id": P2, "expected_version": 2}), 200, "ShoppingTrip")
    assert added["status"] == "open" and added["completed_at"] is None and added["items"]
    removed = ok(client.delete(f"{url}/weeks/{P2}?expected_version=3"), 200, "ShoppingTrip")
    assert removed["status"] == "done"  # weeks can be edited in any state
    reopened = ok(client.post(f"{url}/reopen", json={"expected_version": 4}), 200, "ShoppingTrip")
    assert reopened["status"] == "open" and reopened["bought_items"] == []


def test_purchase_errors(client) -> None:
    trip = make_trip(client, [P1], groups=["long_lasting"])
    url = f"{BASE}/{trip['trip_id']}"
    body = lambda *pairs, v=1: {"expected_version": v, "cells": cells(*pairs)}  # noqa: E731
    fail(client.post(f"{url}/purchases", json=body((P1, "fresh"))), 404, "week_not_on_trip")  # group not on the trip
    fail(client.post(f"{url}/purchases", json=body((P2, "long_lasting"))), 404, "week_not_on_trip")
    fail(client.post(f"{url}/purchases/undo", json=body((P2, "fresh"))), 404, "week_not_on_trip")
    fail(client.post(f"{BASE}/trip_missing/purchases", json=body((P1, "long_lasting"))), 404, "trip_not_found")
    fail(client.post(f"{url}/purchases/undo", json=body((P1, "long_lasting"))), 409, "invalid_transition")  # not bought
    err = fail(client.post(f"{url}/purchases", json=body((P1, "long_lasting"), v=9)), 409, "stale_version")
    assert {"field": "actual_version", "message": "1"} in err["details"]
    ok(client.post(f"{url}/purchases", json=body((P1, "long_lasting"))), 200, "ShoppingTrip")
    fail(client.post(f"{url}/purchases", json=body((P1, "long_lasting"), v=2)), 409, "invalid_transition")  # already bought
    fail(client.post(f"{url}/purchases/undo", json=body((P1, "long_lasting"), v=9)), 409, "stale_version")
    assert ok(client.get(url), 200, "ShoppingTrip")["version"] == 2  # failures changed nothing
    for bad in (
        {}, {"expected_version": 2}, {"cells": cells((P1, "long_lasting"))},
        {"expected_version": 2, "cells": []}, {"expected_version": 2, "cells": "x"},
        {"expected_version": 2, "cells": cells((P1, "long_lasting"), (P1, "long_lasting"))},
        {"expected_version": 2, "cells": cells((P1, "frozen"))},
        {"expected_version": 2, "cells": [{"plan_id": P1}]},
        {"expected_version": 2, "cells": [{"plan_id": P1, "group": "fresh", "x": 1}]},
        {"expected_version": 2, "cells": [{"plan_id": "bad id!", "group": "fresh"}]},
        {"expected_version": 2, "cells": cells((P1, "fresh")), "extra": 1},
    ):
        fail(client.post(f"{url}/purchases", json=bad), 400, "invalid_request")
        fail(client.post(f"{url}/purchases/undo", json=bad), 400, "invalid_request")
