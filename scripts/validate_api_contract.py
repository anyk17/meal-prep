#!/usr/bin/env python3
"""Validate the local read API contract and its checked-in fixture examples."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jsonschema
import yaml
from jsonschema import Draft202012Validator, FormatChecker, RefResolver

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "planner"))
from mealplanner.domain.snapshot import (
    SnapshotValidationError,
    canonical_snapshot_id,
    validate_snapshot,
)

CONTRACT = ROOT / "docs/migration-to-cloud/openapi.yaml"
EXAMPLES = ROOT / "docs/migration-to-cloud/examples"
PLAN_FIXTURE = ROOT / "site/public/plan.json"
SHOP_FIXTURE = ROOT / "site/public/shopping_list.json"
PANTRY_FILE = ROOT / "data/pantry.yml"
# Reviewed operations only: adding any path or method requires editing this table.
EXPECTED_PATHS = {
    "/api/v1/plans": {"get", "post"},
    "/api/v1/plans/{plan_id}": {"get"},
    "/api/v1/plans/{plan_id}/shopping-list": {"get"},
    "/api/v1/plans/{plan_id}/revisions": {"get"},
    "/api/v1/plans/{plan_id}/proposals": {"post"},
    "/api/v1/plans/{plan_id}/proposals/{proposal_id}": {"get", "delete"},
    "/api/v1/plans/{plan_id}/proposals/{proposal_id}/accept": {"post"},
    "/api/v1/plans/{plan_id}/undo": {"post"},
    "/api/v1/plans/{plan_id}/swaps": {"post"},
    "/api/v1/plans/{plan_id}/swaps/preview": {"post"},
    "/api/v1/catalog/variants": {"get"},
    "/api/v1/capabilities": {"get"},
    "/api/v1/months/{month}/fill": {"post"},
    "/api/v1/shopping-trips": {"get", "post"},
    "/api/v1/shopping-trips/{trip_id}": {"get", "delete"},
    "/api/v1/shopping-trips/{trip_id}/weeks": {"post"},
    "/api/v1/shopping-trips/{trip_id}/weeks/{plan_id}": {"delete"},
    "/api/v1/shopping-trips/{trip_id}/done": {"post"},
    "/api/v1/shopping-trips/{trip_id}/reopen": {"post"},
    "/api/v1/coverage": {"get"},
}
# POST operations whose body is an empty object (the identifier is in the path).
EMPTY_BODY_PATHS = {"/api/v1/months/{month}/fill"}
# Mutations that must carry a required Idempotency-Key header.
IDEMPOTENT_PATHS = {
    "/api/v1/plans/{plan_id}/proposals/{proposal_id}/accept",
    "/api/v1/plans/{plan_id}/undo",
    "/api/v1/plans/{plan_id}/swaps",
}
# Operations whose 422 must document the weekly-rule failure (error code rule_violation).
RULE_VIOLATION_OPERATIONS = {
    ("/api/v1/plans", "post"),
    ("/api/v1/plans/{plan_id}/proposals", "post"),
    ("/api/v1/plans/{plan_id}/proposals/{proposal_id}/accept", "post"),
}
HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class DuplicateKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping
)


class Checks:
    def __init__(self):
        self.errors = []

    def require(self, condition, message):
        if not condition:
            self.errors.append(message)

    def finish(self):
        if self.errors:
            raise ValueError("\n".join(f"- {e}" for e in self.errors))


def load_json(path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def fail_remote(uri):
    raise ValueError(f"external schema reference is not allowed: {uri}")


def pointer(spec, ref):
    if not ref.startswith("#/"):
        raise ValueError(f"only local JSON Pointer references are allowed: {ref}")
    value = spec
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[token]
    return value


def dereference(spec, value):
    while isinstance(value, dict) and "$ref" in value:
        value = pointer(spec, value["$ref"])
    return value


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def operation_examples(spec, checks):
    referenced = set()
    schemas = []
    for path, path_item in spec.get("paths", {}).items():
        for method, operation in path_item.items():
            if method.lower() not in {
                "get",
                "put",
                "post",
                "delete",
                "patch",
                "options",
                "head",
                "trace",
            }:
                continue
            for status, response in operation.get("responses", {}).items():
                response = dereference(spec, response)
                content = response.get("content", {}).get("application/json", {})
                schema = content.get("schema")
                if schema:
                    schemas.append(schema)
                for name, example in content.get("examples", {}).items():
                    external = example.get("externalValue")
                    checks.require(
                        bool(external),
                        f"{method.upper()} {path} {status} example {name} must use externalValue",
                    )
                    if external:
                        example_path = (CONTRACT.parent / external).resolve()
                        checks.require(
                            example_path.is_relative_to(EXAMPLES.resolve()),
                            f"example escapes examples directory: {external}",
                        )
                        checks.require(
                            example_path.is_file(),
                            f"missing response example: {external}",
                        )
                        if example_path.is_file() and example_path.is_relative_to(
                            EXAMPLES.resolve()
                        ):
                            referenced.add(example_path)
                            if schema:
                                instance = load_json(example_path)
                                validator = Draft202012Validator(
                                    schema,
                                    resolver=RefResolver.from_schema(
                                        spec,
                                        handlers={
                                            "http": fail_remote,
                                            "https": fail_remote,
                                        },
                                    ),
                                    format_checker=FormatChecker(),
                                )
                                for err in validator.iter_errors(instance):
                                    checks.errors.append(
                                        f"{external}: {err.message} at {list(err.absolute_path)}"
                                    )
    for path in sorted(EXAMPLES.glob("*.json")):
        checks.require(
            path.resolve() in referenced,
            f"orphan JSON example: {path.relative_to(ROOT)}",
        )
    return schemas


def check_contract(spec, checks):
    checks.require(spec.get("openapi") == "3.1.0", "contract must use OpenAPI 3.1.0")
    paths = spec.get("paths", {})
    checks.require(
        set(paths) == set(EXPECTED_PATHS),
        f"paths must be exactly {sorted(EXPECTED_PATHS)}",
    )
    operation_ids = []
    for path, path_item in paths.items():
        methods = {
            m.lower()
            for m in path_item
            if m.lower()
            in {"get", "put", "post", "delete", "patch", "options", "head", "trace"}
        }
        checks.require(
            methods == EXPECTED_PATHS.get(path),
            f"{path} must advertise exactly {sorted(EXPECTED_PATHS.get(path, []))}",
        )
        for method, op in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            operation_ids.append(op.get("operationId"))
            params = [
                dereference(spec, p)
                for p in path_item.get("parameters", []) + op.get("parameters", [])
            ]
            for match in re.findall(r"\{([^}]+)\}", path):
                checks.require(
                    any(
                        p.get("in") == "path"
                        and p.get("name") == match
                        and p.get("required") is True
                        for p in params
                    ),
                    f"{path}: path parameter {match} must be declared and required",
                )
            for p in params:
                schema = p.get("schema", {})
                if "example" in p:
                    try:
                        Draft202012Validator(
                            schema, format_checker=FormatChecker()
                        ).validate(p["example"])
                    except jsonschema.ValidationError as e:
                        checks.errors.append(
                            f"{path} parameter {p.get('name')} example violates schema: {e.message}"
                        )
            for name in ("from", "to"):
                if path == "/api/v1/plans" and method.lower() == "get":
                    checks.require(
                        any(
                            p.get("in") == "query"
                            and p.get("name") == name
                            and p.get("required") is True
                            for p in params
                        ),
                        f"{path}: required query parameter {name} missing",
                    )
            if method.lower() == "post":
                body = dereference(spec, op.get("requestBody", {}))
                checks.require(
                    body.get("required") is True, f"{method.upper()} {path}: requestBody must be required"
                )
                body_schema = dereference(
                    spec,
                    body.get("content", {}).get("application/json", {}).get("schema", {}),
                )
                has_fields = (
                    body_schema.get("maxProperties") == 0
                    if path in EMPTY_BODY_PATHS
                    else bool(body_schema.get("required"))
                )
                checks.require(
                    body_schema.get("additionalProperties") is False and has_fields,
                    f"{method.upper()} {path}: request body schema must forbid unknown fields and require fields",
                )
            if method.lower() != "get":
                has_key = any(
                    p.get("in") == "header"
                    and p.get("name") == "Idempotency-Key"
                    and p.get("required") is True
                    for p in params
                )
                checks.require(
                    has_key == (path in IDEMPOTENT_PATHS and method.lower() == "post"),
                    f"{method.upper()} {path}: required Idempotency-Key header must be present exactly on accept, undo and swap",
                )
                for status in ("403",):
                    if path != "/api/v1/plans" or method.lower() == "post":
                        checks.require(
                            status in op.get("responses", {}),
                            f"{method.upper()} {path}: mutation must document {status}",
                        )
            if path.endswith("/shopping-list"):
                for name in ("revision", "snapshot_id"):
                    checks.require(
                        any(
                            p.get("in") == "query"
                            and p.get("name") == name
                            and p.get("required") is True
                            for p in params
                        ),
                        f"{path}: required query parameter {name} missing",
                    )
            for status, response in op.get("responses", {}).items():
                response = dereference(spec, response)
                headers = response.get("headers", {})
                cache = headers.get("Cache-Control", {})
                cache_schema = cache.get("schema", {})
                checks.require(
                    cache_schema.get("const") == "no-store",
                    f"{path} {status}: Cache-Control must be no-store",
                )
    checks.require(
        None not in operation_ids, "every operation must have operationId"
    )
    checks.require(
        len(operation_ids) == len(set(operation_ids)),
        "operationId values must be unique",
    )
    for ref_object in (x for x in walk(spec) if "$ref" in x):
        try:
            pointer(spec, ref_object["$ref"])
        except Exception as e:  # noqa: BLE001
            checks.errors.append(
                f"unresolved/unsupported local reference {ref_object['$ref']}: {e}"
            )
    for name, schema in spec.get("components", {}).get("schemas", {}).items():
        try:
            Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as e:
            checks.errors.append(f"component schema {name} is invalid: {e.message}")
    operation_examples(spec, checks)
    check_rule_violation(spec, checks)


def check_rule_violation(spec, checks):
    """rule_violation is a documented 422 with rule-code details on every candidate-producing write."""
    enum = spec["components"]["schemas"]["ErrorResponse"]["properties"]["error"]["properties"]["code"]["enum"]
    checks.require("rule_violation" in enum, "ErrorResponse code enum must include rule_violation")
    for path, method in sorted(RULE_VIOLATION_OPERATIONS):
        response = spec["paths"].get(path, {}).get(method, {}).get("responses", {}).get("422")
        examples = (
            dereference(spec, response or {}).get("content", {}).get("application/json", {}).get("examples", {})
        )
        external = examples.get("ruleViolation", {}).get("externalValue")
        checks.require(
            external == "examples/error-rule-violation.json",
            f"{method.upper()} {path}: 422 must carry the ruleViolation example",
        )
    example = EXAMPLES / "error-rule-violation.json"
    if example.is_file():
        error = load_json(example).get("error", {})
        checks.require(error.get("code") == "rule_violation", "rule violation example must use code rule_violation")
        details = error.get("details", [])
        checks.require(
            bool(details)
            and all(re.fullmatch(r"[a-z][a-z0-9_]*", d.get("field", "")) for d in details),
            "rule violation example details must list field = snake_case rule code",
        )


def fixture_examples():
    plan = load_json(EXAMPLES / "plan.json")
    shopping = load_json(EXAMPLES / "shopping-list.json")
    summaries = load_json(EXAMPLES / "plans.json")["items"]
    empty = load_json(EXAMPLES / "plans-empty.json")
    return plan, shopping, summaries, empty


def canonical_snapshot(plan, shopping):
    return canonical_snapshot_id(plan, shopping)


def semantic_errors(
    plan: dict[str, Any],
    shopping: dict[str, Any],
    pantry: set[str] | None,
    summaries: list[dict[str, Any]],
    verify_canonical: bool = True,
) -> list[str]:
    c = Checks()
    summary_fields = (
        "plan_id",
        "week_start",
        "week_end",
        "timezone",
        "revision",
        "snapshot_id",
        "generated_at",
        "seed",
    )
    matching_summaries = [
        item for item in summaries if item.get("plan_id") == plan.get("plan_id")
    ]
    c.require(
        len(matching_summaries) == 1, "exactly one plan summary must match plan_id"
    )
    if matching_summaries:
        c.require(
            {k: matching_summaries[0].get(k) for k in summary_fields}
            == {k: plan.get(k) for k in summary_fields},
            "plan summary metadata must exactly match plan metadata",
        )
    for field in ("plan_id", "revision", "snapshot_id", "week_start", "week_end"):
        c.require(
            plan.get(field) == shopping.get(field),
            f"plan and shopping list {field} must match",
        )
    try:
        start = date.fromisoformat(plan["week_start"])
        end = date.fromisoformat(plan["week_end"])
        c.require(start.weekday() == 0, "week_start must be Monday")
        c.require(
            end == start + timedelta(days=6),
            "week_end must be the inclusive Sunday six days after week_start",
        )
    except (KeyError, ValueError, TypeError):
        c.errors.append("plan week_start/week_end must be valid ISO calendar dates")
        start = date(2000, 1, 3)
    try:
        ZoneInfo(plan["timezone"])
    except (KeyError, ZoneInfoNotFoundError, TypeError):
        c.errors.append("plan timezone must be a valid IANA timezone")
    slots = plan.get("slots", [])
    ids, keys, order = set(), set(), []
    proteins, carbs = Counter(), Counter()
    assignment_totals = defaultdict(float)
    by_id = {}
    for i, slot in enumerate(slots):
        aid = slot.get("assignment_id")
        c.require(aid not in ids, f"slots[{i}]: assignment_id must be unique")
        ids.add(aid)
        by_id[aid] = slot
        try:
            d = date.fromisoformat(slot["date"])
            c.require(
                start <= d <= start + timedelta(days=6),
                f"slots[{i}]: date must fall within plan week",
            )
            c.require(
                slot.get("day") == DAYS[d.weekday()],
                f"slots[{i}]: day must agree with date",
            )
            key = (d.isoformat(), slot.get("meal"))
            c.require(key not in keys, f"slots[{i}]: date/meal must be unique")
            keys.add(key)
            order.append((d.isoformat(), 0 if slot.get("meal") == "lunch" else 1))
        except (ValueError, KeyError, TypeError):
            c.errors.append(f"slots[{i}]: invalid date")
        protein = slot.get("protein")
        proteins[protein] += 1
        carb = slot.get("carb")
        carb_qty = slot.get("carb_quantity")
        c.require(
            (carb is None) == (carb_qty is None),
            f"slots[{i}]: carb and carb_quantity must both be null or non-null",
        )
        if carb is not None:
            carbs[carb] += 1
        ingredients = slot.get("ingredients", [])
        p_rows = [x for x in ingredients if x.get("role") == "protein"]
        pq = slot.get("protein_quantity") or {}
        c.require(
            len(p_rows) == 1
            and p_rows[0].get("quantity") == pq.get("value")
            and p_rows[0].get("unit") == pq.get("unit"),
            f"slots[{i}]: protein_quantity must match its protein ingredient row",
        )
        carb_rows = (
            [
                x
                for x in ingredients
                if x.get("ingredient_id") == carb and x.get("role") == "carb"
            ]
            if carb is not None
            else []
        )
        if carb is not None:
            cq = carb_qty or {}
            c.require(
                len(carb_rows) == 1
                and carb_rows[0].get("quantity") == cq.get("value")
                and carb_rows[0].get("unit") == cq.get("unit"),
                f"slots[{i}]: carb_quantity must match its carb ingredient row",
            )
        for item in ingredients:
            if pantry is None or item.get("ingredient_id") not in pantry:
                assignment_totals[(item.get("ingredient_id"), item.get("unit"))] += (
                    item.get("quantity", 0)
                )
        if slot.get("extended_from_lunch"):
            c.require(
                slot.get("meal") == "dinner",
                f"slots[{i}]: only a dinner may extend lunch",
            )
    c.require(
        order == sorted(order), "slots must be ordered by date, lunch before dinner"
    )
    derived = plan.get("derived", {})
    c.require(
        derived.get("protein_counts")
        == {k: proteins[k] for k in ("chicken", "beef", "pork", "fish", "egg")},
        "derived protein_counts must match slots",
    )
    c.require(
        derived.get("carb_counts", {}) == dict(carbs),
        "derived carb_counts must match slots",
    )
    for slot in slots:
        linked = slot.get("linked_assignment_id")
        if slot.get("meal") == "dinner" and linked is not None:
            c.require(
                slot.get("extended_from_lunch") is True,
                f"{slot.get('assignment_id')}: linked dinner must have extended_from_lunch true",
            )
        if linked is None:
            c.require(
                not slot.get("extended_from_lunch"),
                f"{slot.get('assignment_id')}: extended dinner must link to lunch",
            )
            continue
        other = by_id.get(linked)
        c.require(
            other is not None,
            f"{slot.get('assignment_id')}: linked assignment must exist",
        )
        if other:
            c.require(
                other.get("linked_assignment_id") == slot.get("assignment_id"),
                f"{slot.get('assignment_id')}: links must be reciprocal",
            )
            c.require(
                other.get("date") == slot.get("date")
                and other.get("meal") != slot.get("meal"),
                f"{slot.get('assignment_id')}: link must be same-day opposite meal",
            )
            c.require(
                other.get("recipe_id") == slot.get("recipe_id"),
                f"{slot.get('assignment_id')}: linked assignments must share base recipe",
            )
    shop_totals = defaultdict(float)
    shop_keys = set()
    shop_units = defaultdict(set)
    for section_name, section_lines in shopping.get("sections", {}).items():
        for line in section_lines:
            c.require(
                line.get("section") == section_name,
                f"shopping line {line.get('ingredient_id')} section must match its containing section",
            )
            key = (line.get("ingredient_id"), line.get("unit"))
            c.require(
                key not in shop_keys,
                f"shopping list must not repeat ingredient/unit line {key}",
            )
            shop_keys.add(key)
            shop_units[line.get("ingredient_id")].add(line.get("unit"))
            shop_totals[key] += line.get("quantity", 0)
    assignment_units = defaultdict(set)
    for slot in slots:
        for item in slot.get("ingredients", []):
            assignment_units[item.get("ingredient_id")].add(item.get("unit"))
    for ingredient_id, units in assignment_units.items():
        c.require(
            len(units) == 1,
            f"assignment ingredient {ingredient_id} has conflicting units",
        )
    for ingredient_id, units in shop_units.items():
        c.require(
            len(units) == 1,
            f"shopping ingredient {ingredient_id} has conflicting units",
        )
        c.require(
            units == assignment_units.get(ingredient_id, set()),
            f"shopping ingredient {ingredient_id} unit must match assignments",
        )
    if pantry is None:
        c.require(
            set(shop_totals).issubset(set(assignment_totals)),
            "shopping lines must refer to ingredients in plan assignments",
        )
        for key, quantity in shop_totals.items():
            c.require(
                quantity == round(assignment_totals.get(key, 0), 2),
                f"shopping quantity for {key} must equal included assignment total, rounded to 2 decimals",
            )
    else:
        c.require(
            set(shop_totals) == set(assignment_totals),
            "shopping lines must aggregate all non-pantry assignment ingredient IDs and units",
        )
        for key, quantity in assignment_totals.items():
            c.require(
                shop_totals.get(key) == round(quantity, 2),
                f"shopping quantity for {key} must equal assignment total excluding pantry, rounded to 2 decimals",
            )
    if verify_canonical:
        c.require(
            plan.get("snapshot_id") == canonical_snapshot(plan, shopping),
            "snapshot_id must match canonical SHA-256 plan/shopping payload",
        )
    if pantry is not None:
        try:
            validate_snapshot(plan, shopping, pantry)
        except SnapshotValidationError as exc:
            c.errors.append(f"shared snapshot validation: {exc}")
    return c.errors


def check_fixture_normalization(plan, shopping, checks):
    source = load_json(PLAN_FIXTURE)
    shop_source = load_json(SHOP_FIXTURE)
    start = date.fromisoformat(plan["week_start"])
    expected_slots = []
    for source_slot in source["slots"]:
        d = start + timedelta(days=DAYS.index(source_slot["day"]))
        aid = f"slot_{d.isoformat()}_{source_slot['meal']}"
        extended = source_slot["extendedFromLunch"]
        linked = (
            f"slot_{d.isoformat()}_{'lunch' if source_slot['meal'] == 'dinner' else 'dinner'}"
            if extended
            else None
        )
        if source_slot["meal"] == "lunch" and any(
            x["day"] == source_slot["day"]
            and x["meal"] == "dinner"
            and x["extendedFromLunch"]
            for x in source["slots"]
        ):
            linked = f"slot_{d.isoformat()}_dinner"
        ingredient_rows = [
            {
                "ingredient_id": x["item"],
                "display": x["display"],
                "quantity": x["qty"],
                "unit": x["unit"],
                "role": x["role"],
            }
            for x in source_slot["ingredients"]
        ]
        protein_rows = [x for x in ingredient_rows if x["role"] == "protein"]
        carb_id = None if source_slot["carb"] == "none" else source_slot["carb"]
        carb_rows = (
            [
                x
                for x in ingredient_rows
                if x["ingredient_id"] == carb_id and x["role"] == "carb"
            ]
            if carb_id is not None
            else []
        )
        expected_slots.append(
            {
                "assignment_id": aid,
                "date": d.isoformat(),
                "day": source_slot["day"],
                "meal": source_slot["meal"],
                "recipe_id": source_slot["recipeId"],
                "recipe_name": source_slot["recipeName"],
                "variant_id": source_slot["variantId"],
                "protein": source_slot["protein"],
                "protein_quantity": {
                    "value": source_slot["proteinQty"],
                    "unit": protein_rows[0]["unit"],
                },
                "carb": carb_id,
                "carb_quantity": None
                if source_slot["carbQty"] is None
                else {"value": source_slot["carbQty"], "unit": carb_rows[0]["unit"]},
                "ingredients": ingredient_rows,
                "extended_from_lunch": extended,
                "linked_assignment_id": linked,
            }
        )
    expected_slots.sort(key=lambda x: (x["date"], 0 if x["meal"] == "lunch" else 1))
    checks.require(
        plan.get("slots") == expected_slots,
        "plan slots must match normalized site/public/plan.json fixture",
    )
    checks.require(
        plan.get("seed") == source["seed"]
        and plan.get("generated_at") == source["generated_at"]
        and plan.get("derived") == source["derived"],
        "plan provenance and derived counts must match source fixture",
    )
    expected_sections = {
        section: [
            {
                "ingredient_id": x["item"],
                "display": x["display"],
                "quantity": x["quantity"],
                "unit": x["unit"],
                "section": x["section"],
                "notes": x["notes"],
            }
            for x in values
        ]
        for section, values in shop_source["sections"].items()
    }
    checks.require(
        shopping.get("sections") == expected_sections,
        "shopping sections and quantities must match normalized site/public/shopping_list.json fixture",
    )


def check_proposal_example(pantry):
    """The proposal example's candidate plan/shopping list must be a valid snapshot."""
    proposal = load_json(EXAMPLES / "proposal.json")
    plan, shopping = proposal["plan"], proposal["shopping_list"]
    summary_keys = (
        "plan_id", "week_start", "week_end", "timezone", "revision", "snapshot_id",
        "generated_at", "seed",
    )
    errors = semantic_errors(plan, shopping, pantry, [{k: plan[k] for k in summary_keys}])
    if proposal["plan_id"] != plan["plan_id"] or proposal["base_revision"] + 1 != plan["revision"]:
        errors.append("proposal example ids/revisions must be consistent")
    if proposal["identical"] != (not proposal["changes"]):
        errors.append("proposal example identical must equal 'no changes'")
    return [f"proposal.json: {e}" for e in errors]


def self_test_contract(spec):
    """Mutate the contract and prove the structural checks reject each mutation."""
    mutations = {
        "unreviewed path": lambda d: d["paths"].__setitem__(
            "/api/v1/admin", {"get": {"operationId": "x", "responses": {}}}
        ),
        "unreviewed method": lambda d: d["paths"]["/api/v1/plans/{plan_id}"].__setitem__(
            "delete", {"operationId": "deletePlan", "responses": {}}
        ),
        "missing Idempotency-Key": lambda d: d["paths"][
            "/api/v1/plans/{plan_id}/undo"
        ]["post"].__setitem__("parameters", [{"$ref": "#/components/parameters/PlanId"}]),
        "missing swap Idempotency-Key": lambda d: d["paths"][
            "/api/v1/plans/{plan_id}/swaps"
        ]["post"].__setitem__("parameters", [{"$ref": "#/components/parameters/PlanId"}]),
        "open swap body": lambda d: d["components"]["schemas"]["SwapRequest"].__setitem__(
            "additionalProperties", True
        ),
        "open request body": lambda d: d["components"]["schemas"]["CreatePlanRequest"].__setitem__(
            "additionalProperties", True
        ),
        "cacheable response": lambda d: d["components"]["responses"]["WriteForbidden"][
            "headers"
        ]["Cache-Control"]["schema"].__setitem__("const", "public"),
        "undocumented rule_violation code": lambda d: d["components"]["schemas"][
            "ErrorResponse"
        ]["properties"]["error"]["properties"]["code"]["enum"].remove("rule_violation"),
        "missing rule_violation example": lambda d: d["paths"]["/api/v1/plans"]["post"][
            "responses"
        ]["422"]["content"]["application/json"]["examples"].pop("ruleViolation"),
        "open fill body": lambda d: d["components"]["schemas"]["FillMonthRequest"].__setitem__(
            "additionalProperties", True
        ),
        "missing fill 403": lambda d: d["paths"]["/api/v1/months/{month}/fill"]["post"][
            "responses"
        ].pop("403"),
        "missing trip delete 403": lambda d: d["paths"]["/api/v1/shopping-trips/{trip_id}"][
            "delete"
        ]["responses"].pop("403"),
        "open trip done body": lambda d: d["components"]["schemas"][
            "ExpectedVersionRequest"
        ].__setitem__("additionalProperties", True),
        "missing 403": lambda d: d["paths"]["/api/v1/plans/{plan_id}/undo"]["post"][
            "responses"
        ].pop("403"),
    }
    for name, mutate in mutations.items():
        mutated = copy.deepcopy(spec)
        mutate(mutated)
        probe = Checks()
        try:
            check_contract(mutated, probe)
        except Exception:  # noqa: BLE001 - a crash also counts as rejection
            continue
        if not probe.errors:
            raise ValueError(f"contract self-test failed to reject mutation: {name}")
    return len(mutations)


def self_test(plan, shopping, pantry, summaries, spec):
    cases = []
    changed = copy.deepcopy(plan)
    changed["slots"][0]["day"] = "sun"
    cases.append((changed, copy.deepcopy(shopping), "day must agree with date"))
    changed = copy.deepcopy(plan)
    changed["slots"][1]["linked_assignment_id"] = None
    cases.append(
        (changed, copy.deepcopy(shopping), "extended dinner must link to lunch")
    )
    changed = copy.deepcopy(shopping)
    changed["revision"] += 1
    cases.append(
        (copy.deepcopy(plan), changed, "plan and shopping list revision must match")
    )
    changed = copy.deepcopy(shopping)
    next(iter(changed["sections"].values()))[0]["quantity"] += 1
    cases.append((copy.deepcopy(plan), changed, "shopping quantity for"))
    for p, s, expected in cases:
        errors = semantic_errors(p, s, pantry, summaries)
        if not any(expected in error for error in errors):
            raise ValueError(
                f"self-test failed to detect targeted diagnostic {expected!r}; got {errors}"
            )
    plan_response = dereference(
        spec, spec["paths"]["/api/v1/plans/{plan_id}"]["get"]["responses"]["200"]
    )
    plan_schema = plan_response["content"]["application/json"]["schema"]
    invalid_date = copy.deepcopy(plan)
    invalid_date["slots"][0]["date"] = "2026-13-45"
    validator = Draft202012Validator(
        plan_schema,
        resolver=RefResolver.from_schema(spec),
        format_checker=FormatChecker(),
    )
    if not any(
        error.validator == "format" for error in validator.iter_errors(invalid_date)
    ):
        raise ValueError(
            "self-test failed to reject invalid date format through the response schema"
        )
    contract_cases = self_test_contract(spec)
    print(
        f"self-test passed ({len(cases)} targeted semantic mutations, invalid date format, "
        f"and {contract_cases} contract mutations rejected)"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="also mutate examples and prove semantic checks reject them",
    )
    parser.add_argument(
        "--require-openapi-validator",
        action="store_true",
        help="fail unless full OpenAPI validation runs via the validator package or --openapi-schema",
    )
    parser.add_argument(
        "--openapi-schema",
        type=Path,
        help="validate the complete document against a supplied OpenAPI 3.1 JSON Schema meta-schema",
    )
    args = parser.parse_args()
    checks = Checks()
    try:
        spec = yaml.load(
            CONTRACT.read_text(encoding="utf-8"), Loader=DuplicateKeyLoader
        )
        check_contract(spec, checks)
        plan, shopping, summaries, empty = fixture_examples()
        checks.require(
            len(summaries) == 1,
            "plans.json must contain exactly one sample plan summary",
        )
        checks.require(empty == {"items": []}, "plans-empty.json must be {items: []}")
        pantry = set(yaml.safe_load(PANTRY_FILE.read_text(encoding="utf-8")) or [])
        checks.errors.extend(semantic_errors(plan, shopping, pantry, summaries))
        checks.errors.extend(check_proposal_example(pantry))
        check_fixture_normalization(plan, shopping, checks)
        full_validation_passed = False
        if args.openapi_schema:
            schema_path = args.openapi_schema.resolve()
            oas_schema = yaml.load(
                schema_path.read_text(encoding="utf-8"), Loader=DuplicateKeyLoader
            )
            Draft202012Validator.check_schema(oas_schema)
            oas_validator = Draft202012Validator(
                oas_schema,
                resolver=RefResolver.from_schema(
                    oas_schema, handlers={"http": fail_remote, "https": fail_remote}
                ),
                format_checker=FormatChecker(),
            )
            schema_errors = list(oas_validator.iter_errors(spec))
            for error in schema_errors:
                checks.errors.append(
                    f"OpenAPI document structure: {error.message} at {list(error.absolute_path)}"
                )
            if not schema_errors:
                full_validation_passed = True
                print(
                    "OpenAPI document structure validation passed against supplied 3.1 meta-schema"
                )
        validator = None
        try:
            from openapi_spec_validator import validate as validator
        except ImportError:
            message = "openapi-spec-validator is unavailable; use --openapi-schema for complete document structure validation"
            if not full_validation_passed:
                print(f"WARNING: {message}", file=sys.stderr)
        if validator:
            validator(spec)
            full_validation_passed = True
            print(
                "OpenAPI document structure validation passed with openapi-spec-validator"
            )
        if args.require_openapi_validator and not full_validation_passed:
            checks.errors.append(
                "full OpenAPI validation required; install openapi-spec-validator or supply --openapi-schema"
            )
        checks.finish()
        if args.self_test:
            self_test(plan, shopping, pantry, summaries, spec)
        print(
            "API contract validation passed (local refs, schemas, examples, fixture semantics)"
        )
    except Exception as e:  # noqa: BLE001
        print(f"API contract validation failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
