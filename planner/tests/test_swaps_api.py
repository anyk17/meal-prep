"""HTTP tests for the phase 5 swap routes, the variants catalog, and gating."""

from __future__ import annotations

import pytest

from mealplanner.entrypoints.http.app import create_app

from auth_helpers import logged_in_client

from test_write_api import (  # noqa: F401  (fixtures + helpers)
    FUTURE_ID,
    KEY,
    app,
    check_schema,
    clock,
    db_path,
    fail,
    make_plan,
    ok,
    propose,
)

PREVIEW = f"/api/v1/plans/{FUTURE_ID}/swaps/preview"
SWAPS = f"/api/v1/plans/{FUTURE_ID}/swaps"


@pytest.fixture
def client(app):
    return logged_in_client(app)


def catalog(client) -> list[dict]:
    return ok(client.get("/api/v1/catalog/variants"), 200, "SwapVariantList")["items"]


def other_variant(client, slot: dict, *, same_protein: bool = False) -> str:
    for v in catalog(client):
        if v["recipe_id"] != slot["recipe_id"] and (v["protein"] == slot["protein"]) == same_protein:
            return v["variant_id"]
    raise AssertionError


def body(slot: dict, variant: str, scope: str = "pair", revision: int = 1) -> dict:
    return {"slot_id": slot["assignment_id"], "variant_id": variant, "scope": scope, "expected_revision": revision}


@pytest.fixture
def plan(client) -> dict:
    return make_plan(client)


def linked_lunch(plan: dict) -> dict:
    return next(s for s in plan["slots"] if s["linked_assignment_id"] and s["meal"] == "lunch")


def test_catalog_lists_sorted_variants_with_display_names(client) -> None:
    items = catalog(client)
    assert [(i["name"], i["variant_id"]) for i in items] == sorted((i["name"], i["variant_id"]) for i in items)
    assert all(set(i) == {"recipe_id", "variant_id", "name", "protein", "carb"} for i in items)
    assert any(i["carb"] is None for i in items) and any(i["carb"] for i in items)


def test_catalog_is_unavailable_in_json_mode() -> None:
    client = logged_in_client(create_app(week_start="2026-10-19"))
    error = fail(client.get("/api/v1/catalog/variants"), 503, "source_unavailable")
    assert "SQLite" in error["message"]


def test_preview_returns_changes_delta_warnings_and_stores_nothing(client, plan) -> None:
    lunch = linked_lunch(plan)
    variant = other_variant(client, lunch)
    preview = ok(client.post(PREVIEW, json=body(lunch, variant)), 200, "SwapPreview")
    assert preview["plan_id"] == FUTURE_ID and preview["base_revision"] == 1
    assert {c["assignment_id"] for c in preview["changes"]} == {lunch["assignment_id"], lunch["linked_assignment_id"]}
    assert all(c["kind"] == "changed" and c["after"]["variant_id"] == variant for c in preview["changes"])
    assert preview["grocery_delta"] and preview["trips"] == []
    assert all(r["difference"] != 0 for r in preview["grocery_delta"])
    revisions = ok(client.get(f"/api/v1/plans/{FUTURE_ID}/revisions"), 200, "RevisionList")["items"]
    assert [r["revision"] for r in revisions] == [1]
    assert ok(client.get(f"/api/v1/plans/{FUTURE_ID}"), 200, "Plan")["revision"] == 1


def test_swap_creates_one_revision_and_replays_idempotently(client, plan) -> None:
    lunch = linked_lunch(plan)
    variant = other_variant(client, lunch, same_protein=False)
    first = client.post(SWAPS, json=body(lunch, variant), headers=KEY)
    result = ok(first, 201, "SwapResult")
    assert first.headers["Location"] == f"/api/v1/plans/{FUTURE_ID}"
    swapped = result["plan"]
    assert swapped["revision"] == 2 and swapped["snapshot_id"] != plan["snapshot_id"]
    assert ok(client.get(f"/api/v1/plans/{FUTURE_ID}"), 200, "Plan") == swapped
    assert {w["rule"] for w in result["rule_warnings"]}
    replay = ok(client.post(SWAPS, json=body(lunch, variant), headers=KEY), 200, "SwapResult")
    assert replay == result
    revisions = ok(client.get(f"/api/v1/plans/{FUTURE_ID}/revisions"), 200, "RevisionList")["items"]
    assert [(r["revision"], r["source"]) for r in revisions] == [(1, "create"), (2, "swap")]
    other = {**body(lunch, variant), "scope": "single", "expected_revision": 1}
    fail(client.post(SWAPS, json=other, headers=KEY), 422, "retry_id_reused")


def test_swapping_to_the_same_variant_is_a_200_noop(client, plan) -> None:
    slot = next(s for s in plan["slots"] if s["linked_assignment_id"] is None)
    result = ok(client.post(SWAPS, json=body(slot, slot["variant_id"]), headers=KEY), 200, "SwapResult")
    assert result["plan"] == plan
    assert ok(client.get(f"/api/v1/plans/{FUTURE_ID}"), 200, "Plan")["revision"] == 1


def test_single_scope_unlinks_the_pair(client, plan) -> None:
    lunch = linked_lunch(plan)
    variant = other_variant(client, lunch)
    result = ok(client.post(SWAPS, json=body(lunch, variant, "single"), headers=KEY), 201, "SwapResult")
    slots = {s["assignment_id"]: s for s in result["plan"]["slots"]}
    assert slots[lunch["assignment_id"]]["linked_assignment_id"] is None
    assert slots[lunch["linked_assignment_id"]]["linked_assignment_id"] is None
    assert slots[lunch["linked_assignment_id"]]["variant_id"] != variant


def test_error_codes(client, plan, clock) -> None:
    lunch = linked_lunch(plan)
    variant = other_variant(client, lunch)
    for path in (PREVIEW, SWAPS):
        h = KEY if path == SWAPS else {}
        fail(client.post(path, json={**body(lunch, variant), "slot_id": "slot_nope"}, headers=h), 404, "slot_not_found")
        fail(client.post(path, json={**body(lunch, variant), "variant_id": "nope__carb_none"}, headers=h), 404, "variant_not_found")
        fail(client.post(path, json=body(lunch, variant, revision=9), headers=h), 409, "stale_revision")
        fail(client.post(path.replace(FUTURE_ID, "plan_2030_01_07"), json=body(lunch, variant), headers=h), 404, "plan_not_found")
    clock.now = clock.now.replace(month=11, day=2)  # the whole week is in the past
    for path in (PREVIEW, SWAPS):
        h = KEY if path == SWAPS else {}
        fail(client.post(path, json=body(lunch, variant), headers=h), 409, "slot_not_editable")
    revisions = ok(client.get(f"/api/v1/plans/{FUTURE_ID}/revisions"), 200, "RevisionList")["items"]
    assert len(revisions) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "both"},
        {"scope": None},
        {"expected_revision": 0},
        {"expected_revision": "1"},
        {"slot_id": ""},
        {"variant_id": 3},
        {"extra": 1},
    ],
)
def test_bad_bodies_are_invalid_requests(client, plan, change) -> None:
    lunch = linked_lunch(plan)
    good = body(lunch, other_variant(client, lunch))
    bad = {**good, **change}
    fail(client.post(PREVIEW, json=bad), 400, "invalid_request")
    fail(client.post(SWAPS, json=bad, headers=KEY), 400, "invalid_request")
    for missing in good:
        partial = {k: v for k, v in good.items() if k != missing}
        fail(client.post(PREVIEW, json=partial), 400, "invalid_request")


def test_swap_requires_a_single_idempotency_key(client, plan) -> None:
    lunch = linked_lunch(plan)
    good = body(lunch, other_variant(client, lunch))
    fail(client.post(SWAPS, json=good), 400, "invalid_request")
    fail(client.post(SWAPS, json=good, headers={"Idempotency-Key": "bad key"}), 400, "invalid_request")
    fail(client.post(PREVIEW, data="x", content_type="text/plain"), 400, "invalid_request")


def test_swap_routes_are_gated_like_other_writes(app, client, plan, tmp_path) -> None:
    lunch = linked_lunch(plan)
    good = body(lunch, other_variant(client, lunch))
    anonymous = app.test_client()
    fail(anonymous.post(PREVIEW, json=good), 401, "unauthenticated")  # preview is a POST: login required too
    fail(anonymous.post(SWAPS, json=good, headers=KEY), 401, "unauthenticated")
    assert anonymous.get("/api/v1/catalog/variants").status_code == 200
    json_client = logged_in_client(create_app(week_start="2026-10-19"))
    fail(json_client.post(PREVIEW, json=good), 403, "read_only")
    fail(json_client.post(SWAPS, json=good, headers=KEY), 403, "read_only")
    assert ok(client.get(f"/api/v1/plans/{FUTURE_ID}"), 200, "Plan")["revision"] == 1


def test_preview_reports_done_trip_extra_needed(client, plan) -> None:
    lunch = linked_lunch(plan)
    variant = other_variant(client, lunch)
    trip = ok(client.post("/api/v1/shopping-trips", json={"plan_ids": [FUTURE_ID]}), 201, "ShoppingTrip")
    (open_trip,) = ok(client.post(PREVIEW, json=body(lunch, variant)), 200, "SwapPreview")["trips"]
    assert open_trip["status"] == "open" and open_trip["extra_needed"] == []
    ok(client.post(f"/api/v1/shopping-trips/{trip['trip_id']}/done", json={"expected_version": 1}), 200, "ShoppingTrip")
    (done,) = ok(client.post(PREVIEW, json=body(lunch, variant)), 200, "SwapPreview")["trips"]
    assert done["status"] == "done" and done["trip_id"] == trip["trip_id"] and done["extra_needed"]
    assert all(r["quantity"] > 0 for r in done["extra_needed"])


def test_proposal_reports_replaced_swaps_and_undo_of_a_swap_is_allowed(client, plan) -> None:
    assert propose(client)["replaced_swaps"] == 0
    lunch = linked_lunch(plan)
    variant = other_variant(client, lunch)
    ok(client.post(SWAPS, json=body(lunch, variant), headers=KEY), 201, "SwapResult")
    proposal = propose(client, revision=2)
    changed = {c["assignment_id"] for c in proposal["changes"]}
    swapped = {lunch["assignment_id"], lunch["linked_assignment_id"]}
    assert proposal["replaced_swaps"] == len(changed & swapped) >= 1
    undo = ok(client.post(f"/api/v1/plans/{FUTURE_ID}/undo", json={"expected_revision": 2}, headers={"Idempotency-Key": "u1"}), 200, "MutationResult")
    assert undo["revision"] == 3 and undo["changed"] is True
    assert ok(client.get(f"/api/v1/plans/{FUTURE_ID}"), 200, "Plan")["slots"] == plan["slots"]


def test_preview_lists_each_trip_with_its_groups_and_limits_extra_needed(client, plan) -> None:
    lunch = linked_lunch(plan)
    variant = other_variant(client, lunch)
    ll = ok(client.post("/api/v1/shopping-trips", json={"plan_ids": [FUTURE_ID], "groups": ["long_lasting"]}), 201, "ShoppingTrip")
    fresh = ok(client.post("/api/v1/shopping-trips", json={"plan_ids": [FUTURE_ID], "groups": ["fresh"]}), 201, "ShoppingTrip")
    for t in (ll, fresh):
        ok(client.post(f"/api/v1/shopping-trips/{t['trip_id']}/done", json={"expected_version": 1}), 200, "ShoppingTrip")
    trips = ok(client.post(PREVIEW, json=body(lunch, variant)), 200, "SwapPreview")["trips"]
    assert {t["trip_id"]: t["groups"] for t in trips} == {ll["trip_id"]: ["long_lasting"], fresh["trip_id"]: ["fresh"]}
    extra = {t["trip_id"]: {r["ingredient_id"] for r in t["extra_needed"]} for t in trips}
    labels = {i["ingredient_id"]: i["shelf_life"] for t in (ll, fresh) for i in t["items"]}
    assert all(labels.get(i, "long_lasting") == "long_lasting" for i in extra[ll["trip_id"]])
    assert all(labels.get(i, "fresh") == "fresh" for i in extra[fresh["trip_id"]])
