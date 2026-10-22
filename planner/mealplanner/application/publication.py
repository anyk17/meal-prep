"""Publication export: select months, read current plans and trips, build and check a bundle.

Pure of the filesystem. The bundle is the fixed layout of docs/migration-to-cloud
(publication.schema.json): an index plus, relative to ``publications/<id>/``, a manifest,
``plans/<plan_id>/{plan,shopping-list}.json`` and ``shopping-trips/<trip_id>.json``.
``check_bundle`` is the structural/coherence check shared with the writer adapter; the full
JSON-schema validation (scripts/validate_publication_contract.py) is injected by the entrypoint.

Read consistency: plans, groceries and trips are read through separate ports, not in one
database transaction. ``build`` instead reads optimistically, checks the pieces agree
(summaries, plan/shopping tokens, trip weeks against published plans) and re-reads the
summaries at the end; any disagreement means something changed mid-export and the whole read
is retried (``READ_ATTEMPTS`` times) before failing with ``PublicationError``.
"""

from __future__ import annotations

import re
import secrets
from calendar import monthrange
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ..domain import shopping_trip as trips
from ..domain.snapshot import JsonObject
from .ports import PlanReadRepository, PublicationWriter
from .shopping_trips import ShoppingTripService

SCHEMA_VERSION = 1
READ_ATTEMPTS = 3
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")

Clock = Callable[[], datetime]  # must return a timezone-aware datetime


class PublicationError(Exception):
    """The export cannot produce (or write) a coherent bundle."""

    def __init__(self, message: str, errors: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.errors = list(errors)


@dataclass(frozen=True)
class PublicationBundle:
    """A complete, checked publication. ``files`` maps paths relative to the publication directory."""

    publication_id: str
    index: JsonObject
    files: Mapping[str, JsonObject]


@dataclass(frozen=True)
class SkippedTrip:
    trip_id: str
    name: str
    reason: str


@dataclass(frozen=True)
class PublicationResult:
    publication_id: str
    exported_at: str
    selected_months: tuple[str, ...]
    plans: tuple[JsonObject, ...]  # PlanSummary dicts, in week order
    trips_included: tuple[JsonObject, ...]  # {"trip_id", "name", "status"}
    trips_skipped: tuple[SkippedTrip, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------- months


def normalize_months(months: Iterable[str]) -> list[str]:
    """Validate YYYY-MM strings and return them sorted and unique."""
    cleaned = sorted(set(months))
    if not cleaned:
        raise PublicationError("select at least one month (YYYY-MM)")
    for month in cleaned:
        if not isinstance(month, str) or not MONTH.fullmatch(month):
            raise PublicationError(f"not a YYYY-MM month: {month!r}")
    return cleaned


def month_bounds(month: str) -> tuple[date, date]:
    first = date.fromisoformat(f"{month}-01")
    return first, date(first.year, first.month, monthrange(first.year, first.month)[1])


def overlaps_months(week_start: date, week_end: date, months: Sequence[str]) -> bool:
    """True when the Monday-Sunday week shares a day with any selected month."""
    return any(week_start <= last and week_end >= first for first, last in map(month_bounds, months))


# ---------------------------------------------------------------- paths


def plan_path(plan_id: str) -> str:
    return f"plans/{plan_id}/plan.json"


def shopping_path(plan_id: str) -> str:
    return f"plans/{plan_id}/shopping-list.json"


def trip_path(trip_id: str) -> str:
    return f"shopping-trips/{trip_id}.json"


# ---------------------------------------------------------------- bundle check

_SUMMARY_KEYS = ("plan_id", "week_start", "week_end", "timezone", "revision", "snapshot_id", "generated_at", "seed")


def check_bundle(index: JsonObject, files: Mapping[str, JsonObject]) -> list[str]:
    """Structural and coherence errors for a bundle (empty list = coherent).

    Mirrors the semantic rules of the public contract that need no JSON Schema: identities,
    ordering, month overlap, plan/grocery agreement, trip membership and revision rules, and
    "no file unless referenced". Schema validation of DTO shapes is done by the full validator.
    """
    errors: list[str] = []
    pid = index.get("publication_id")
    if not isinstance(pid, str) or not SAFE_ID.fullmatch(pid):
        return [f"index publication_id is not a safe id: {pid!r}"]
    if index.get("manifest_path") != f"publications/{pid}/manifest.json":
        errors.append("index manifest_path must be publications/<publication_id>/manifest.json")
    manifest = files.get("manifest.json")
    if manifest is None:
        return errors + ["missing manifest.json"]
    if manifest.get("publication_id") != pid:
        errors.append("manifest publication_id must match index")
    months = manifest.get("selected_months") or []
    if not months or months != sorted(set(months)) or not all(
        isinstance(m, str) and MONTH.fullmatch(m) for m in months
    ):
        errors.append("selected_months must be non-empty, sorted, unique YYYY-MM values")
        months = []
    referenced = {"manifest.json"}
    published: dict[str, JsonObject] = {}
    entries = manifest.get("plans") or []
    starts = [e["summary"]["week_start"] for e in entries]
    if starts != sorted(set(starts)):
        errors.append("manifest plans must be sorted by unique week_start")
    for entry in entries:
        summary = entry["summary"]
        plan_id = summary["plan_id"]
        if plan_id in published:
            errors.append(f"duplicate plan_id {plan_id}")
        published[plan_id] = summary
        if summary.get("timezone") != manifest.get("timezone"):
            errors.append(f"{plan_id}: timezone must match manifest")
        start, end = date.fromisoformat(summary["week_start"]), date.fromisoformat(summary["week_end"])
        if start.weekday() != 0 or end != start + timedelta(days=6):
            errors.append(f"{plan_id}: week must be Monday through Sunday")
        if months and not overlaps_months(start, end, months):
            errors.append(f"{plan_id}: week does not overlap any selected month")
        if (entry.get("plan_path"), entry.get("shopping_path")) != (plan_path(plan_id), shopping_path(plan_id)):
            errors.append(f"{plan_id}: paths must match the plan_id directory")
        plan, shopping = files.get(plan_path(plan_id)), files.get(shopping_path(plan_id))
        referenced.update((plan_path(plan_id), shopping_path(plan_id)))
        if plan is None or shopping is None:
            errors.append(f"{plan_id}: missing plan or shopping-list file")
            continue
        if {k: plan.get(k) for k in _SUMMARY_KEYS} != {k: summary.get(k) for k in _SUMMARY_KEYS}:
            errors.append(f"{plan_id}: summary must exactly match plan metadata")
        for key in ("plan_id", "revision", "week_start", "week_end"):
            if shopping.get(key) != plan.get(key):
                errors.append(f"{plan_id}: shopping list {key} must match the plan")
        if shopping.get("snapshot_id") != plan.get("snapshot_id"):
            errors.append(f"{plan_id}: shopping list snapshot_id must match the plan")
    errors += _check_trips(manifest, files, published, referenced)
    for path in sorted(set(files) - referenced):
        errors.append(f"unreferenced file {path}")
    return errors


def _check_trips(
    manifest: JsonObject,
    files: Mapping[str, JsonObject],
    published: Mapping[str, JsonObject],
    referenced: set[str],
) -> list[str]:
    errors: list[str] = []
    entries = manifest.get("shopping_trips")
    if not isinstance(entries, list):
        return ["manifest shopping_trips must be an array"]
    ids = [e["trip_id"] for e in entries]
    if ids != sorted(set(ids)):
        errors.append("shopping_trips must be sorted by unique trip_id")
    owner: dict[tuple[str, str], str] = {}  # (plan_id, group) -> trip_id
    labels: dict[str, str] = {}  # ingredient_id -> shelf_life, as the bundle's trip items show it
    for entry in entries:
        trip = files.get(trip_path(entry["trip_id"]))
        for item in (trip or {}).get("items") or []:
            known = labels.setdefault(item["ingredient_id"], item.get("shelf_life"))
            if known != item.get("shelf_life"):
                errors.append(
                    f"ingredient {item['ingredient_id']} has conflicting shelf_life values in trip items"
                )
    for entry in entries:
        trip_id = entry["trip_id"]
        if not SAFE_ID.fullmatch(trip_id) or entry.get("path") != trip_path(trip_id):
            errors.append(f"trip {trip_id}: path must be {trip_path(trip_id)}")
        referenced.add(trip_path(trip_id))
        trip = files.get(trip_path(trip_id))
        if trip is None:
            errors.append(f"trip {trip_id}: missing file")
            continue
        if trip.get("trip_id") != trip_id:
            errors.append(f"trip {trip_id}: file trip_id must match its manifest entry")
        if (trip.get("status") == trips.DONE) != (trip.get("completed_at") is not None):
            errors.append(f"trip {trip_id}: completed_at must be set exactly when done")
        weeks = trip.get("weeks") or []
        if not weeks:
            errors.append(f"trip {trip_id}: needs at least one week")
        for week in weeks:
            plan_id = week["plan_id"]
            groups = week.get("groups") or []
            if not groups or list(groups) != [g for g in trips.GROUPS if g in groups]:
                errors.append(f"trip {trip_id}: week {plan_id} groups must be a non-empty subset in order {', '.join(trips.GROUPS)}")
            for group in groups:
                if owner.setdefault((plan_id, group), trip_id) != trip_id:
                    errors.append(
                        f"trip {trip_id}: plan {plan_id} ({group}) is already on trip {owner[(plan_id, group)]}"
                    )
            summary = published.get(plan_id)
            if summary is None:
                errors.append(f"trip {trip_id}: week {plan_id} has no entry in manifest plans")
            elif trip.get("status") == trips.OPEN:
                if (week["revision"], week["snapshot_id"]) != (summary["revision"], summary["snapshot_id"]):
                    errors.append(f"trip {trip_id}: open week {plan_id} must use the published revision and snapshot_id")
            elif week["revision"] > summary["revision"]:
                errors.append(f"trip {trip_id}: done week {plan_id} revision exceeds the published revision")
        if all(
            w["plan_id"] in published
            and shopping_path(w["plan_id"]) in files
            and w["revision"] == published[w["plan_id"]]["revision"]
            for w in weeks
        ):  # items must be the sums of the published shopping lists
            expected = trips.sum_items(
                [(w["plan_id"], files[shopping_path(w["plan_id"])], w.get("groups") or []) for w in weeks],
                labels,
                skip_unlabeled=True,
            )
            if trip.get("items") != expected:
                errors.append(
                    f"trip {trip_id}: items must equal the sum of the covered groups' rows of the weeks' shopping lists"
                )
    return errors


# ---------------------------------------------------------------- service


class _Inconsistent(Exception):
    """The data changed while it was being read; retry the whole read."""


class PublicationService:
    """Builds (``build``) and writes (``export``) a publication from the current stored state."""

    def __init__(
        self,
        plans: PlanReadRepository,
        trip_service: ShoppingTripService,
        clock: Clock,
        timezone_name: str,
        writer: PublicationWriter | None = None,
        id_factory: Callable[[], str] = lambda: secrets.token_hex(3),
    ) -> None:
        self._plans = plans
        self._trips = trip_service
        self._clock = clock
        self._timezone = timezone_name
        self._writer = writer
        self._new_id = id_factory

    def build(self, months: Iterable[str]) -> tuple[PublicationBundle, PublicationResult]:
        selected = normalize_months(months)
        last_error = "unknown"
        for _ in range(READ_ATTEMPTS):
            try:
                return self._build_once(selected)
            except _Inconsistent as exc:
                last_error = str(exc)
        raise PublicationError(f"data kept changing during export ({last_error}); try again")

    def export(self, months: Iterable[str]) -> PublicationResult:
        if self._writer is None:
            raise PublicationError("no PublicationWriter configured")
        bundle, result = self.build(months)
        self._writer.write(bundle)
        return result

    # ---------------------------------------------------------- internals

    def _build_once(self, months: list[str]) -> tuple[PublicationBundle, PublicationResult]:
        now = self._clock().astimezone(timezone.utc)
        publication_id = f"pub_{now:%Y%m%dT%H%M%SZ}_{self._new_id()}"
        summaries = self._selected_summaries(months)
        files: dict[str, JsonObject] = {}
        entries: list[JsonObject] = []
        for summary in summaries:
            plan_id = summary["plan_id"]
            plan = self._plans.get_plan(plan_id)
            shopping = self._plans.get_shopping_list(plan_id)
            if plan is None or shopping is None:
                raise _Inconsistent(f"plan {plan_id} disappeared")
            if {k: plan.get(k) for k in _SUMMARY_KEYS} != {k: summary.get(k) for k in _SUMMARY_KEYS}:
                raise _Inconsistent(f"plan {plan_id} changed")
            if summary["timezone"] != self._timezone:
                raise PublicationError(
                    f"plan {plan_id} uses timezone {summary['timezone']}, not {self._timezone}"
                )
            if (shopping["revision"], shopping["snapshot_id"]) != (summary["revision"], summary["snapshot_id"]):
                raise _Inconsistent(f"shopping list of {plan_id} changed")
            files[plan_path(plan_id)] = plan
            files[shopping_path(plan_id)] = shopping
            entries.append(
                {"summary": deepcopy(summary), "plan_path": plan_path(plan_id), "shopping_path": shopping_path(plan_id)}
            )
        included, skipped = self._select_trips(summaries)
        for trip in included:
            files[trip_path(trip["trip_id"])] = trip
        if self._selected_summaries(months) != summaries:
            raise _Inconsistent("plans changed during the read")
        manifest: JsonObject = {
            "schema_version": SCHEMA_VERSION,
            "publication_id": publication_id,
            "exported_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "selected_months": months,
            "timezone": self._timezone,
            "plans": entries,
            "shopping_trips": [
                {"trip_id": t["trip_id"], "path": trip_path(t["trip_id"])}
                for t in sorted(included, key=lambda t: t["trip_id"])
            ],
        }
        files["manifest.json"] = manifest
        index: JsonObject = {
            "schema_version": SCHEMA_VERSION,
            "publication_id": publication_id,
            "manifest_path": f"publications/{publication_id}/manifest.json",
        }
        errors = check_bundle(index, files)
        if errors:
            if any("open week" in e or "done week" in e or "items must" in e for e in errors):
                raise _Inconsistent("; ".join(errors))
            raise PublicationError("export produced an incoherent bundle", errors)
        result = PublicationResult(
            publication_id,
            manifest["exported_at"],
            tuple(months),
            tuple(summaries),
            tuple({"trip_id": t["trip_id"], "name": t["name"], "status": t["status"]} for t in included),
            tuple(skipped),
        )
        return PublicationBundle(publication_id, index, files), result

    def _selected_summaries(self, months: Sequence[str]) -> list[JsonObject]:
        selected = [
            deepcopy(s)
            for s in self._plans.list_plan_summaries()
            if overlaps_months(date.fromisoformat(s["week_start"]), date.fromisoformat(s["week_end"]), months)
        ]
        selected.sort(key=lambda s: (s["week_start"], s["plan_id"]))
        return selected

    def _select_trips(self, summaries: Sequence[JsonObject]) -> tuple[list[JsonObject], list[SkippedTrip]]:
        in_bundle = {s["plan_id"] for s in summaries}
        included: list[JsonObject] = []
        skipped: list[SkippedTrip] = []
        for trip in sorted(self._trips.list()["items"], key=lambda t: t["trip_id"]):
            missing = [w["plan_id"] for w in trip["weeks"] if w["plan_id"] not in in_bundle]
            if missing:
                skipped.append(
                    SkippedTrip(
                        trip["trip_id"],
                        trip["name"],
                        "weeks not in the selected months: " + ", ".join(missing),
                    )
                )
            else:
                included.append(trip)
        return included, skipped
