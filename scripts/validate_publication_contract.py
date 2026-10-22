#!/usr/bin/env python3
"""Validate public static publication schemas, fixtures, and bundle references."""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import shutil
import sys
import tempfile
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jsonschema
import yaml
from jsonschema import Draft202012Validator, FormatChecker, RefResolver

import validate_api_contract as api
from mealplanner.domain import shopping_trip as trip_domain

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs/migration-to-cloud"
OPENAPI_PATH = DOCS / "openapi.yaml"
PUBLICATION_SCHEMA_PATH = DOCS / "publication.schema.json"
FRONTEND_SCHEMA_PATH = DOCS / "frontend-config.schema.json"
STATIC_EXAMPLES = DOCS / "static-examples"
DEFAULT_BUNDLES = (STATIC_EXAMPLES / "populated", STATIC_EXAMPLES / "empty")
FRONTEND_CONFIGS = (STATIC_EXAMPLES / "frontend-api.json", STATIC_EXAMPLES / "frontend-static.json")
# A bundle directory is the deployed `publication/` directory itself. The
# frontend-config path `publication/index.json` is relative to the site root.
INDEX_RELATIVE = PurePosixPath("index.json")
DATE_MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


def load_json(path: Path) -> Any:
    """Load JSON while rejecting duplicate object keys."""

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            reject_constant(value)
        return number

    with path.open(encoding="utf-8") as handle:
        return json.load(
            handle,
            object_pairs_hook=unique_pairs,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )


def fail_remote(uri: str) -> Any:
    raise ValueError(f"external schema reference is not allowed: {uri}")


def schema_resolver(schema_path: Path, schema: dict[str, Any], openapi: dict[str, Any]) -> RefResolver:
    """Resolve the one approved external schema reference without network access."""
    store = {OPENAPI_PATH.resolve().as_uri(): openapi}
    return RefResolver(
        base_uri=schema_path.resolve().as_uri(),
        referrer=schema,
        store=store,
        handlers={"http": fail_remote, "https": fail_remote},
    )


def validate_schema_documents(
    checks: api.Checks,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], RefResolver]:
    """Load/check schemas and validate the frontend mode examples."""
    openapi = yaml.load(OPENAPI_PATH.read_text(encoding="utf-8"), Loader=api.DuplicateKeyLoader)
    publication_schema = load_json(PUBLICATION_SCHEMA_PATH)
    frontend_schema = load_json(FRONTEND_SCHEMA_PATH)
    for label, schema in (("publication", publication_schema), ("frontend config", frontend_schema)):
        try:
            Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as error:
            checks.errors.append(f"{label} schema is invalid: {error.message}")

    resolver = schema_resolver(PUBLICATION_SCHEMA_PATH, publication_schema, openapi)
    frontend_validator = Draft202012Validator(frontend_schema, format_checker=FormatChecker())
    for config_path in FRONTEND_CONFIGS:
        try:
            instance = load_json(config_path)
        except Exception as error:
            checks.errors.append(f"cannot load frontend config {config_path.name}: {error}")
            continue
        for error in frontend_validator.iter_errors(instance):
            checks.errors.append(
                f"{config_path.name}: frontend config violates schema: {error.message} at {list(error.absolute_path)}"
            )
    return openapi, publication_schema, frontend_schema, resolver


def schema_errors(
    instance: Any,
    schema: dict[str, Any],
    resolver: RefResolver | None,
    label: str,
) -> list[str]:
    validator = Draft202012Validator(schema, resolver=resolver, format_checker=FormatChecker())
    return [
        f"{label}: {error.message} at {list(error.absolute_path)}"
        for error in validator.iter_errors(instance)
    ]


def safe_relative_path(raw: Any, base: Path, label: str) -> tuple[Path | None, str | None]:
    """Resolve a strictly relative, unencoded POSIX path under its containing directory."""
    if not isinstance(raw, str) or not raw:
        return None, f"{label}: path must be a non-empty relative string"
    if any(char in raw for char in ("\\", "%", "?", "#")):
        return None, f"{label}: unsafe relative path contains a forbidden character"
    if "://" in raw or raw.startswith("/"):
        return None, f"{label}: unsafe relative path must not be absolute or external"
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None, f"{label}: unsafe relative path contains traversal or empty segments"
    parsed = PurePosixPath(raw)
    if parsed.is_absolute() or parsed.as_posix() != raw:
        return None, f"{label}: unsafe relative path is not a normalized POSIX path"
    root = base.resolve()
    candidate = base.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, f"{label}: missing bundle file {raw}"
    if not resolved.is_relative_to(root):
        return None, f"{label}: unsafe relative path escapes its containing directory"
    if not resolved.is_file():
        return None, f"{label}: referenced path is not a file: {raw}"
    return resolved, None


def check_bundle_tree(bundle_root: Path, expected_files: set[Path], checks: api.Checks) -> None:
    """Reject symlinks and any artifact not referenced by the active bundle."""
    found: set[Path] = set()
    for current, dirnames, filenames in os.walk(bundle_root, followlinks=False):
        current_path = Path(current)
        for name in list(dirnames):
            entry = current_path / name
            if entry.is_symlink():
                checks.errors.append(f"bundle symlink is forbidden: {entry.relative_to(bundle_root)}")
                dirnames.remove(name)
        for name in filenames:
            entry = current_path / name
            relative = entry.relative_to(bundle_root)
            found.add(relative)
            checks.require(not entry.is_symlink(), f"bundle symlink is forbidden: {relative}")
    for path in sorted(expected_files - found):
        checks.errors.append(f"missing bundle file: {path.as_posix()}")
    for path in sorted(found - expected_files):
        checks.errors.append(f"unexpected public bundle file: {path.as_posix()}")


def month_bounds(month: str) -> tuple[date, date] | None:
    if not isinstance(month, str) or not DATE_MONTH.fullmatch(month):
        return None
    try:
        start = date.fromisoformat(f"{month}-01")
    except ValueError:
        return None
    return start, date(start.year, start.month, monthrange(start.year, start.month)[1])


def canonical_metadata(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        key: plan.get(key)
        for key in ("plan_id", "week_start", "week_end", "timezone", "revision", "snapshot_id", "generated_at", "seed")
    }


def validate_trips(
    manifest: dict[str, Any],
    manifest_path: Path,
    bundle_root: Path,
    published_plans: dict[str, dict[str, Any]],
    shopping_by_plan: dict[str, dict[str, Any]],
    trip_schema: dict[str, Any],
    expected_files: set[Path],
    checks: api.Checks,
) -> None:
    """Check manifest.shopping_trips entries, their files, and coherence with the published plans."""
    entries = manifest.get("shopping_trips", [])
    if not isinstance(entries, list):
        checks.errors.append("manifest shopping_trips must be an array")
        return
    trip_ids = [e.get("trip_id") for e in entries if isinstance(e, dict) and isinstance(e.get("trip_id"), str)]
    checks.require(trip_ids == sorted(set(trip_ids)), "manifest shopping_trips must be sorted by trip_id and unique")
    plan_owner: dict[tuple[str, str], str] = {}  # (plan_id, group) -> trip_id
    labels: dict[str, str] = {}  # ingredient_id -> shelf_life as the bundle's trip items show it
    item_checks: list[tuple[str, dict[str, Any]]] = []
    for position, entry in enumerate(entries):
        label = f"shopping_trips[{position}]"
        if not isinstance(entry, dict):
            continue
        trip_id = entry.get("trip_id")
        raw_path = entry.get("path")
        checks.require(raw_path == f"shopping-trips/{trip_id}.json", f"manifest {label}: path must be shopping-trips/<trip_id>.json")
        path, path_error = safe_relative_path(raw_path, manifest_path.parent, f"{label} path")
        if path_error:
            checks.errors.append(path_error)
        if path is None:
            continue
        expected_files.add(path.relative_to(bundle_root))
        try:
            trip = load_json(path)
        except Exception as error:
            checks.errors.append(f"manifest {label}: referenced trip is invalid JSON: {error}")
            continue
        file_label = str(path.relative_to(bundle_root))
        trip_errors = schema_errors(trip, trip_schema, None, file_label)
        checks.errors.extend(trip_errors)
        if trip_errors or not isinstance(trip, dict):
            continue
        checks.require(trip["trip_id"] == trip_id, f"{file_label}: trip_id must match its manifest entry")
        states = [c["state"] for w in trip["weeks"] for c in w["cells"]]
        is_done = bool(states) and all(st == "bought" for st in states)
        checks.require(
            (trip["status"] == "done") == is_done,
            f"{file_label}: status must be done exactly when every cell is bought",
        )
        checks.require(
            (trip["completed_at"] is not None) == is_done,
            f"{file_label}: completed_at must be set exactly when the trip is done",
        )
        weeks = trip["weeks"]
        checks.require(
            [w["week_start"] for w in weeks] == sorted(w["week_start"] for w in weeks),
            f"{file_label}: weeks must be ordered by week_start",
        )
        week_ids = [w["plan_id"] for w in weeks]
        checks.require(len(set(week_ids)) == len(week_ids), f"{file_label}: week plan_ids must be unique")
        for item in [*trip["items"], *trip["bought_items"]]:
            known = labels.setdefault(item["ingredient_id"], item["shelf_life"])
            checks.require(
                known == item["shelf_life"],
                f"{file_label}: ingredient {item['ingredient_id']} has conflicting shelf_life values",
            )
        for week in weeks:
            plan_id = week["plan_id"]
            groups = week["groups"]
            checks.require(
                groups == [g for g in trip_domain.GROUPS if g in groups] and len(set(groups)) == len(groups),
                f"{file_label}: week {plan_id} groups must be unique and ordered long_lasting, fresh",
            )
            for group in groups:
                other = plan_owner.setdefault((plan_id, group), trip["trip_id"])
                checks.require(
                    other == trip["trip_id"],
                    f"{file_label}: plan {plan_id} ({group}) is already on trip {other}",
                )
            summary = published_plans.get(plan_id)
            if summary is None:
                checks.errors.append(f"{file_label}: week {plan_id} has no entry in manifest plans")
                continue
            checks.require(
                (week["week_start"], week["week_end"]) == (summary["week_start"], summary["week_end"]),
                f"{file_label}: week {plan_id} dates must match the published plan",
            )
            checks.require(
                week["current_revision"] == summary["revision"],
                f"{file_label}: week {plan_id} current_revision must be the published plan revision",
            )
            checks.require(
                [c["group"] for c in week["cells"]] == groups,
                f"{file_label}: week {plan_id} cells must match its groups, in order",
            )
            for cell in week["cells"]:
                where = f"{file_label}: cell {plan_id} ({cell['group']})"
                frozen = (cell["bought_at"], cell["revision"], cell["snapshot_id"])
                if cell["state"] == "to_buy":
                    checks.require(
                        all(v is None for v in frozen) and not cell["changed_since_shopping"],
                        f"{where}: a to-buy cell has no bought_at, revision or snapshot_id",
                    )
                    continue
                checks.require(
                    all(v is not None for v in frozen),
                    f"{where}: a bought cell needs bought_at, revision and snapshot_id",
                )
                if cell["revision"] is None:
                    continue
                checks.require(
                    cell["revision"] <= summary["revision"],
                    f"{where}: revision must not exceed the published plan revision",
                )
                checks.require(
                    cell["changed_since_shopping"] == (summary["revision"] > cell["revision"]),
                    f"{where}: changed_since_shopping must say whether the plan moved past the bought revision",
                )
                if cell["revision"] == summary["revision"]:
                    checks.require(
                        cell["snapshot_id"] == summary["snapshot_id"],
                        f"{where}: snapshot_id must match the published plan at the same revision",
                    )
        # To-buy items always come from the published (current) shopping lists; bought items are
        # checkable only when every bought cell sits at the published revision.
        if all(w["plan_id"] in shopping_by_plan for w in weeks):
            item_checks.append((file_label, trip))
    # Item sums need every trip's labels, so they run after all trips are read. A row whose
    # ingredient no trip item labels belongs to a group no list buys and is skipped.
    for file_label, trip in item_checks:
        expected_items = trip_domain.sum_items(
            trip_domain.merge_cells(
                (w["plan_id"], None, shopping_by_plan[w["plan_id"]], c["group"])
                for w in trip["weeks"]
                for c in w["cells"]
                if c["state"] == "to_buy"
            ),
            labels,
            skip_unlabeled=True,
        )
        checks.require(
            trip["items"] == expected_items,
            f"{file_label}: items must equal the sum of the to-buy cells' rows of the weeks' shopping lists",
        )
        bought = [(w, c) for w in trip["weeks"] for c in w["cells"] if c["state"] == "bought"]
        if all(c["revision"] == shopping_by_plan[w["plan_id"]].get("revision") for w, c in bought):
            expected_bought = trip_domain.sum_items(
                trip_domain.merge_cells(
                    (w["plan_id"], c["revision"], shopping_by_plan[w["plan_id"]], c["group"])
                    for w, c in bought
                ),
                labels,
                skip_unlabeled=True,
            )
            checks.require(
                trip["bought_items"] == expected_bought,
                f"{file_label}: bought_items must equal the sum of the bought cells' rows of the weeks' shopping lists",
            )


def validate_bundle(
    bundle_root: Path,
    openapi: dict[str, Any],
    publication_schema: dict[str, Any],
    resolver: RefResolver,
) -> list[str]:
    """Validate one fixture or supplied immutable public artifact directory."""
    checks = api.Checks()
    if bundle_root.is_symlink():
        return [f"{bundle_root}: bundle root symlink is forbidden"]
    bundle_root = bundle_root.resolve()
    index_path = bundle_root / INDEX_RELATIVE
    if index_path.is_symlink():
        return [f"{INDEX_RELATIVE.as_posix()}: symlink is forbidden"]
    if not index_path.is_file():
        return [f"{bundle_root}: missing bundle file: {INDEX_RELATIVE.as_posix()}"]
    try:
        index = load_json(index_path)
    except Exception as error:
        return [f"{INDEX_RELATIVE.as_posix()}: cannot parse JSON: {error}"]
    index_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": publication_schema.get("$defs", {}),
        "$ref": "#/$defs/Index",
    }
    manifest_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": publication_schema.get("$defs", {}),
        "$ref": "#/$defs/Manifest",
    }
    checks.errors.extend(schema_errors(index, index_schema, resolver, INDEX_RELATIVE.as_posix()))
    if not isinstance(index, dict) or "publication_id" not in index:
        return checks.errors

    publication_id = index.get("publication_id")
    manifest_path_raw = index.get("manifest_path")
    expected_manifest_path = f"publications/{publication_id}/manifest.json"
    checks.require(manifest_path_raw == expected_manifest_path, "index manifest_path must identify its publication_id directory")
    manifest_path, path_error = safe_relative_path(manifest_path_raw, index_path.parent, "index manifest_path")
    if path_error:
        checks.errors.append(path_error)
    if manifest_path is None:
        return checks.errors
    expected_files = {INDEX_RELATIVE, manifest_path.relative_to(bundle_root)}
    try:
        manifest = load_json(manifest_path)
    except Exception as error:
        checks.errors.append(f"manifest: cannot parse JSON: {error}")
        check_bundle_tree(bundle_root, expected_files, checks)
        return checks.errors

    checks.errors.extend(schema_errors(manifest, manifest_schema, resolver, "manifest"))
    if not isinstance(manifest, dict):
        check_bundle_tree(bundle_root, expected_files, checks)
        return checks.errors
    checks.require(manifest.get("publication_id") == publication_id, "manifest publication_id must match index")
    timezone_name = manifest.get("timezone")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        checks.errors.append("manifest timezone must be a valid IANA timezone")

    selected_months = manifest.get("selected_months", [])
    if not isinstance(selected_months, list):
        checks.errors.append("selected_months must be an array")
        selected_months = []
    if all(isinstance(month, str) for month in selected_months):
        checks.require(selected_months == sorted(set(selected_months)), "selected_months must be unique and sorted ascending")
    else:
        checks.require(False, "selected_months entries must be strings")
    bounds: list[tuple[date, date]] = []
    for month in selected_months:
        value = month_bounds(month)
        if value is None:
            checks.errors.append(f"selected_months entry is not a real YYYY-MM calendar month: {month!r}")
        else:
            bounds.append(value)

    plan_ids: set[str] = set()
    week_starts: set[str] = set()
    all_plan_paths: set[str] = set()
    summaries = manifest.get("plans", [])
    if not isinstance(summaries, list):
        checks.errors.append("manifest plans must be an array")
        summaries = []
    week_order: list[str] = []
    published_plans: dict[str, dict[str, Any]] = {}
    shopping_by_plan: dict[str, dict[str, Any]] = {}
    api_schemas = openapi.get("components", {}).get("schemas", {})
    summary_schema = api_schemas.get("PlanSummary", {})
    dto_root = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "components": {"schemas": api_schemas},
    }
    plan_schema = {**dto_root, "$ref": "#/components/schemas/Plan"}
    shopping_schema = {**dto_root, "$ref": "#/components/schemas/ShoppingList"}

    for index_in_manifest, entry in enumerate(summaries):
        if not isinstance(entry, dict):
            continue
        summary = entry.get("summary")
        if not isinstance(summary, dict):
            continue
        plan_id = summary.get("plan_id")
        week_start = summary.get("week_start")
        if isinstance(plan_id, str):
            published_plans[plan_id] = summary
            checks.require(plan_id not in plan_ids, f"manifest plans[{index_in_manifest}]: plan_id must be unique")
            plan_ids.add(plan_id)
        if isinstance(week_start, str):
            checks.require(week_start not in week_starts, f"manifest plans[{index_in_manifest}]: week_start must be unique")
            week_starts.add(week_start)
        if isinstance(week_start, str):
            week_order.append(week_start)
        for label, schema, value in (("summary", summary_schema, summary),):
            checks.errors.extend(schema_errors(value, schema, None, f"plans[{index_in_manifest}] {label}"))

        checks.require(summary.get("timezone") == timezone_name, f"manifest plans[{index_in_manifest}]: timezone must match manifest")
        try:
            start = date.fromisoformat(summary["week_start"])
            end = date.fromisoformat(summary["week_end"])
            valid_week = start.weekday() == 0 and end == start + timedelta(days=6)
            checks.require(valid_week, f"manifest plans[{index_in_manifest}]: week dates must be Monday through inclusive Sunday")
            if bounds:
                checks.require(
                    any(start <= month_end and end >= month_start for month_start, month_end in bounds),
                    f"manifest plans[{index_in_manifest}]: week does not overlap any selected month",
                )
            elif selected_months:
                checks.require(False, f"manifest plans[{index_in_manifest}]: cannot verify selected-month overlap")
        except (KeyError, ValueError, TypeError):
            checks.errors.append(f"manifest plans[{index_in_manifest}]: week_start/week_end must be real ISO dates")

        plan_path_raw = entry.get("plan_path")
        shopping_path_raw = entry.get("shopping_path")
        expected_plan = f"plans/{plan_id}/plan.json"
        expected_shopping = f"plans/{plan_id}/shopping-list.json"
        checks.require(plan_path_raw == expected_plan, f"manifest plans[{index_in_manifest}]: plan_path must match plan_id directory")
        checks.require(shopping_path_raw == expected_shopping, f"manifest plans[{index_in_manifest}]: shopping_path must match plan_id directory")
        for raw_path in (plan_path_raw, shopping_path_raw):
            if isinstance(raw_path, str):
                checks.require(raw_path not in all_plan_paths, f"manifest plans[{index_in_manifest}]: referenced paths must be unique")
                all_plan_paths.add(raw_path)
        plan_path, plan_path_error = safe_relative_path(plan_path_raw, manifest_path.parent, f"plans[{index_in_manifest}] plan_path")
        shop_path, shop_path_error = safe_relative_path(shopping_path_raw, manifest_path.parent, f"plans[{index_in_manifest}] shopping_path")
        if plan_path_error:
            checks.errors.append(plan_path_error)
        if shop_path_error:
            checks.errors.append(shop_path_error)
        for resolved in (plan_path, shop_path):
            if resolved is not None:
                expected_files.add(resolved.relative_to(bundle_root))
        if plan_path is None or shop_path is None:
            continue
        try:
            plan = load_json(plan_path)
            shopping = load_json(shop_path)
        except Exception as error:
            checks.errors.append(f"manifest plans[{index_in_manifest}]: referenced DTO is invalid JSON: {error}")
            continue
        if not isinstance(plan, dict) or not isinstance(shopping, dict):
            checks.errors.append(f"manifest plans[{index_in_manifest}]: plan and shopping DTOs must be JSON objects")
            continue
        plan_schema_errors = schema_errors(plan, plan_schema, None, f"{plan_path.relative_to(bundle_root)}")
        shopping_schema_errors = schema_errors(shopping, shopping_schema, None, f"{shop_path.relative_to(bundle_root)}")
        checks.errors.extend(plan_schema_errors)
        checks.errors.extend(shopping_schema_errors)
        if not shopping_schema_errors and isinstance(plan_id, str):
            shopping_by_plan[plan_id] = shopping
        checks.require(canonical_metadata(plan) == {key: summary.get(key) for key in canonical_metadata(plan)}, f"manifest plans[{index_in_manifest}]: summary must exactly match plan metadata")
        if plan.get("timezone") != timezone_name:
            checks.errors.append(f"{plan_path.relative_to(bundle_root)}: timezone must match manifest")
        verify_hash = isinstance(plan.get("snapshot_id"), str) and plan["snapshot_id"].startswith("sha256:")
        if not plan_schema_errors and not shopping_schema_errors:
            checks.errors.extend(api.semantic_errors(plan, shopping, pantry=None, summaries=[summary], verify_canonical=verify_hash))
    checks.require(week_order == sorted(week_order), "manifest plans must be sorted by week_start ascending")
    validate_trips(
        manifest, manifest_path, bundle_root, published_plans, shopping_by_plan,
        {**dto_root, "$ref": "#/components/schemas/ShoppingTrip"}, expected_files, checks,
    )
    check_bundle_tree(bundle_root, expected_files, checks)
    return checks.errors


def validate_fixtures(
    bundle_roots: tuple[Path, ...],
    openapi: dict[str, Any],
    publication_schema: dict[str, Any],
    resolver: RefResolver,
) -> list[str]:
    errors: list[str] = []
    for bundle_root in bundle_roots:
        if bundle_root.is_symlink():
            errors.append(f"bundle root symlink is forbidden: {bundle_root}")
            continue
        if not bundle_root.exists():
            try:
                shown_path = bundle_root.relative_to(ROOT)
            except ValueError:
                shown_path = bundle_root
            errors.append(f"missing publication fixture bundle directory: {shown_path}")
            continue
        errors.extend(validate_bundle(bundle_root, openapi, publication_schema, resolver))
    return errors


def mutate_bundle(bundle_root: Path, mutation: Callable[[Path], Any]) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="meal-pub-selftest-") as temp_name:
        copied = Path(temp_name) / "bundle"
        shutil.copytree(bundle_root, copied, symlinks=True)
        mutation(copied)
        openapi, publication_schema, _, resolver = validate_schema_documents(api.Checks())
        return validate_bundle(copied, openapi, publication_schema, resolver)


def mutate_json(path: Path, transform: Callable[[dict[str, Any]], Any]) -> None:
    value = load_json(path)
    transform(value)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def self_test(bundle_root: Path) -> None:
    """Run targeted mutations in temporary copies and require specific diagnostics."""
    openapi, publication_schema, frontend_schema, resolver = validate_schema_documents(api.Checks())
    index_rel = INDEX_RELATIVE
    index_path = bundle_root / index_rel
    index = load_json(index_path)
    manifest_path = bundle_root / index["manifest_path"]
    manifest = load_json(manifest_path)
    published = manifest.get("plans", [])
    if not published:
        raise ValueError("self-test needs the populated publication fixture")
    tests: list[tuple[str, Callable[[Path], Any], str]] = []
    tests.append(("traversal path", lambda root: mutate_json(root / index_rel, lambda x: x.update(manifest_path="publications/../escape/manifest.json")), "unsafe relative path"))
    tests.append(("external path", lambda root: mutate_json(root / index_rel, lambda x: x.update(manifest_path="https://example.invalid/manifest.json")), "unsafe relative path"))
    tests.append(("encoded path", lambda root: mutate_json(root / index_rel, lambda x: x.update(manifest_path="publications/a%2f..%2fb/manifest.json")), "unsafe relative path"))
    tests.append(("missing file", lambda root: (root / index["manifest_path"]).unlink(), "missing bundle file"))
    tests.append(("publication mismatch", lambda root: mutate_json(root / index["manifest_path"], lambda x: x.update(publication_id="other")), "manifest publication_id must match index"))
    tests.append(("revision mismatch", lambda root: mutate_json(root / index["manifest_path"].replace("manifest.json", published[0]["shopping_path"]), lambda x: x.update(revision=x["revision"] + 1)), "plan and shopping list revision must match"))

    def bad_hash(root: Path) -> None:
        mpath = root / index["manifest_path"]
        m = load_json(mpath)
        fake = "sha256:" + "0" * 64
        m["plans"][0]["summary"]["snapshot_id"] = fake
        mpath.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
        ppath = mpath.parent / m["plans"][0]["plan_path"]
        spath = mpath.parent / m["plans"][0]["shopping_path"]
        mutate_json(ppath, lambda x: x.update(snapshot_id=fake))
        mutate_json(spath, lambda x: x.update(snapshot_id=fake))

    tests.append(("snapshot hash mismatch", bad_hash, "snapshot_id must match canonical SHA-256"))

    def duplicate_week(root: Path) -> None:
        mpath = root / index["manifest_path"]
        mutate_json(mpath, lambda x: x["plans"].append(copy.deepcopy(x["plans"][0])))

    tests.append(("duplicate week", duplicate_week, "week_start must be unique"))

    def wrong_date(root: Path) -> None:
        mpath = root / index["manifest_path"]
        m = load_json(mpath)
        ppath = mpath.parent / m["plans"][0]["plan_path"]
        mutate_json(ppath, lambda x: x["slots"][0].update(date="2026-10-20"))

    tests.append(("wrong slot date", wrong_date, "day must agree with date"))
    tests.append(("unselected week", lambda root: mutate_json(root / index["manifest_path"], lambda x: x.update(selected_months=["2026-11"])), "week does not overlap any selected month"))
    tests.append(("invalid timezone", lambda root: mutate_json(root / index["manifest_path"], lambda x: x.update(timezone="Not/A_Timezone")), "manifest timezone must be a valid IANA timezone"))
    tests.append(("invalid month", lambda root: mutate_json(root / index["manifest_path"], lambda x: x.update(selected_months=["2026-13"])), "not a real YYYY-MM calendar month"))
    tests.append(("unexpected secret/db file", lambda root: (root / "orphaned" / "private.sqlite").parent.mkdir() or (root / "orphaned" / "private.sqlite").write_bytes(b"db"), "unexpected public bundle file"))
    tests.append(("bundle symlink", lambda root: (root / "external-link").symlink_to(root.parent / "outside.txt"), "bundle symlink is forbidden"))
    def symlink_index(root: Path) -> None:
        (root / index_rel).unlink()
        (root / index_rel).symlink_to(index_path)

    tests.append(("index symlink", symlink_index, "index.json: symlink is forbidden"))
    tests.append(("duplicate JSON key", lambda root: (root / index_rel).write_text('{"schema_version":1,"schema_version":1,"publication_id":"x","manifest_path":"publications/x/manifest.json"}\n', encoding="utf-8"), "duplicate JSON key"))

    trip_rel = manifest["shopping_trips"][0]["path"]

    def trip_file(root: Path) -> Path:
        return root / index["manifest_path"].replace("manifest.json", trip_rel)

    def mutate_trip(transform: Callable[[dict[str, Any]], Any]) -> Callable[[Path], Any]:
        return lambda root: mutate_json(trip_file(root), transform)

    def done_week_too_new(trip: dict[str, Any]) -> None:
        trip["weeks"][0]["cells"][0]["revision"] += 1
        trip["weeks"][0]["current_revision"] += 1

    def week_not_published(trip: dict[str, Any]) -> None:
        trip["weeks"][0]["plan_id"] = "plan_not_published"

    def orphan_trip_file(root: Path) -> None:
        shutil.copy(trip_file(root), trip_file(root).with_name("trip_orphan.json"))

    def unsorted_trips(root: Path) -> None:
        mpath = root / index["manifest_path"]
        mutate_json(mpath, lambda x: x["shopping_trips"].insert(0, {"trip_id": "trip_zzz", "path": "shopping-trips/trip_zzz.json"}))
        shutil.copy(trip_file(root), trip_file(root).with_name("trip_zzz.json"))
        mutate_json(trip_file(root).with_name("trip_zzz.json"), lambda x: x.update(trip_id="trip_zzz"))

    def first_bought(trip: dict[str, Any]) -> dict[str, Any]:
        return next(c for w in trip["weeks"] for c in w["cells"] if c["state"] == "bought")

    def first_to_buy(trip: dict[str, Any]) -> dict[str, Any]:
        return next(c for w in trip["weeks"] for c in w["cells"] if c["state"] == "to_buy")

    tests.append(("trip current_revision mismatch", mutate_trip(lambda t: t["weeks"][0].update(current_revision=t["weeks"][0]["current_revision"] + 1)), "current_revision must be the published plan revision"))
    tests.append(("bought cell snapshot mismatch", mutate_trip(lambda t: first_bought(t).update(snapshot_id="sha256:" + "1" * 64)), "snapshot_id must match the published plan"))
    tests.append(("bought cell revision above published", mutate_trip(done_week_too_new), "revision must not exceed the published plan revision"))
    tests.append(("bought cell changed flag wrong", mutate_trip(lambda t: first_bought(t).update(changed_since_shopping=True)), "changed_since_shopping must say"))
    tests.append(("to-buy cell with frozen revision", mutate_trip(lambda t: first_to_buy(t).update(revision=1)), "a to-buy cell has no bought_at"))
    tests.append(("bought cell without bought_at", mutate_trip(lambda t: first_bought(t).update(bought_at=None)), "a bought cell needs bought_at"))
    tests.append(("done status with a to-buy cell", mutate_trip(lambda t: t.update(status="done", completed_at="2026-10-22T10:00:00+00:00")), "status must be done exactly when every cell is bought"))
    tests.append(("completed_at while open", mutate_trip(lambda t: t.update(completed_at="2026-10-22T10:00:00+00:00")), "completed_at must be set exactly when the trip is done"))
    tests.append(("bought items not summed", mutate_trip(lambda t: t["bought_items"][0].update(quantity=t["bought_items"][0]["quantity"] + 1)), "bought_items must equal the sum"))
    tests.append(("trip week without published plan", mutate_trip(week_not_published), "has no entry in manifest plans"))
    tests.append(("trip id mismatch", mutate_trip(lambda t: t.update(trip_id="trip_other")), "trip_id must match its manifest entry"))
    tests.append(("trip items not summed", mutate_trip(lambda t: t["items"][0].update(quantity=t["items"][0]["quantity"] + 1)), "items must equal the sum"))
    tests.append(("unreferenced trip file", orphan_trip_file, "unexpected public bundle file"))
    tests.append(("trips not sorted", unsorted_trips, "sorted by trip_id"))
    tests.append(("trips missing from manifest", lambda root: mutate_json(root / index["manifest_path"], lambda x: x.pop("shopping_trips")), "'shopping_trips' is a required property"))

    def nonfinite_number(root: Path) -> None:
        mpath = root / index["manifest_path"]
        m = load_json(mpath)
        ppath = mpath.parent / m["plans"][0]["plan_path"]
        text = ppath.read_text(encoding="utf-8").replace('"value": 260.0', '"value": NaN', 1)
        ppath.write_text(text, encoding="utf-8")

    tests.append(("non-finite JSON number", nonfinite_number, "non-finite JSON number is not allowed"))

    for label, mutation, diagnostic in tests:
        errors = mutate_bundle(bundle_root, mutation)
        if not any(diagnostic in error for error in errors):
            raise ValueError(f"self-test {label!r} missed targeted diagnostic {diagnostic!r}; got {errors}")

    with tempfile.TemporaryDirectory(prefix="meal-pub-json-selftest-") as temp_name:
        for name, raw in (("nan.json", "NaN"), ("infinity.json", "Infinity"), ("overflow.json", "1e999")):
            path = Path(temp_name) / name
            path.write_text(f"{{\"value\":{raw}}}", encoding="utf-8")
            try:
                load_json(path)
            except ValueError as error:
                if "non-finite JSON number" not in str(error):
                    raise ValueError(f"self-test {name} failed for an unexpected reason: {error}")
            else:
                raise ValueError(f"self-test accepted non-finite JSON value in {name}")

    with tempfile.TemporaryDirectory(prefix="meal-pub-link-selftest-") as temp_name:
        linked_root = Path(temp_name) / "bundle-link"
        linked_root.symlink_to(bundle_root, target_is_directory=True)
        root_errors = validate_bundle(linked_root, openapi, publication_schema, resolver)
        if not any("bundle root symlink is forbidden" in error for error in root_errors):
            raise ValueError(f"self-test did not reject a symlink bundle root: {root_errors}")

    bad_schema = {"type": 5}
    try:
        Draft202012Validator.check_schema(bad_schema)
    except jsonschema.SchemaError:
        pass
    else:
        raise ValueError("self-test accepted a malformed JSON Schema")

    invalid_config = {"mode": "static", "publication_index_path": "publication/index.json", "api_base_url": "http://nas.local/api/v1"}
    config_errors = list(Draft202012Validator(frontend_schema).iter_errors(invalid_config))
    def nested_errors(errors: Iterable[jsonschema.ValidationError]) -> Iterator[jsonschema.ValidationError]:
        for error in errors:
            yield error
            yield from nested_errors(error.context)

    if not any(
        error.validator == "additionalProperties" and "api_base_url" in error.message
        for error in nested_errors(config_errors)
    ):
        raise ValueError("self-test did not reject a static config containing a NAS API URL")
    print(f"self-test passed ({len(tests)} targeted bundle mutations, duplicate/non-finite JSON, malformed schema, and static API URL rejected)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, help="validate one supplied bundle directory instead of checked-in fixtures")
    parser.add_argument("--self-test", action="store_true", help="run targeted temporary-copy mutations")
    args = parser.parse_args()
    checks = api.Checks()
    try:
        openapi, publication_schema, _, resolver = validate_schema_documents(checks)
        roots = (args.bundle,) if args.bundle else DEFAULT_BUNDLES
        checks.errors.extend(validate_fixtures(roots, openapi, publication_schema, resolver))
        checks.finish()
        if args.self_test:
            self_test(DEFAULT_BUNDLES[0])
        print(f"publication contract validation passed ({len(roots)} bundle(s), schema refs, DTOs, paths, dates, tokens, and publication consistency)")
        return 0
    except Exception as error:
        print(f"publication contract validation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
