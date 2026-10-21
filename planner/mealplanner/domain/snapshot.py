"""Storage-independent plan snapshot validation, identity, and legacy-JSON normalization."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

JsonObject = dict[str, Any]
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PROTEINS = ("chicken", "beef", "pork", "fish", "egg")


class SourceInvalidError(Exception):
    """Legacy JSON plan/shopping/pantry sources do not form a valid snapshot."""


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


# ---------------------------------------------------------------- legacy JSON normalization

UNITS = {"g", "ml", "units"}
ROLES = {
    "protein",
    "protein_extra",
    "veg",
    "fat",
    "condiment",
    "garnish",
    "herb",
    "spice",
    "dairy",
    "other",
    "carb",
}


def _require_source(condition: bool, message: str) -> None:
    if not condition:
        raise SourceInvalidError(message)


def _number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _normalize_ingredients(source_slot: JsonObject) -> list[JsonObject]:
    source_rows = source_slot.get("ingredients")
    if not isinstance(source_rows, list):
        raise SourceInvalidError("slot ingredients must be a list")
    result = []
    for row in source_rows:
        _require_source(isinstance(row, dict), "slot ingredient row must be an object")
        ingredient_id = row.get("item")
        display = row.get("display")
        quantity = row.get("qty")
        unit = row.get("unit")
        role = row.get("role")
        _require_source(
            isinstance(ingredient_id, str) and bool(ingredient_id), "ingredient ID is required"
        )
        _require_source(isinstance(display, str), "ingredient display is required")
        _require_source(_number(quantity), "ingredient quantity must be a non-negative finite number")
        _require_source(isinstance(unit, str) and unit in UNITS, "ingredient unit is unsupported")
        _require_source(isinstance(role, str) and role in ROLES, "ingredient role is unsupported")
        result.append(
            {
                "ingredient_id": ingredient_id,
                "display": display,
                "quantity": quantity,
                "unit": unit,
                "role": role,
            }
        )
    return result


def normalize_snapshot(
    source_plan: JsonObject,
    source_shopping: JsonObject,
    pantry: set[str],
    week_start: date,
    timezone: str = "America/Bogota",
) -> PlanSnapshot:
    """Convert legacy camelCase weekday data and validate its full read model."""
    _require_source(week_start.weekday() == 0, "configured week_start must be Monday")
    _require_source(isinstance(timezone, str) and bool(timezone), "timezone is required")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise SourceInvalidError("configured timezone is invalid") from exc
    _require_source(
        isinstance(source_plan, dict) and isinstance(source_plan.get("slots"), list),
        "plan source must contain slots",
    )
    _require_source(
        isinstance(source_plan.get("derived"), dict), "plan source must contain derived counts"
    )
    _require_source(
        isinstance(source_shopping, dict) and isinstance(source_shopping.get("sections"), dict),
        "shopping source must contain sections",
    )
    end = week_start + timedelta(days=6)
    plan_id = "plan_" + week_start.isoformat().replace("-", "_")
    plan: JsonObject = {
        "plan_id": plan_id,
        "week_start": week_start.isoformat(),
        "week_end": end.isoformat(),
        "timezone": timezone,
        "revision": 1,
        "generated_at": source_plan.get("generated_at"),
        "seed": source_plan.get("seed"),
        "slots": [],
        "derived": deepcopy(source_plan["derived"]),
    }
    _require_source(
        isinstance(plan["generated_at"], str) and bool(plan["generated_at"]),
        "generated_at is required",
    )
    _require_source(
        re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            plan["generated_at"],
        )
        is not None,
        "generated_at must use RFC 3339 date-time format",
    )
    try:
        generated_at = datetime.fromisoformat(plan["generated_at"].replace("Z", "+00:00"))
        _require_source(generated_at.tzinfo is not None, "generated_at must include a timezone")
    except ValueError as exc:
        raise SourceInvalidError("generated_at must be a timezone-aware ISO timestamp") from exc
    _require_source(
        isinstance(plan["seed"], int) and not isinstance(plan["seed"], bool),
        "seed must be an integer",
    )
    source_slots = source_plan["slots"]
    _require_source(bool(source_slots), "plan source must contain at least one assignment")
    slot_by_day_meal: dict[tuple[str, str], JsonObject] = {}
    for source in source_slots:
        _require_source(isinstance(source, dict), "slot must be an object")
        day = source.get("day")
        meal = source.get("meal")
        _require_source(day in DAYS and meal in {"lunch", "dinner"}, "slot day or meal is invalid")
        key = (day, meal)
        _require_source(key not in slot_by_day_meal, "duplicate source date/meal")
        slot_by_day_meal[key] = source
    slots: list[JsonObject] = []
    for source in source_slots:
        day = source["day"]
        meal = source["meal"]
        meal_date = week_start + timedelta(days=DAYS.index(day))
        ingredients = _normalize_ingredients(source)
        protein_rows = [row for row in ingredients if row["role"] == "protein"]
        _require_source(len(protein_rows) == 1, "slot must have exactly one raw protein ingredient row")
        protein_quantity = source.get("proteinQty")
        _require_source(_number(protein_quantity), "protein quantity must be non-negative and finite")
        _require_source(
            protein_rows[0]["quantity"] == protein_quantity,
            "protein quantity does not match ingredient row",
        )
        carb_id = source.get("carb")
        if carb_id == "none":
            carb_id = None
        carb_quantity_value = source.get("carbQty")
        carb_quantity = None
        if carb_id is None:
            _require_source(carb_quantity_value is None, "carb quantity must be null when carb is absent")
            _require_source(
                not any(row["role"] == "carb" for row in ingredients),
                "carb ingredient row exists without carb ID",
            )
        else:
            _require_source(isinstance(carb_id, str) and bool(carb_id), "carb ID is invalid")
            _require_source(_number(carb_quantity_value), "carb quantity must be non-negative and finite")
            carb_rows = [
                row
                for row in ingredients
                if row["ingredient_id"] == carb_id and row["role"] == "carb"
            ]
            _require_source(len(carb_rows) == 1, "carb quantity must have one matching ingredient row")
            _require_source(
                carb_rows[0]["quantity"] == carb_quantity_value,
                "carb quantity does not match ingredient row",
            )
            carb_quantity = {"value": carb_quantity_value, "unit": carb_rows[0]["unit"]}
        extended = source.get("extendedFromLunch")
        _require_source(isinstance(extended, bool), "extendedFromLunch must be boolean")
        _require_source(not extended or meal == "dinner", "only dinner may extend lunch")
        linked_id = None
        if meal == "dinner" and extended:
            linked_id = f"slot_{meal_date.isoformat()}_lunch"
        elif meal == "lunch":
            paired_dinner = slot_by_day_meal.get((day, "dinner"))
            if paired_dinner and paired_dinner.get("extendedFromLunch") is True:
                linked_id = f"slot_{meal_date.isoformat()}_dinner"
        slot = {
            "assignment_id": f"slot_{meal_date.isoformat()}_{meal}",
            "date": meal_date.isoformat(),
            "day": day,
            "meal": meal,
            "recipe_id": source.get("recipeId"),
            "recipe_name": source.get("recipeName"),
            "variant_id": source.get("variantId"),
            "protein": source.get("protein"),
            "protein_quantity": {"value": protein_quantity, "unit": protein_rows[0]["unit"]},
            "carb": carb_id,
            "carb_quantity": carb_quantity,
            "ingredients": ingredients,
            "extended_from_lunch": extended,
            "linked_assignment_id": linked_id,
        }
        for field in ("recipe_id", "recipe_name", "variant_id"):
            _require_source(isinstance(slot[field], str) and bool(slot[field]), f"{field} is required")
        _require_source(slot["protein"] in PROTEINS, "protein type is invalid")
        slots.append(slot)
    slots.sort(key=lambda slot: (slot["date"], 0 if slot["meal"] == "lunch" else 1))
    plan["slots"] = slots
    shopping: JsonObject = {
        "plan_id": plan_id,
        "revision": 1,
        "week_start": week_start.isoformat(),
        "week_end": end.isoformat(),
        "sections": {},
    }
    for section_name, source_rows in source_shopping["sections"].items():
        _require_source(
            isinstance(section_name, str) and isinstance(source_rows, list),
            "shopping section is invalid",
        )
        rows = []
        for source in source_rows:
            _require_source(isinstance(source, dict), "shopping row must be an object")
            item = {
                "ingredient_id": source.get("item"),
                "display": source.get("display"),
                "quantity": source.get("quantity"),
                "unit": source.get("unit"),
                "section": source.get("section"),
                "notes": source.get("notes"),
            }
            _require_source(
                isinstance(item["ingredient_id"], str) and bool(item["ingredient_id"]),
                "shopping ingredient ID is required",
            )
            _require_source(isinstance(item["display"], str), "shopping display is required")
            _require_source(_number(item["quantity"]), "shopping quantity must be non-negative and finite")
            _require_source(
                isinstance(item["unit"], str) and item["unit"] in UNITS,
                "shopping unit is unsupported",
            )
            _require_source(
                item["section"] == section_name,
                "shopping section field must match its containing section",
            )
            _require_source(
                item["notes"] is None or isinstance(item["notes"], str),
                "shopping notes must be string or null",
            )
            rows.append(item)
        shopping["sections"][section_name] = rows
    token = canonical_snapshot_id(plan, shopping)
    plan["snapshot_id"] = token
    shopping["snapshot_id"] = token
    try:
        validate_snapshot(plan, shopping, pantry)
    except SnapshotValidationError as exc:
        raise SourceInvalidError(str(exc)) from exc
    return PlanSnapshot(plan=plan, shopping_list=shopping)
