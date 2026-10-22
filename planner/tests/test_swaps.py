"""Phase 5 meal swaps: domain/service tests on memory + SQLite, and HTTP tests."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest

from mealplanner.adapters.memory import InMemoryShoppingTripRepository
from mealplanner.adapters.sqlite.store import SqliteShoppingTripRepository
from mealplanner.application.errors import InvalidRequest
from mealplanner.application.plan_workflows import PlanWorkflowService
from mealplanner.application.shopping_trips import ShoppingTripService
from mealplanner.domain.snapshot import validate_snapshot
from mealplanner.domain.swap import (
    SlotNotEditable,
    SlotNotFound,
    VariantNotFound,
    list_swap_variants,
)
from mealplanner.domain.weekly_plan import RetryIdReused, StaleRevision

from test_plan_workflows import FUTURE, INPUTS, Calls, Clock, Reader, make_repo, proposal_count

KEY = {"Idempotency-Key": "key-1"}


class SwapEnv:
    def __init__(self, kind: str, tmp_path: Path) -> None:
        self.repo, self.read_adapter = make_repo(kind, tmp_path)
        self.trip_repo = (
            InMemoryShoppingTripRepository()
            if kind == "memory"
            else SqliteShoppingTripRepository(tmp_path / "plans.db")
        )
        self.clock = Clock()
        self.solver = Calls()
        self.reader = Reader()
        self.svc = PlanWorkflowService(
            self.repo,
            self.reader,
            self.clock,
            generator=self.solver,
            proposal_ttl=timedelta(minutes=10),
            trips=self.trip_repo,
        )
        self.trips = ShoppingTripService(self.trip_repo, self.repo, self.clock, INPUTS.shelf_lives)
        self.plan = self._plan_with_pair()

    def _plan_with_pair(self):
        plan = self.svc.create_plan(FUTURE, seed=1)
        links = [s["linked_assignment_id"] for s in plan.snapshot.plan["slots"]]
        assert any(links) and not all(links), "seed 1 needs a linked pair and an unlinked slot"
        return plan

    @property
    def plan_id(self) -> str:
        return self.plan.plan_id

    def current(self):
        return self.svc.get_current(self.plan_id)

    def slots(self):
        return {s["assignment_id"]: s for s in self.current().snapshot.plan["slots"]}

    def linked_lunch(self) -> dict:
        return next(
            s for s in self.slots().values() if s["linked_assignment_id"] and s["meal"] == "lunch"
        )

    def unlinked(self) -> dict:
        return next(s for s in self.slots().values() if s["linked_assignment_id"] is None)

    def variant_for(self, slot: dict, *, protein_differs: bool = False, carb: bool = False) -> str:
        for v in list_swap_variants(INPUTS):
            if v.recipe_id == slot["recipe_id"]:
                continue
            if protein_differs and v.protein == slot["protein"]:
                continue
            if (v.carb is not None) != carb:
                continue
            return v.variant_id
        raise AssertionError("no variant")

    def swap(self, slot: dict, variant_id: str, scope: str = "pair", key: str = "k1", rev=None):
        rev = rev or self.current().revision
        return self.svc.swap(self.plan_id, slot["assignment_id"], variant_id, scope, rev, key)


@pytest.fixture(params=["memory", "sqlite"])
def env(request, tmp_path) -> SwapEnv:
    return SwapEnv(request.param, tmp_path)


def unchanged(before: dict, after: dict, ids: set[str]) -> bool:
    return all(before[i] == after[i] for i in before if i not in ids)


# ---------------------------------------------------------------- applying


def test_pair_swap_changes_both_linked_slots_and_keeps_the_link(env: SwapEnv) -> None:
    lunch = env.linked_lunch()
    before = env.slots()
    variant = env.variant_for(lunch, carb=False)
    out = env.swap(lunch, variant, "pair")
    assert out.result.changed and out.result.revision == 2 and not out.result.replayed
    rev = env.current()
    assert rev.revision == 2 and rev.source == "swap"
    validate_snapshot(rev.snapshot.plan, rev.snapshot.shopping_list, set(rev.pantry))
    after = env.slots()
    dinner_id = lunch["linked_assignment_id"]
    target = INPUTS.recipes[variant.split("__")[0]]
    for aid in (lunch["assignment_id"], dinner_id):
        slot = after[aid]
        assert slot["variant_id"] == variant and slot["recipe_name"] == target.name
        assert slot["recipe_id"] == target.id and slot["carb"] is None
        assert (slot["date"], slot["meal"]) == (before[aid]["date"], before[aid]["meal"])
        assert slot["ingredients"] != before[aid]["ingredients"]
    assert after[lunch["assignment_id"]]["linked_assignment_id"] == dinner_id
    assert after[dinner_id]["linked_assignment_id"] == lunch["assignment_id"]
    assert after[dinner_id]["extended_from_lunch"] is True
    assert unchanged(before, after, {lunch["assignment_id"], dinner_id})
    assert rev.snapshot.plan["snapshot_id"] != env.plan.snapshot.plan["snapshot_id"]
    assert out.snapshot.plan == rev.snapshot.plan


def test_single_swap_of_a_linked_slot_unlinks_the_pair(env: SwapEnv) -> None:
    lunch = env.linked_lunch()
    before = env.slots()
    dinner_id = lunch["linked_assignment_id"]
    variant = env.variant_for(lunch)
    env.swap(lunch, variant, "single")
    after = env.slots()
    assert after[lunch["assignment_id"]]["variant_id"] == variant
    assert after[dinner_id]["variant_id"] == before[dinner_id]["variant_id"]  # partner untouched
    for aid in (lunch["assignment_id"], dinner_id):
        assert after[aid]["linked_assignment_id"] is None
    assert after[dinner_id]["extended_from_lunch"] is False
    assert unchanged(before, after, {lunch["assignment_id"], dinner_id})
    rev = env.current()
    validate_snapshot(rev.snapshot.plan, rev.snapshot.shopping_list, set(rev.pantry))


def test_single_swap_of_the_linked_dinner_unlinks_too(env: SwapEnv) -> None:
    lunch = env.linked_lunch()
    dinner = env.slots()[lunch["linked_assignment_id"]]
    env.swap(dinner, env.variant_for(dinner), "single")
    after = env.slots()
    assert after[dinner["assignment_id"]]["extended_from_lunch"] is False
    assert after[lunch["assignment_id"]]["linked_assignment_id"] is None
    assert after[lunch["assignment_id"]]["variant_id"] == lunch["variant_id"]


def test_pair_scope_on_an_unlinked_slot_is_a_single_swap(env: SwapEnv) -> None:
    slot = env.unlinked()
    before = env.slots()
    variant = env.variant_for(slot)
    out = env.swap(slot, variant, "pair")
    after = env.slots()
    assert after[slot["assignment_id"]]["variant_id"] == variant
    assert unchanged(before, after, {slot["assignment_id"]})
    assert out.result.revision == 2


def test_rule_violating_swap_is_allowed_with_warnings(env: SwapEnv) -> None:
    slot = env.unlinked()
    variant = env.variant_for(slot, protein_differs=True)  # breaks the exact weekly protein counts
    out = env.swap(slot, variant, "single")
    assert out.result.changed and env.current().revision == 2
    rules = {w["rule"] for w in out.rule_warnings}
    assert "weekly_protein_count" in rules
    assert all(set(w) == {"rule", "message"} for w in out.rule_warnings)


def test_swap_uses_the_base_revisions_captured_pantry(env: SwapEnv) -> None:
    base = env.current()
    bought = next(
        row["ingredient_id"]
        for rows in base.snapshot.shopping_list["sections"].values()
        for row in rows
        if row["ingredient_id"] not in INPUTS.pantry
    )
    env.reader.inputs = dataclasses.replace(INPUTS, pantry=(*INPUTS.pantry, bought))
    slot = env.unlinked()
    env.swap(slot, env.variant_for(slot), "single")
    rev = env.current()
    assert rev.pantry == base.pantry
    ids = {r["ingredient_id"] for rows in rev.snapshot.shopping_list["sections"].values() for r in rows}
    assert bought in ids or not any(  # still bought unless no remaining slot uses it
        r["ingredient_id"] == bought for s in rev.snapshot.plan["slots"] for r in s["ingredients"]
    )


def test_same_variant_is_a_noop_without_a_new_revision(env: SwapEnv) -> None:
    slot = env.unlinked()
    out = env.swap(slot, slot["variant_id"], "single")
    assert not out.result.changed and out.result.revision == 1
    assert env.current().revision == 1 and len(env.repo.list_revisions(env.plan_id)) == 1
    assert out.snapshot.plan == env.plan.snapshot.plan
    again = env.swap(slot, slot["variant_id"], "single")  # same key: replay of the no-op
    assert again.result.replayed and again.result.revision == 1


def test_swap_to_a_linked_slots_own_variant_as_single_still_unlinks(env: SwapEnv) -> None:
    lunch = env.linked_lunch()
    out = env.swap(lunch, lunch["variant_id"], "single")
    assert out.result.changed
    assert env.slots()[lunch["assignment_id"]]["linked_assignment_id"] is None


# ---------------------------------------------------------------- errors


def test_unknown_slot_variant_scope_and_past_slot(env: SwapEnv) -> None:
    slot = env.unlinked()
    with pytest.raises(SlotNotFound):
        env.svc.swap(env.plan_id, "slot_nope", slot["variant_id"], "pair", 1, "k")
    with pytest.raises(VariantNotFound):
        env.svc.swap(env.plan_id, slot["assignment_id"], "nope__carb_none", "pair", 1, "k")
    with pytest.raises(InvalidRequest):
        env.svc.swap(env.plan_id, slot["assignment_id"], slot["variant_id"], "both", 1, "k")
    assert env.current().revision == 1


def test_slots_before_today_are_fixed_but_today_and_later_in_a_started_week_are_not(
    env: SwapEnv,
) -> None:
    slots = sorted(env.slots().values(), key=lambda s: (s["date"], s["meal"]))
    env.clock.now = env.clock.now.replace(year=2026, month=10, day=28)  # Wednesday of that week
    assert env.clock().astimezone(env.svc._tz).date().isoformat() == "2026-10-28"
    monday = next(s for s in slots if s["date"] == "2026-10-26")
    with pytest.raises(SlotNotEditable):
        env.svc.preview_swap(env.plan_id, monday["assignment_id"], env.variant_for(monday), "single", 1)
    with pytest.raises(SlotNotEditable):
        env.swap(monday, env.variant_for(monday), "single")
    assert env.current().revision == 1
    later = [s for s in slots if s["date"] >= "2026-10-29"]
    assert later
    assert env.swap(later[-1], env.variant_for(later[-1]), "pair").result.revision == 2
    wednesday = [s for s in slots if s["date"] == "2026-10-28"]
    assert wednesday  # a slot dated today is still editable
    out = env.swap(wednesday[0], env.variant_for(wednesday[0], protein_differs=True), "single", key="k2")
    assert out.result.revision == 3


def test_stale_revision(env: SwapEnv) -> None:
    slot = env.unlinked()
    env.swap(slot, env.variant_for(slot), "single")
    with pytest.raises(StaleRevision):
        env.swap(slot, env.variant_for(slot, protein_differs=True), "single", key="k2", rev=1)
    with pytest.raises(StaleRevision):
        env.svc.preview_swap(env.plan_id, slot["assignment_id"], slot["variant_id"], "single", 1)


def test_idempotent_retry_and_key_reuse(env: SwapEnv) -> None:
    slot = env.unlinked()
    variant = env.variant_for(slot)
    first = env.swap(slot, variant, "single", key="same")
    again = env.swap(slot, variant, "single", key="same", rev=1)
    assert again.result.replayed and again.result.revision == first.result.revision == 2
    assert again.snapshot.plan == first.snapshot.plan
    assert len(env.repo.list_revisions(env.plan_id)) == 2
    with pytest.raises(RetryIdReused):
        env.swap(env.linked_lunch(), variant, "single", key="same", rev=1)  # another slot
    with pytest.raises(RetryIdReused):  # even a different scope is a different request
        env.swap(slot, variant, "pair", key="same", rev=1)


# ---------------------------------------------------------------- preview, groceries, trips


def totals(shopping: dict) -> dict:
    return {
        (r["ingredient_id"], r["unit"]): r["quantity"]
        for rows in shopping["sections"].values()
        for r in rows
    }


def test_grocery_delta_matches_the_before_and_after_lists(env: SwapEnv) -> None:
    lunch = env.linked_lunch()
    variant = env.variant_for(lunch, protein_differs=True)
    base = env.current().snapshot.shopping_list
    preview = env.svc.preview_swap(env.plan_id, lunch["assignment_id"], variant, "pair", 1)
    env.swap(lunch, variant, "pair")
    new = env.current().snapshot.shopping_list
    before, after = totals(base), totals(new)
    expected = {
        k: (before.get(k, 0), after.get(k, 0))
        for k in before.keys() | after.keys()
        if round(after.get(k, 0) - before.get(k, 0), 2) != 0
    }
    rows = {(r["ingredient_id"], r["unit"]): r for r in preview.grocery_delta}
    assert set(rows) == set(expected) and rows
    for key, (b, a) in expected.items():
        assert (rows[key]["before"], rows[key]["after"]) == (b, a)
        assert rows[key]["difference"] == round(a - b, 2)
    assert any(r["before"] == 0 for r in rows.values()) or any(r["after"] == 0 for r in rows.values())
    assert [r["section"] for r in preview.grocery_delta] == sorted(r["section"] for r in preview.grocery_delta)


def test_preview_stores_nothing(env: SwapEnv) -> None:
    slot = env.linked_lunch()
    variant = env.variant_for(slot, protein_differs=True)
    first = env.svc.preview_swap(env.plan_id, slot["assignment_id"], variant, "pair", 1)
    second = env.svc.preview_swap(env.plan_id, slot["assignment_id"], variant, "pair", 1)
    assert first == second
    assert first.base_revision == 1 and len(first.changes) == 2
    assert {c.kind for c in first.changes} == {"changed"}
    assert first.rule_warnings and first.trips == ()
    assert env.current().revision == 1 and len(env.repo.list_revisions(env.plan_id)) == 1
    assert proposal_count(env.repo) == 0
    assert env.repo.get_retry(env.plan_id, "k1") is None


def test_preview_trip_impact_open_done_and_none(env: SwapEnv) -> None:
    slot = env.linked_lunch()
    variant = env.variant_for(slot, protein_differs=True)
    args = (env.plan_id, slot["assignment_id"], variant, "pair", 1)
    assert env.svc.preview_swap(*args).trips == ()
    trip = env.trips.create([env.plan_id], "Weekly")
    (impact,) = env.svc.preview_swap(*args).trips
    assert impact == {
        "trip_id": trip["trip_id"], "name": "Weekly", "status": "open",
        "groups": [{"group": "long_lasting", "state": "to_buy"}, {"group": "fresh", "state": "to_buy"}],
        "extra_needed": [],
    }
    env.trips.mark_done(trip["trip_id"], trip["version"])
    (done,) = env.svc.preview_swap(*args).trips
    assert done["status"] == "done" and done["trip_id"] == trip["trip_id"]
    assert done["groups"] == [{"group": "long_lasting", "state": "bought"}, {"group": "fresh", "state": "bought"}]
    base = totals(env.current().snapshot.shopping_list)
    env.swap(slot, variant, "pair")
    new = totals(env.current().snapshot.shopping_list)
    want = {k: round(v - base.get(k, 0), 2) for k, v in new.items() if round(v - base.get(k, 0), 2) > 0}
    got = {(r["ingredient_id"], r["unit"]): r["quantity"] for r in done["extra_needed"]}
    assert got == want and want
    assert all(set(r) == {"ingredient_id", "display", "unit", "quantity"} for r in done["extra_needed"])


def test_done_trip_keeps_items_and_flags_the_week_after_a_swap(env: SwapEnv) -> None:
    trip = env.trips.create([env.plan_id])
    done = env.trips.mark_done(trip["trip_id"], trip["version"])
    slot = env.unlinked()
    env.swap(slot, env.variant_for(slot, protein_differs=True), "single")
    after = env.trips.get(trip["trip_id"])
    assert after["items"] == [] and after["bought_items"] == done["bought_items"]
    assert [c["changed_since_shopping"] for c in after["weeks"][0]["cells"]] == [True, True]


def test_preview_with_one_group_bought_limits_extra_needed_to_it(env: SwapEnv) -> None:
    slot = env.linked_lunch()
    variant = env.variant_for(slot, protein_differs=True)
    trip = env.trips.create([env.plan_id])
    env.trips.mark_purchases(trip["trip_id"], [(env.plan_id, "long_lasting")], trip["version"])
    (impact,) = env.svc.preview_swap(env.plan_id, slot["assignment_id"], variant, "pair", 1).trips
    assert impact["status"] == "open"
    assert impact["groups"] == [{"group": "long_lasting", "state": "bought"}, {"group": "fresh", "state": "to_buy"}]
    labels = INPUTS.shelf_lives()
    assert {labels[r["ingredient_id"]] for r in impact["extra_needed"]} <= {"long_lasting"}


def test_open_trip_follows_the_swap(env: SwapEnv) -> None:
    trip = env.trips.create([env.plan_id])
    slot = env.unlinked()
    env.swap(slot, env.variant_for(slot, protein_differs=True), "single")
    after = env.trips.get(trip["trip_id"])
    assert after["weeks"][0]["current_revision"] == 2
    assert {(i["ingredient_id"], i["unit"]): i["quantity"] for i in after["items"]} == totals(
        env.current().snapshot.shopping_list
    )


# ---------------------------------------------------------------- undo and regenerate


def test_undo_restores_the_pre_swap_content(env: SwapEnv) -> None:
    slot = env.unlinked()
    env.swap(slot, env.variant_for(slot), "single")
    result = env.svc.undo(env.plan_id, 2, "u1")
    assert result.revision == 3
    restored = env.current()
    assert restored.source == "undo"
    assert restored.snapshot.plan["slots"] == env.plan.snapshot.plan["slots"]
    assert restored.snapshot.shopping_list["sections"] == env.plan.snapshot.shopping_list["sections"]


def test_undo_may_restore_a_rule_violating_revision(env: SwapEnv) -> None:
    slot = env.unlinked()
    violating = env.swap(slot, env.variant_for(slot, protein_differs=True), "single")
    assert violating.rule_warnings
    prop = env.svc.propose_regeneration(env.plan_id, 2, seed=77)
    env.svc.accept_proposal(env.plan_id, prop.proposal_id, 2, "a1")  # revision 3, valid again
    result = env.svc.undo(env.plan_id, 3, "u1")  # restores the swapped, rule-breaking revision 2
    assert result.revision == 4 and result.changed
    assert env.current().snapshot.plan["slots"] == env.repo.get_revision(env.plan_id, 2).snapshot.plan["slots"]


def test_replaced_swaps_counts_only_swapped_slots_the_proposal_changes(env: SwapEnv) -> None:
    assert env.svc.propose_regeneration(env.plan_id, 1, seed=5).replaced_swaps == 0
    lunch = env.linked_lunch()
    env.swap(lunch, env.variant_for(lunch, protein_differs=True), "pair")
    swapped = {lunch["assignment_id"], lunch["linked_assignment_id"]}
    prop = env.svc.propose_regeneration(env.plan_id, 2, seed=5)
    changed = {c.assignment_id for c in prop.changes}
    assert prop.replaced_swaps == len(swapped & changed) and prop.replaced_swaps >= 1
    assert env.svc.get_proposal(env.plan_id, prop.proposal_id).replaced_swaps == prop.replaced_swaps
    # accepting the regeneration makes it the new generated baseline: nothing is "swapped" any more
    env.svc.accept_proposal(env.plan_id, prop.proposal_id, 2, "a1")
    assert env.svc.propose_regeneration(env.plan_id, 3, seed=6).replaced_swaps == 0


def test_replaced_swaps_is_zero_after_undoing_the_swap(env: SwapEnv) -> None:
    slot = env.unlinked()
    env.swap(slot, env.variant_for(slot), "single")
    env.svc.undo(env.plan_id, 2, "u1")
    assert env.svc.propose_regeneration(env.plan_id, 3, seed=5).replaced_swaps == 0


def test_catalog_lists_every_variant_sorted(env: SwapEnv) -> None:
    items = env.svc.list_swap_variants()
    assert [(i.name, i.variant_id) for i in items] == sorted((i.name, i.variant_id) for i in items)
    assert len({i.variant_id for i in items}) == len(items) > 50
    sample = next(i for i in items if i.carb is None)
    assert INPUTS.recipes[sample.recipe_id].name == sample.name


def test_every_listed_variant_makes_a_valid_snapshot(env: SwapEnv) -> None:
    from mealplanner.domain.swap import apply_swap, catalog_variants
    from mealplanner.domain.weekly_plan import stamp_revision

    base = env.current()
    for lunch_or_dinner in (env.linked_lunch(), env.unlinked()):
        for variant in catalog_variants(INPUTS).values():
            raw = apply_swap(
                base.snapshot, lunch_or_dinner["assignment_id"], variant, "pair", INPUTS, base.pantry
            )
            stamp_revision(raw, 2, base.pantry)  # validates the full snapshot invariants
    assert "bistec_a_caballo__carb_none" not in catalog_variants(INPUTS)  # two protein rows: unplaceable
