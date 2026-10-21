"""Storage-independent normalized plan snapshot validation and identity."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

JsonObject = dict[str, Any]
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PROTEINS = ("chicken", "beef", "pork", "fish", "egg")


class SnapshotValidationError(ValueError):
    """A normalized plan and grocery payload violate shared domain rules."""


@dataclass(frozen=True)
class PlanSnapshot:
    """A fully normalized current plan and matching shopping payload."""

    plan: JsonObject
    shopping_list: JsonObject

    @property
    def summary(self) -> JsonObject:
        return {
            key: deepcopy(self.plan[key])
            for key in (
                "plan_id",
                "week_start",
                "week_end",
                "timezone",
                "revision",
                "snapshot_id",
                "generated_at",
                "seed",
            )
        }


def canonical_snapshot_id(plan: JsonObject, shopping: JsonObject) -> str:
    """Hash canonical JSON payloads while excluding their snapshot tokens."""
    plan_payload = {key: value for key, value in plan.items() if key != "snapshot_id"}
    shopping_payload = {key: value for key, value in shopping.items() if key != "snapshot_id"}
    canonical = json.dumps(
        {"plan": plan_payload, "shopping_list": shopping_payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SnapshotValidationError(message)


def validate_snapshot(plan: JsonObject, shopping: JsonObject, pantry: set[str]) -> None:
    """Validate normalized plan and shopping DTOs, independent of their storage."""
    _require(
        isinstance(plan, dict) and isinstance(shopping, dict), "snapshot payloads must be objects"
    )
    _require(
        isinstance(pantry, set) and all(isinstance(item, str) for item in pantry),
        "pantry IDs must be strings",
    )
    for field in (
        "plan_id",
        "week_start",
        "week_end",
        "timezone",
        "revision",
        "snapshot_id",
        "generated_at",
        "seed",
    ):
        _require(field in plan, f"plan metadata is missing {field}")
    _require(plan.get("plan_id") == shopping.get("plan_id"), "plan and shopping IDs must match")
    _require(
        plan.get("revision") == shopping.get("revision"), "plan and shopping revisions must match"
    )
    _require(
        plan.get("snapshot_id") == shopping.get("snapshot_id"),
        "plan and shopping snapshot IDs must match",
    )
    _require(
        plan.get("week_start") == shopping.get("week_start"),
        "plan and shopping week starts must match",
    )
    _require(
        plan.get("week_end") == shopping.get("week_end"), "plan and shopping week ends must match"
    )
    _require(
        isinstance(plan.get("revision"), int)
        and not isinstance(plan["revision"], bool)
        and plan["revision"] >= 1,
        "revision must be a positive integer",
    )
    _require(
        isinstance(plan.get("snapshot_id"), str) and bool(plan["snapshot_id"]),
        "snapshot ID is required",
    )
    _require(
        plan["snapshot_id"] == canonical_snapshot_id(plan, shopping),
        "snapshot ID does not match canonical plan and shopping content",
    )
    try:
        start = date.fromisoformat(plan["week_start"])
        end = date.fromisoformat(plan["week_end"])
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError("plan week dates must be ISO calendar dates") from exc
    _require(
        start.weekday() == 0 and end == start + timedelta(days=6),
        "plan week must be Monday through Sunday",
    )
    _require(
        isinstance(plan.get("slots"), list) and bool(plan["slots"]), "plan must contain assignments"
    )
    _require(isinstance(plan.get("derived"), dict), "derived counts must be an object")
    sections = shopping.get("sections")
    if not isinstance(sections, dict):
        raise SnapshotValidationError("shopping sections must be an object")

    assignment_ids: set[str] = set()
    date_meals: set[tuple[str, str]] = set()
    by_id: dict[str, JsonObject] = {}
    proteins: Counter[str] = Counter()
    carbs: Counter[str] = Counter()
    assignment_totals: dict[tuple[str, str], float] = defaultdict(float)
    assignment_units: dict[str, set[str]] = defaultdict(set)
    order: list[tuple[str, int]] = []
    for slot in plan["slots"]:
        _require(isinstance(slot, dict), "assignment must be an object")
        aid = slot.get("assignment_id")
        _require(
            isinstance(aid, str) and bool(aid) and aid not in assignment_ids,
            "assignment IDs must be unique",
        )
        assignment_ids.add(aid)
        by_id[aid] = slot
        try:
            meal_date = date.fromisoformat(slot["date"])
        except (TypeError, ValueError, KeyError) as exc:
            raise SnapshotValidationError("assignment date must be an ISO calendar date") from exc
        _require(start <= meal_date <= end, "assignment date is outside its plan week")
        _require(
            slot.get("day") == DAYS[meal_date.weekday()], "assignment weekday does not match date"
        )
        meal = slot.get("meal")
        _require(meal in {"lunch", "dinner"}, "assignment meal is invalid")
        date_meal = (slot["date"], meal)
        _require(date_meal not in date_meals, "date/meal assignments must be unique")
        date_meals.add(date_meal)
        order.append((slot["date"], 0 if meal == "lunch" else 1))
        protein = slot.get("protein")
        _require(protein in PROTEINS, "assignment protein is invalid")
        proteins[protein] += 1
        carb_id = slot.get("carb")
        if carb_id is not None:
            _require(
                isinstance(carb_id, str) and bool(carb_id),
                "carb ID must be a nonempty string or null",
            )
            carbs[carb_id] += 1
        ingredient_rows = slot.get("ingredients")
        _require(isinstance(ingredient_rows, list), "assignment ingredients must be an array")
        protein_rows = []
        carb_rows = []
        for ingredient in ingredient_rows:
            _require(isinstance(ingredient, dict), "assignment ingredient must be an object")
            ingredient_id, unit, quantity = (
                ingredient.get("ingredient_id"),
                ingredient.get("unit"),
                ingredient.get("quantity"),
            )
            _require(
                isinstance(ingredient_id, str) and bool(ingredient_id), "ingredient ID is required"
            )
            _require(isinstance(unit, str) and bool(unit), "ingredient unit is required")
            _require(
                isinstance(quantity, (int, float))
                and not isinstance(quantity, bool)
                and quantity >= 0,
                "ingredient quantity must be non-negative",
            )
            assignment_units[ingredient_id].add(unit)
            if ingredient.get("role") == "protein":
                protein_rows.append(ingredient)
            if ingredient.get("role") == "carb":
                carb_rows.append(ingredient)
            if ingredient_id not in pantry:
                assignment_totals[(ingredient_id, unit)] += quantity
        protein_quantity = slot.get("protein_quantity")
        _require(isinstance(protein_quantity, dict), "protein quantity is required")
        _require(
            len(protein_rows) == 1
            and protein_rows[0]["ingredient_id"]
            in {row["ingredient_id"] for row in ingredient_rows if row.get("role") == "protein"},
            "assignment must have exactly one raw protein ingredient",
        )
        _require(
            protein_quantity.get("value") == protein_rows[0]["quantity"]
            and protein_quantity.get("unit") == protein_rows[0]["unit"],
            "protein quantity must match its ingredient row",
        )
        carb_quantity = slot.get("carb_quantity")
        if carb_id is None:
            _require(
                carb_quantity is None and not carb_rows,
                "missing carb must have null quantity and no carb row",
            )
        else:
            _require(
                isinstance(carb_quantity, dict) and len(carb_rows) == 1,
                "carb must have one matching ingredient and quantity",
            )
            _require(
                carb_rows[0]["ingredient_id"] == carb_id
                and carb_quantity.get("value") == carb_rows[0]["quantity"]
                and carb_quantity.get("unit") == carb_rows[0]["unit"],
                "carb quantity must match its ingredient row",
            )
        extended = slot.get("extended_from_lunch")
        _require(isinstance(extended, bool), "extended_from_lunch must be boolean")
        _require(not extended or meal == "dinner", "only a dinner can extend lunch")
    _require(order == sorted(order), "assignments must be ordered by date and meal")

    for slot in plan["slots"]:
        linked_id = slot.get("linked_assignment_id")
        if slot["meal"] == "dinner" and linked_id is not None:
            _require(
                slot["extended_from_lunch"] is True,
                "linked dinner must have extended_from_lunch true",
            )
        if slot["extended_from_lunch"]:
            _require(linked_id is not None, "extended dinner must link to lunch")
        if linked_id is None:
            continue
        other = by_id.get(linked_id)
        if other is None:
            raise SnapshotValidationError("linked assignment is missing")
        _require(
            other.get("linked_assignment_id") == slot["assignment_id"],
            "linked assignments must be reciprocal",
        )
        _require(
            other.get("date") == slot["date"] and other.get("meal") != slot["meal"],
            "linked assignments must be same-day opposite meals",
        )
        _require(
            other.get("recipe_id") == slot.get("recipe_id"),
            "linked assignments must use the same base recipe",
        )

    derived = plan["derived"]
    protein_counts = derived.get("protein_counts")
    _require(
        isinstance(protein_counts, dict)
        and all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0
            for v in protein_counts.values()
        ),
        "protein counts must be non-negative integers",
    )
    _require(
        protein_counts == {protein: proteins[protein] for protein in PROTEINS},
        "protein counts do not match assignments",
    )
    carb_counts = derived.get("carb_counts")
    _require(
        isinstance(carb_counts, dict)
        and all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in carb_counts.values()
        ),
        "carb counts must be non-negative integers",
    )
    _require(carb_counts == dict(carbs), "carb counts do not match assignments")

    shopping_totals: dict[tuple[str, str], float] = defaultdict(float)
    shopping_keys: set[tuple[str, str]] = set()
    shopping_units: dict[str, set[str]] = defaultdict(set)
    for section, rows in sections.items():
        _require(isinstance(section, str) and isinstance(rows, list), "shopping section is invalid")
        for row in rows:
            _require(
                isinstance(row, dict) and row.get("section") == section,
                "shopping row section must match containing section",
            )
            ingredient_id, unit, quantity = (
                row.get("ingredient_id"),
                row.get("unit"),
                row.get("quantity"),
            )
            _require(
                isinstance(ingredient_id, str) and bool(ingredient_id),
                "shopping ingredient ID is required",
            )
            _require(isinstance(unit, str) and bool(unit), "shopping unit is required")
            _require(
                isinstance(quantity, (int, float))
                and not isinstance(quantity, bool)
                and quantity >= 0,
                "shopping quantity must be non-negative",
            )
            key = (ingredient_id, unit)
            _require(key not in shopping_keys, "shopping list repeats an ingredient/unit row")
            shopping_keys.add(key)
            shopping_units[ingredient_id].add(unit)
            shopping_totals[key] += quantity
    _require(
        set(shopping_totals) == set(assignment_totals),
        "shopping ingredients do not match non-pantry assignments",
    )
    for ingredient_id, units in assignment_units.items():
        _require(len(units) == 1, "assignment ingredient has conflicting units")
    for ingredient_id, units in shopping_units.items():
        _require(len(units) == 1, "shopping ingredient has conflicting units")
        _require(
            units == assignment_units.get(ingredient_id, set()),
            "shopping units do not match assignment units",
        )
    for key, quantity in assignment_totals.items():
        _require(
            shopping_totals[key] == round(quantity, 2),
            "shopping quantity does not match pantry-adjusted assignment total",
        )
