"""Manual meal swaps (phase 5): pure functions over plan snapshots.

A swap replaces the recipe/variant of one dated slot (and, for a linked lunch/dinner
pair, its partner) with any catalog variant. Weekly rules are advisory here: callers
report `check_weekly_rules` violations as warnings and never refuse a swap for them.
Everything below is storage-independent; the lifecycle service (application layer)
decides who may swap which slot and when.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass

from .catalog import Ingredient
from .planner_inputs import PlannerInputs
from .snapshot import PROTEINS, JsonObject, PlanSnapshot
from .variants import RecipeVariant, expand_variants, materialize_variant
from .weekly_plan import MutationResult, PlanRevision, SlotChange, WorkflowError, diff_snapshots

PAIR = "pair"
SINGLE = "single"
SCOPES = (PAIR, SINGLE)

# Revision sources produced by generation (everything else derives from an earlier revision).
SWAP_SOURCE = "swap"
UNDO_SOURCE = "undo"


class SlotNotFound(WorkflowError):
    """The plan has no assignment with that id."""


class VariantNotFound(WorkflowError):
    """The catalog has no recipe variant with that id."""


class SlotNotEditable(WorkflowError):
    """The slot's date is before today in the household timezone."""


@dataclass(frozen=True)
class SwapVariant:
    """One swappable catalog variant as listed to callers."""

    recipe_id: str
    variant_id: str
    name: str
    protein: str
    carb: str | None


@dataclass(frozen=True)
class SwapPreview:
    """Stateless outcome of a would-be swap against a base revision."""

    plan_id: str
    base_revision: int
    changes: tuple[SlotChange, ...]
    grocery_delta: tuple[JsonObject, ...]
    rule_warnings: tuple[JsonObject, ...]
    trips: tuple[JsonObject, ...]  # every list the week is on (see PlanWorkflowService)


@dataclass(frozen=True)
class SwapOutcome:
    """Result of applying a swap: the revision pointer, its snapshot, and advisory warnings."""

    result: MutationResult
    snapshot: PlanSnapshot
    rule_warnings: tuple[JsonObject, ...]


# ---------------------------------------------------------------- catalog


def _is_representable(variant: RecipeVariant, inputs: PlannerInputs) -> bool:
    """A plan slot needs exactly one raw protein ingredient row (snapshot invariant), so a
    variant whose recipe lists a second `role: protein` ingredient cannot be stored in any
    plan and is not offered (generation cannot place it either)."""
    content = materialize_variant(variant, "lunch", inputs.rules, inputs.ingredients)
    return sum(1 for line in content.ingredients if line.role == "protein") == 1


def catalog_variants(inputs: PlannerInputs) -> dict[str, RecipeVariant]:
    """Every swappable recipe variant of the current inputs, keyed by variant id."""
    variants = expand_variants(dict(inputs.recipes), dict(inputs.ingredients))
    return {v.variant_id: v for v in variants if _is_representable(v, inputs)}


def list_swap_variants(inputs: PlannerInputs) -> list[SwapVariant]:
    """All swappable variants, sorted by display name then variant id."""
    items = [
        SwapVariant(
            v.base_recipe_id,
            v.variant_id,
            v.recipe.name,
            v.recipe.tags.protein_group.value,
            v.carb_ingredient_id,
        )
        for v in catalog_variants(inputs).values()
    ]
    items.sort(key=lambda i: (i.name, i.variant_id))
    return items


# ---------------------------------------------------------------- applying a swap


def find_slot(snapshot: PlanSnapshot, slot_id: str) -> JsonObject:
    for slot in snapshot.plan["slots"]:
        if slot["assignment_id"] == slot_id:
            return slot
    raise SlotNotFound(slot_id)


def _materialize(slot: JsonObject, variant: RecipeVariant, inputs: PlannerInputs) -> JsonObject:
    """The slot with its recipe-dependent fields replaced (ids, date, meal stay)."""
    content = materialize_variant(variant, slot["meal"], inputs.rules, inputs.ingredients)
    rows = [
        {
            "ingredient_id": line.item,
            "display": line.display,
            "quantity": line.qty,
            "unit": line.unit,
            "role": line.role,
        }
        for line in content.ingredients
    ]
    protein_row = next(r for r in rows if r["role"] == "protein")
    carb_quantity = None
    if content.carb is not None:
        carb_row = next(r for r in rows if r["role"] == "carb")
        carb_quantity = {"value": carb_row["quantity"], "unit": carb_row["unit"]}
    updated = dict(slot)
    updated.update(
        recipe_id=variant.base_recipe_id,
        recipe_name=variant.recipe.name,
        variant_id=variant.variant_id,
        protein=content.protein,
        protein_quantity={"value": protein_row["quantity"], "unit": protein_row["unit"]},
        carb=content.carb,
        carb_quantity=carb_quantity,
        ingredients=rows,
    )
    return updated


def apply_swap(
    base: PlanSnapshot,
    slot_id: str,
    variant: RecipeVariant,
    scope: str,
    inputs: PlannerInputs,
    pantry: Sequence[str],
) -> PlanSnapshot:
    """Return the base snapshot with the slot swapped to `variant`.

    pair: a linked slot swaps together with its partner and they stay linked; an unlinked
    slot is a single swap. single: only that slot changes and the link is removed from both.
    Derived counts and groceries are recomputed from the resulting assignments using the
    base revision's captured pantry. The result still carries the base revision and
    snapshot token; stamp_revision gives it its own. Every other assignment is untouched.
    """
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    slots = deepcopy(base.plan["slots"])
    by_id = {s["assignment_id"]: s for s in slots}
    target = by_id.get(slot_id)
    if target is None:
        raise SlotNotFound(slot_id)
    partner_id = target.get("linked_assignment_id")
    partner = by_id.get(partner_id) if partner_id else None
    swap_ids = [slot_id]
    if partner is not None and scope == PAIR:
        swap_ids.append(partner["assignment_id"])
    elif partner is not None:  # single swap of a linked slot: drop the link on both halves
        for half in (target, partner):
            half["linked_assignment_id"] = None
            half["extended_from_lunch"] = False
    for aid in swap_ids:
        by_id[aid].update(_materialize(by_id[aid], variant, inputs))
    plan = deepcopy(base.plan)
    plan["slots"] = slots
    carbs = Counter(s["carb"] for s in slots if s["carb"] is not None)
    proteins = Counter(s["protein"] for s in slots)
    plan["derived"] = dict(plan["derived"])
    plan["derived"]["protein_counts"] = {p: proteins[p] for p in PROTEINS}
    plan["derived"]["carb_counts"] = dict(carbs)
    shopping = deepcopy(base.shopping_list)
    shopping["sections"] = rebuild_sections(base.shopping_list, slots, set(pantry), inputs.ingredients)
    return PlanSnapshot(plan=plan, shopping_list=shopping)


def rebuild_sections(
    base_shopping: JsonObject,
    slots: Sequence[JsonObject],
    pantry: set[str],
    ingredients: Mapping[str, Ingredient],
) -> dict[str, list[JsonObject]]:
    """Grocery sections from assignments minus pantry; known rows keep their order and notes."""
    totals: dict[tuple[str, str], float] = {}
    displays: dict[tuple[str, str], str] = {}
    for slot in slots:
        for row in slot["ingredients"]:
            if row["ingredient_id"] in pantry:
                continue
            key = (row["ingredient_id"], row["unit"])
            totals[key] = totals.get(key, 0.0) + row["quantity"]
            displays.setdefault(key, row["display"])
    known: dict[tuple[str, str], JsonObject] = {}
    for rows in base_shopping["sections"].values():
        for row in rows:
            known[(row["ingredient_id"], row["unit"])] = row
    ordered = [k for k in known if k in totals] + [k for k in totals if k not in known]
    sections: dict[str, list[JsonObject]] = {}
    for key in ordered:
        old = known.get(key)
        ingredient = ingredients.get(key[0])
        section = old["section"] if old else (ingredient.section if ingredient else "other")
        sections.setdefault(section, []).append(
            {
                "ingredient_id": key[0],
                "display": old["display"] if old else displays[key],
                "quantity": round(totals[key], 2),
                "unit": key[1],
                "section": section,
                "notes": old.get("notes") if old else None,
            }
        )
    return sections


# ---------------------------------------------------------------- comparisons


def _quantities(shopping: JsonObject) -> dict[tuple[str, str], JsonObject]:
    return {
        (r["ingredient_id"], r["unit"]): r
        for rows in shopping["sections"].values()
        for r in rows
    }


def grocery_delta(before: JsonObject, after: JsonObject) -> list[JsonObject]:
    """Rows whose weekly quantity changed (before/after are 0 when absent), by section/display."""
    old, new = _quantities(before), _quantities(after)
    rows = []
    for key in old.keys() | new.keys():
        ref = new.get(key) or old[key]
        b = old[key]["quantity"] if key in old else 0
        a = new[key]["quantity"] if key in new else 0
        difference = round(a - b, 2)
        if difference == 0:
            continue
        rows.append(
            {
                "ingredient_id": key[0],
                "display": ref["display"],
                "unit": key[1],
                "section": ref["section"],
                "before": b,
                "after": a,
                "difference": difference,
            }
        )
    rows.sort(key=lambda r: (r["section"], r["display"], r["ingredient_id"], r["unit"]))
    return rows


def extra_needed(
    covered: JsonObject,
    current: JsonObject,
    only: Callable[[str], bool] | None = None,
) -> list[JsonObject]:
    """Quantities `current` needs beyond `covered` (positive differences only).

    only, when given, keeps just the ingredient ids it accepts (e.g. one list's groups).
    """
    old, new = _quantities(covered), _quantities(current)
    rows = []
    for key, row in new.items():
        if only is not None and not only(key[0]):
            continue
        extra = round(row["quantity"] - (old[key]["quantity"] if key in old else 0), 2)
        if extra > 0:
            rows.append(
                {
                    "ingredient_id": key[0],
                    "display": row["display"],
                    "unit": key[1],
                    "quantity": extra,
                    "section": row["section"],
                }
            )
    rows.sort(key=lambda r: (r["section"], r["display"], r["ingredient_id"], r["unit"]))
    for r in rows:
        del r["section"]
    return rows


# ---------------------------------------------------------------- provenance


def generation_baseline(revisions: Sequence[PlanRevision]) -> PlanRevision:
    """The latest generated revision the newest revision's content derives from.

    A swap derives from the revision before it; an undo restores the content of the
    revision two before it (it is appended on top of N and restores N - 1). Create,
    regenerate, and import revisions are generated.
    """
    by_number = {r.revision: r for r in revisions}
    number = max(by_number)
    while True:
        rev = by_number[number]
        if rev.source == SWAP_SOURCE and number > 1:
            number -= 1
        elif rev.source == UNDO_SOURCE and number > 2:
            number -= 2
        else:
            return rev


def swapped_assignment_ids(revisions: Sequence[PlanRevision]) -> set[str]:
    """Assignments of the newest revision that differ from its generation baseline."""
    latest = max(revisions, key=lambda r: r.revision)
    baseline = generation_baseline(revisions)
    return {c.assignment_id for c in diff_snapshots(baseline.snapshot, latest.snapshot)}
