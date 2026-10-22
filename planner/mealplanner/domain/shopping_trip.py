"""ShoppingTrip aggregate (UI: "shopping list"): invariants, item summation, week coverage.

A trip covers a set of weekly plans, each per shelf-life group (long_lasting, fresh), and
answers "what do we buy for these weeks?". Each (plan, group) is a cell, ``to_buy`` or
``bought``; a bought cell freezes the plan revision it was bought at. The trip status
(open/done) and completed_at are derived from its cells. It never modifies a plan.
Everything here is pure:
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
TO_BUY = "to_buy"
BOUGHT = "bought"

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


class InvalidTransition(TripError):
    """A cell is already in the requested state (or there is no cell to move)."""


class WeekNotOnTrip(TripError):
    """The plan (or the requested group of it) is not on the trip."""


class LastWeek(TripError):
    """A trip keeps at least one week; delete the trip instead of removing its last week or group."""


class InvalidTrip(TripError):
    """Trip input is invalid (no weeks, duplicate weeks, bad name or groups)."""


# ---------------------------------------------------------------- records

MAX_NAME_LENGTH = 80


@dataclass(frozen=True)
class TripCell:
    """One (week, group) on the list. bought_at/revision/snapshot_id are set exactly when bought."""

    group: str
    state: str = TO_BUY
    bought_at: datetime | None = None
    revision: int | None = None
    snapshot_id: str | None = None


@dataclass(frozen=True)
class TripWeek:
    """One covered week. Pass groups (all to buy) or cells; groups always follows cells."""

    plan_id: str
    week_start: date
    groups: tuple[str, ...] = GROUPS  # non-empty, canonical order
    cells: tuple[TripCell, ...] = ()

    def __post_init__(self) -> None:
        if self.cells:
            by_group = {c.group: c for c in self.cells}
            groups = clean_groups(by_group)
            object.__setattr__(self, "cells", tuple(by_group[g] for g in groups))
            object.__setattr__(self, "groups", groups)
        else:
            groups = clean_groups(self.groups)
            object.__setattr__(self, "groups", groups)
            object.__setattr__(self, "cells", tuple(TripCell(g) for g in groups))

    def cell(self, group: str) -> TripCell | None:
        return next((c for c in self.cells if c.group == group), None)


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
    version: int
    created_at: datetime
    updated_at: datetime
    weeks: tuple[TripWeek, ...]  # sorted by week_start

    @property
    def all_cells(self) -> tuple[tuple[TripWeek, TripCell], ...]:
        return tuple((w, c) for w in self.weeks for c in w.cells)

    @property
    def status(self) -> str:
        """done when every cell is bought, else open."""
        cells = self.all_cells
        return DONE if cells and all(c.state == BOUGHT for _, c in cells) else OPEN

    @property
    def completed_at(self) -> datetime | None:
        """The latest bought_at while done, else None."""
        if self.status != DONE:
            return None
        return max(c.bought_at for _, c in self.all_cells if c.bought_at is not None)

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
    return ShoppingTrip(trip_id, clean_name(name), 1, now, now, open_weeks)


def require_version(trip: ShoppingTrip, expected: int) -> None:
    if trip.version != expected:
        raise StaleVersion(expected, trip.version)


def _bump(trip: ShoppingTrip, now: datetime, **changes: object) -> ShoppingTrip:
    return replace(trip, version=trip.version + 1, updated_at=now, **changes)


def add_week(trip: ShoppingTrip, week: TripWeek, expected: int, now: datetime) -> ShoppingTrip:
    """Add the week's groups as to-buy cells (any state; a done list becomes open).

    A week already on the list is extended with the groups it lacks."""
    require_version(trip, expected)
    existing = next((w for w in trip.weeks if w.plan_id == week.plan_id), None)
    if existing is None:
        return _bump(
            trip, now,
            weeks=_sorted_weeks((*trip.weeks, TripWeek(week.plan_id, week.week_start, groups=week.groups))),
        )  # fmt: skip
    new = [g for g in week.groups if g not in existing.groups]
    if not new:  # nothing to add: every requested group is already on this list
        raise WeekAlreadyOnTrip(week.plan_id, trip.trip_id, week.groups[0])
    merged = replace(existing, cells=(*existing.cells, *(TripCell(g) for g in new)))
    return _bump(trip, now, weeks=_sorted_weeks(merged if w is existing else w for w in trip.weeks))


def remove_week(
    trip: ShoppingTrip, plan_id: str, expected: int, now: datetime, group: str | None = None
) -> ShoppingTrip:
    """Remove the whole week, or one group of it (the week goes when no group is left)."""
    require_version(trip, expected)
    week = next((w for w in trip.weeks if w.plan_id == plan_id), None)
    if week is None or (group is not None and group not in week.groups):
        raise WeekNotOnTrip(plan_id)
    if group is not None and group not in GROUPS:
        raise InvalidTrip(f"group must be one of {', '.join(GROUPS)}")
    left = tuple(c for c in week.cells if c.group != group) if group is not None else ()
    if not left and len(trip.weeks) == 1:
        raise LastWeek("a trip needs at least one week; delete the trip instead")
    others = tuple(w for w in trip.weeks if w.plan_id != plan_id)
    kept = (replace(week, cells=left),) if left else ()
    return _bump(trip, now, weeks=_sorted_weeks((*others, *kept)))


CellRef = tuple[str, str]  # (plan_id, group)


def _targets(trip: ShoppingTrip, refs: Sequence[CellRef], state: str) -> list[CellRef]:
    """Validate refs: non-empty, unique, on the trip (WeekNotOnTrip), not already in `state`."""
    if not refs:
        raise InvalidTrip("cells must list at least one (plan_id, group)")
    if len(set(refs)) != len(refs):
        raise InvalidTrip("cells must be unique")
    for plan_id, group in refs:
        week = next((w for w in trip.weeks if w.plan_id == plan_id), None)
        cell = week.cell(group) if week else None
        if cell is None:
            raise WeekNotOnTrip(plan_id)
    for plan_id, group in refs:
        cell = next(w for w in trip.weeks if w.plan_id == plan_id).cell(group)
        assert cell is not None
        if cell.state == state:
            raise InvalidTransition(f"{plan_id} ({group}) is already {state}")
    return list(refs)


def _with_cells(
    trip: ShoppingTrip, now: datetime, change: Mapping[CellRef, TripCell]
) -> ShoppingTrip:
    weeks = tuple(
        replace(w, cells=tuple(change.get((w.plan_id, c.group), c) for c in w.cells))
        for w in trip.weeks
    )
    return _bump(trip, now, weeks=weeks)


def mark_bought(
    trip: ShoppingTrip,
    refs: Sequence[CellRef],
    frozen: Mapping[str, tuple[int, str]],
    expected: int,
    now: datetime,
) -> ShoppingTrip:
    """Mark cells bought, freezing frozen[plan_id] = (revision, snapshot_id) for each.

    All or nothing: WeekNotOnTrip for an unknown cell, InvalidTransition if one is already bought."""
    require_version(trip, expected)
    targets = _targets(trip, refs, BOUGHT)
    return _with_cells(
        trip, now,
        {(p, g): TripCell(g, BOUGHT, now, frozen[p][0], frozen[p][1]) for p, g in targets},
    )  # fmt: skip


def unmark_bought(
    trip: ShoppingTrip, refs: Sequence[CellRef], expected: int, now: datetime
) -> ShoppingTrip:
    """Return bought cells to to-buy (current revision again)."""
    require_version(trip, expected)
    targets = _targets(trip, refs, TO_BUY)
    return _with_cells(trip, now, {(p, g): TripCell(g) for p, g in targets})


def cells_in_state(trip: ShoppingTrip, state: str) -> list[CellRef]:
    return [(w.plan_id, c.group) for w, c in trip.all_cells if c.state == state]


# ---------------------------------------------------------------- derived views


def sum_items(
    weeks: Sequence[tuple[str, JsonObject, Sequence[str]]],
    labels: Mapping[str, str],
    *,
    skip_unlabeled: bool = False,
) -> list[JsonObject]:
    """Sum saved weekly grocery rows per (ingredient_id, unit), only for each week's covered groups.

    weeks is [(plan_id, shopping_list, groups)] in week order; labels maps ingredient_id to its
    shelf life. Each item carries its shelf_life, the plan_ids that contribute and quantity_by_plan
    (per-plan quantities rounded to 2 decimals; quantity is exactly their sum). Rows keep
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
                        "quantity_by_plan": {},
                    }
                by_plan = item["quantity_by_plan"]
                by_plan[plan_id] = by_plan.get(plan_id, 0.0) + line["quantity"]
                if plan_id not in item["plan_ids"]:
                    item["plan_ids"].append(plan_id)
    items = list(totals.values())
    for item in items:
        by_plan = {p: round(q, 2) for p, q in item["quantity_by_plan"].items()}
        item["quantity_by_plan"] = by_plan
        item["quantity"] = round(sum(by_plan.values()), 2)
    items.sort(key=lambda i: section_order[i["section"]])  # stable
    return items


def merge_cells(
    cells: Iterable[tuple[str, int | None, JsonObject, str]],
) -> list[tuple[str, JsonObject, list[str]]]:
    """Group (plan_id, revision, shopping_list, group) cells into sum_items entries.

    Cells of one plan at the same revision share one entry, so item order matches a single pass
    over that week's rows (the view and the publication check must agree on it)."""
    merged: dict[tuple[str, int | None], tuple[str, JsonObject, list[str]]] = {}
    for plan_id, revision, shopping, group in cells:
        entry = merged.setdefault((plan_id, revision), (plan_id, shopping, []))
        entry[2].append(group)
    return list(merged.values())


def week_coverage(current_revision: int, cell: TripCell | None) -> tuple[str, bool]:
    """(coverage, changed_since_shopping) for one (plan, group) given its cell, if on a trip."""
    if cell is None:
        return NOT_COVERED, False
    if cell.state == TO_BUY:
        return TO_BUY, False
    return BOUGHT, cell.revision is not None and current_revision > cell.revision
