"""Load and validate the legacy JSON plan as one immutable read snapshot."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml  # type: ignore[import-untyped]

from .read_api import JsonObject, PlanReadRepository
from .snapshot import (
    PlanSnapshot,
    SnapshotValidationError,
    canonical_snapshot_id,
    validate_snapshot,
)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PROTEINS = ("chicken", "beef", "pork", "fish", "egg")
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


class SourceUnavailableError(Exception):
    """A configured JSON or pantry input file could not be read."""


class SourceInvalidError(Exception):
    """The JSON and pantry files are readable but do not form a valid snapshot."""


class JsonPlanReadRepository(PlanReadRepository):
    """In-memory immutable repository initialized from a coherent JSON fixture."""

    def __init__(self, snapshots: list[PlanSnapshot]) -> None:
        self._snapshots = {
            snapshot.plan["plan_id"]: PlanSnapshot(
                plan=deepcopy(snapshot.plan), shopping_list=deepcopy(snapshot.shopping_list)
            )
            for snapshot in snapshots
        }

    @classmethod
    def from_files(
        cls,
        plan_path: Path,
        shopping_path: Path,
        pantry_path: Path,
        week_start: date,
        timezone: str = "America/Bogota",
    ) -> JsonPlanReadRepository:
        """Read all source files once and publish only a validated snapshot."""
        paths = (plan_path, shopping_path, pantry_path)
        try:
            before = tuple(_file_signature(path) for path in paths)
            plan_text, shopping_text, pantry_text = (
                path.read_text(encoding="utf-8") for path in paths
            )
            after = tuple(_file_signature(path) for path in paths)
        except OSError as exc:
            raise SourceUnavailableError("configured plan source is unavailable") from exc
        except UnicodeDecodeError as exc:
            raise SourceInvalidError("configured plan source is invalid") from exc
        if before != after:
            raise SourceUnavailableError("configured plan source changed during startup capture")
        try:
            plan_data = json.loads(plan_text)
            shopping_data = json.loads(shopping_text)
            pantry_data = yaml.safe_load(pantry_text)
        except (json.JSONDecodeError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise SourceInvalidError("configured plan source is invalid") from exc
        if not isinstance(pantry_data, list) or any(
            not isinstance(item, str) for item in pantry_data
        ):
            raise SourceInvalidError("pantry source must be a list of ingredient IDs")
        try:
            snapshot = normalize_snapshot(
                plan_data, shopping_data, set(pantry_data), week_start, timezone
            )
        except SourceInvalidError:
            raise
        except (TypeError, KeyError, IndexError, AttributeError) as exc:
            raise SourceInvalidError("configured plan source is invalid") from exc
        return cls([snapshot])

    def list_plan_summaries(self) -> list[JsonObject]:
        return [snapshot.summary for snapshot in self._snapshots.values()]

    def get_plan(self, plan_id: str) -> JsonObject | None:
        snapshot = self._snapshots.get(plan_id)
        return deepcopy(snapshot.plan) if snapshot else None

    def get_shopping_list(self, plan_id: str) -> JsonObject | None:
        snapshot = self._snapshots.get(plan_id)
        return deepcopy(snapshot.shopping_list) if snapshot else None


def _file_signature(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def _require(condition: bool, message: str) -> None:
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
        _require(isinstance(row, dict), "slot ingredient row must be an object")
        ingredient_id = row.get("item")
        display = row.get("display")
        quantity = row.get("qty")
        unit = row.get("unit")
        role = row.get("role")
        _require(
            isinstance(ingredient_id, str) and bool(ingredient_id), "ingredient ID is required"
        )
        _require(isinstance(display, str), "ingredient display is required")
        _require(_number(quantity), "ingredient quantity must be a non-negative finite number")
        _require(isinstance(unit, str) and unit in UNITS, "ingredient unit is unsupported")
        _require(isinstance(role, str) and role in ROLES, "ingredient role is unsupported")
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
    _require(week_start.weekday() == 0, "configured week_start must be Monday")
    _require(isinstance(timezone, str) and bool(timezone), "timezone is required")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise SourceInvalidError("configured timezone is invalid") from exc
    _require(
        isinstance(source_plan, dict) and isinstance(source_plan.get("slots"), list),
        "plan source must contain slots",
    )
    _require(
        isinstance(source_plan.get("derived"), dict), "plan source must contain derived counts"
    )
    _require(
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
    _require(
        isinstance(plan["generated_at"], str) and bool(plan["generated_at"]),
        "generated_at is required",
    )
    _require(
        re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            plan["generated_at"],
        )
        is not None,
        "generated_at must use RFC 3339 date-time format",
    )
    try:
        generated_at = datetime.fromisoformat(plan["generated_at"].replace("Z", "+00:00"))
        _require(generated_at.tzinfo is not None, "generated_at must include a timezone")
    except ValueError as exc:
        raise SourceInvalidError("generated_at must be a timezone-aware ISO timestamp") from exc
    _require(
        isinstance(plan["seed"], int) and not isinstance(plan["seed"], bool),
        "seed must be an integer",
    )
    source_slots = source_plan["slots"]
    _require(bool(source_slots), "plan source must contain at least one assignment")
    slot_by_day_meal: dict[tuple[str, str], JsonObject] = {}
    for source in source_slots:
        _require(isinstance(source, dict), "slot must be an object")
        day = source.get("day")
        meal = source.get("meal")
        _require(day in DAYS and meal in {"lunch", "dinner"}, "slot day or meal is invalid")
        key = (day, meal)
        _require(key not in slot_by_day_meal, "duplicate source date/meal")
        slot_by_day_meal[key] = source
    slots: list[JsonObject] = []
    for source in source_slots:
        day = source["day"]
        meal = source["meal"]
        meal_date = week_start + timedelta(days=DAYS.index(day))
        ingredients = _normalize_ingredients(source)
        protein_rows = [row for row in ingredients if row["role"] == "protein"]
        _require(len(protein_rows) == 1, "slot must have exactly one raw protein ingredient row")
        protein_quantity = source.get("proteinQty")
        _require(_number(protein_quantity), "protein quantity must be non-negative and finite")
        _require(
            protein_rows[0]["quantity"] == protein_quantity,
            "protein quantity does not match ingredient row",
        )
        carb_id = source.get("carb")
        if carb_id == "none":
            carb_id = None
        carb_quantity_value = source.get("carbQty")
        carb_quantity = None
        if carb_id is None:
            _require(carb_quantity_value is None, "carb quantity must be null when carb is absent")
            _require(
                not any(row["role"] == "carb" for row in ingredients),
                "carb ingredient row exists without carb ID",
            )
        else:
            _require(isinstance(carb_id, str) and bool(carb_id), "carb ID is invalid")
            _require(_number(carb_quantity_value), "carb quantity must be non-negative and finite")
            carb_rows = [
                row
                for row in ingredients
                if row["ingredient_id"] == carb_id and row["role"] == "carb"
            ]
            _require(len(carb_rows) == 1, "carb quantity must have one matching ingredient row")
            _require(
                carb_rows[0]["quantity"] == carb_quantity_value,
                "carb quantity does not match ingredient row",
            )
            carb_quantity = {"value": carb_quantity_value, "unit": carb_rows[0]["unit"]}
        extended = source.get("extendedFromLunch")
        _require(isinstance(extended, bool), "extendedFromLunch must be boolean")
        _require(not extended or meal == "dinner", "only dinner may extend lunch")
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
            _require(isinstance(slot[field], str) and bool(slot[field]), f"{field} is required")
        _require(slot["protein"] in PROTEINS, "protein type is invalid")
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
        _require(
            isinstance(section_name, str) and isinstance(source_rows, list),
            "shopping section is invalid",
        )
        rows = []
        for source in source_rows:
            _require(isinstance(source, dict), "shopping row must be an object")
            item = {
                "ingredient_id": source.get("item"),
                "display": source.get("display"),
                "quantity": source.get("quantity"),
                "unit": source.get("unit"),
                "section": source.get("section"),
                "notes": source.get("notes"),
            }
            _require(
                isinstance(item["ingredient_id"], str) and bool(item["ingredient_id"]),
                "shopping ingredient ID is required",
            )
            _require(isinstance(item["display"], str), "shopping display is required")
            _require(_number(item["quantity"]), "shopping quantity must be non-negative and finite")
            _require(
                isinstance(item["unit"], str) and item["unit"] in UNITS,
                "shopping unit is unsupported",
            )
            _require(
                item["section"] == section_name,
                "shopping section field must match its containing section",
            )
            _require(
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
