"""ShoppingTrip aggregate (UI: "shopping list"): invariants, item summation, week coverage.

A trip covers a set of weekly plans, each per shelf-life group (long_lasting, fresh), and
answers "what do we buy for these weeks?". It never modifies a plan. Everything here is pure:
mutations return a new trip with ``version + 1``; storage enforces the same invariants (one trip
per (plan, group), version compare-and-swap) atomically.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from .catalog import ShelfLife
from .snapshot import JsonObject

OPEN = "open"
DONE = "done"

NOT_COVERED = "not_covered"
ON_LIST = "on_list"
COVERED = "covered"

LONG_LASTING = ShelfLife.LONG_LASTING.value
FRESH = ShelfLife.FRESH.value
GROUPS: tuple[str, ...] = (LONG_LASTING, FRESH)  # canonical order

# ---------------------------------------------------------------- errors


class TripError(Exception):
    """Base class for expected shopping trip failures."""


class TripNotFound(TripError):
    """No trip exists for the identifier."""


class StaleVersion(TripError):
    """The caller's expected trip version is no longer the current version."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"expected trip version {expected} but current version is {actual}")
        self.expected = expected
        self.actual = actual


class WeekAlreadyOnTrip(TripError):
    """A (plan, group) pair already belongs to a trip (any status); it is on at most one trip."""

    def __init__(self, plan_id: str, trip_id: str | None = None, group: str | None = None) -> None:
        where = f" {trip_id}" if trip_id else ""
        what = f" ({group})" if group else ""
        super().__init__(f"plan {plan_id}{what} is already on trip{where}")
        self.plan_id = plan_id
        self.trip_id = trip_id
        self.group = group


class TripDone(TripError):
    """The action needs an open trip; reopen the done trip first."""


class TripNotDone(TripError):
    """The action needs a done trip (reopen on an open trip, or mark done twice)."""


class WeekNotOnTrip(TripError):
    """The plan (or the requested group of it) is not on the trip."""


class LastWeek(TripError):
    """A trip keeps at least one week; delete the trip instead of removing its last week or group."""


class InvalidTrip(TripError):
    """Trip input is invalid (no weeks, duplicate weeks, bad name or groups)."""


# ---------------------------------------------------------------- records

MAX_NAME_LENGTH = 80


@dataclass(frozen=True)
class TripWeek:
    """One covered week with the groups of it on the list. covered_* are set exactly when done."""

    plan_id: str
    week_start: date
    covered_revision: int | None = None
    covered_snapshot_id: str | None = None
    groups: tuple[str, ...] = GROUPS  # non-empty, canonical order

    def __post_init__(self) -> None:
        object.__setattr__(self, "groups", clean_groups(self.groups))


def clean_groups(groups: Iterable[str] | None) -> tuple[str, ...]:
    """Canonical (long_lasting, fresh) order; None means both. Raises InvalidTrip when empty/unknown."""
    if groups is None:
        return GROUPS
    wanted = list(groups)
    if not wanted or any(g not in GROUPS for g in wanted):
        raise InvalidTrip(f"groups must be a non-empty list of {', '.join(GROUPS)}")
    return tuple(g for g in GROUPS if g in wanted)


@dataclass(frozen=True)
class ShoppingTrip:
    trip_id: str
    name: str | None  # a user-chosen name; None means "named after its weeks"
    status: str  # OPEN | DONE
    version: int
    created_at: datetime
    updated_at: datetime
    weeks: tuple[TripWeek, ...]  # sorted by week_start
    completed_at: datetime | None = None

    @property
    def plan_ids(self) -> tuple[str, ...]:
        return tuple(w.plan_id for w in self.weeks)

    @property
    def display_name(self) -> str:
        """The chosen name, or one derived from the current weeks so it follows add/remove."""
        return self.name if self.name is not None else default_name(self.weeks)


def _sorted_weeks(weeks: Iterable[TripWeek]) -> tuple[TripWeek, ...]:
    return tuple(sorted(weeks, key=lambda w: (w.week_start, w.plan_id)))


def default_name(weeks: Sequence[TripWeek]) -> str:
    """Label from the covered dates, e.g. "Oct 19 - Nov 1"."""
    first = min(w.week_start for w in weeks)
    last = max(w.week_start for w in weeks)
    end = last + timedelta(days=6)

    def fmt(d: date) -> str:
        return f"{d.strftime('%b')} {d.day}"

    return f"{fmt(first)} - {fmt(end)}"


def clean_name(name: str | None) -> str | None:
    """Normalized user name, or None (blank) to name the trip after its weeks."""
    if name is None or not name.strip():
        return None
    cleaned = " ".join(name.split())
    if len(cleaned) > MAX_NAME_LENGTH:
        raise InvalidTrip(f"name must be at most {MAX_NAME_LENGTH} characters")
    return cleaned


# ---------------------------------------------------------------- lifecycle (pure)


def new_trip(
    trip_id: str, name: str | None, weeks: Sequence[TripWeek], now: datetime
) -> ShoppingTrip:
    if not weeks:
        raise InvalidTrip("a trip needs at least one week")
    if len({w.plan_id for w in weeks}) != len(weeks):
        raise InvalidTrip("plan_ids must be unique")
    open_weeks = _sorted_weeks(TripWeek(w.plan_id, w.week_start, groups=w.groups) for w in weeks)
    return ShoppingTrip(trip_id, clean_name(name), OPEN, 1, now, now, open_weeks)


def require_version(trip: ShoppingTrip, expected: int) -> None:
    if trip.version != expected:
        raise StaleVersion(expected, trip.version)


def _bump(trip: ShoppingTrip, now: datetime, **changes: object) -> ShoppingTrip:
    return replace(trip, version=trip.version + 1, updated_at=now, **changes)


def add_week(trip: ShoppingTrip, week: TripWeek, expected: int, now: datetime) -> ShoppingTrip:
    """Add the week's groups; a week already on the list is extended with the new groups."""
    require_version(trip, expected)
    if trip.status == DONE:
        raise TripDone("trip is done; reopen it to change its weeks")
    existing = next((w for w in trip.weeks if w.plan_id == week.plan_id), None)
    if existing is None:
        return _bump(
            trip, now,
            weeks=_sorted_weeks((*trip.weeks, TripWeek(week.plan_id, week.week_start, groups=week.groups))),
        )  # fmt: skip
    new = [g for g in week.groups if g not in existing.groups]
    if not new:  # nothing to add: every requested group is already on this list
        raise WeekAlreadyOnTrip(week.plan_id, trip.trip_id, week.groups[0])
    merged = replace(existing, groups=clean_groups((*existing.groups, *new)))
    return _bump(trip, now, weeks=_sorted_weeks(merged if w is existing else w for w in trip.weeks))


def remove_week(
    trip: ShoppingTrip, plan_id: str, expected: int, now: datetime, group: str | None = None
) -> ShoppingTrip:
    """Remove the whole week, or one group of it (the week goes when no group is left)."""
    require_version(trip, expected)
    if trip.status == DONE:
        raise TripDone("trip is done; reopen it to remove a week")
    week = next((w for w in trip.weeks if w.plan_id == plan_id), None)
    if week is None or (group is not None and group not in week.groups):
        raise WeekNotOnTrip(plan_id)
    if group is not None and group not in GROUPS:
        raise InvalidTrip(f"group must be one of {', '.join(GROUPS)}")
    left = tuple(g for g in week.groups if g != group) if group is not None else ()
    if not left and len(trip.weeks) == 1:
        raise LastWeek("a trip needs at least one week; delete the trip instead")
    others = tuple(w for w in trip.weeks if w.plan_id != plan_id)
    kept = (replace(week, groups=left),) if left else ()
    return _bump(trip, now, weeks=_sorted_weeks((*others, *kept)))


def mark_done(
    trip: ShoppingTrip,
    covered: Mapping[str, tuple[int, str]],
    expected: int,
    now: datetime,
) -> ShoppingTrip:
    """Freeze the trip to covered[plan_id] = (revision, snapshot_id) for every week."""
    require_version(trip, expected)
    if trip.status == DONE:
        raise TripNotDone("trip is already done")
    weeks = tuple(
        TripWeek(w.plan_id, w.week_start, covered[w.plan_id][0], covered[w.plan_id][1], w.groups)
        for w in trip.weeks
    )
    return _bump(trip, now, status=DONE, weeks=weeks, completed_at=now)


def reopen(trip: ShoppingTrip, expected: int, now: datetime) -> ShoppingTrip:
    require_version(trip, expected)
    if trip.status == OPEN:
        raise TripNotDone("trip is not done")
    weeks = tuple(TripWeek(w.plan_id, w.week_start, groups=w.groups) for w in trip.weeks)
    return _bump(trip, now, status=OPEN, weeks=weeks, completed_at=None)


# ---------------------------------------------------------------- derived views


def sum_items(
    weeks: Sequence[tuple[str, JsonObject, Sequence[str]]],
    labels: Mapping[str, str],
    *,
    skip_unlabeled: bool = False,
) -> list[JsonObject]:
    """Sum saved weekly grocery rows per (ingredient_id, unit), only for each week's covered groups.

    weeks is [(plan_id, shopping_list, groups)] in week order; labels maps ingredient_id to its
    shelf life. Each item carries its shelf_life and the plan_ids that contribute. Rows keep
    first-seen order, grouped by first-seen section. No pantry logic: the weekly rows already
    reflect their captured pantry. An ingredient without a label is an error, or skipped with
    skip_unlabeled (publication checks that only know the labels the bundle shows).
    """
    totals: dict[tuple[str, str], JsonObject] = {}
    section_order: dict[str, int] = {}
    for plan_id, shopping, groups in weeks:
        for section, lines in shopping["sections"].items():
            for line in lines:
                label = labels.get(line["ingredient_id"])
                if label is None:
                    if skip_unlabeled:
                        continue
                    raise InvalidTrip(f"ingredient {line['ingredient_id']} has no shelf life")
                if label not in groups:
                    continue
                key = (line["ingredient_id"], line["unit"])
                item = totals.get(key)
                if item is None:
                    section_order.setdefault(line["section"], len(section_order))
                    item = totals[key] = {
                        "ingredient_id": line["ingredient_id"],
                        "display": line["display"],
                        "quantity": 0.0,
                        "unit": line["unit"],
                        "section": line["section"],
                        "shelf_life": label,
                        "plan_ids": [],
                    }
                item["quantity"] += line["quantity"]
                if plan_id not in item["plan_ids"]:
                    item["plan_ids"].append(plan_id)
    items = list(totals.values())
    for item in items:
        item["quantity"] = round(item["quantity"], 2)
    items.sort(key=lambda i: section_order[i["section"]])  # stable
    return items


def week_coverage(
    plan_id: str, current_revision: int, trip: ShoppingTrip | None
) -> tuple[str, bool]:
    """(coverage, changed_since_shopping) for one (plan, group) given the trip that holds it, if any."""
    if trip is None:
        return NOT_COVERED, False
    if trip.status == OPEN:
        return ON_LIST, False
    week = next(w for w in trip.weeks if w.plan_id == plan_id)
    covered = week.covered_revision
    return COVERED, covered is not None and current_revision > covered
