"""ShoppingTrip use cases: create, change weeks, mark/unmark purchases, done/reopen, delete, read, coverage.

Independent of Flask and storage. Trips only read plans (through PlanWorkflowRepository)
and never write them or create plan revisions. Every mutation carries the expected trip
version; the repository applies it atomically (stale -> StaleVersion).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta

from ..domain import shopping_trip as trips
from ..domain.shopping_trip import ShoppingTrip, TripNotFound, TripWeek
from ..domain.snapshot import JsonObject
from .errors import PlanNotFound
from .ports import PlanWorkflowRepository, ShoppingTripRepository

Clock = Callable[[], datetime]  # must return a timezone-aware datetime
Labels = Callable[[], Mapping[str, str]]  # ingredient_id -> shelf life, read from current inputs


class ShoppingTripService:
    """Application service for the ShoppingTrip aggregate."""

    def __init__(
        self,
        repository: ShoppingTripRepository,
        plans: PlanWorkflowRepository,
        clock: Clock,
        labels: Labels,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._repo = repository
        self._plans = plans
        self._clock = clock
        self._labels = labels
        self._new_id = id_factory

    # ------------------------------------------------------------ reads

    def get(self, trip_id: str) -> JsonObject:
        return self._view(self._trip(trip_id), self._labels())

    def list(self) -> JsonObject:
        labels = self._labels()
        return {"items": [self._view(t, labels) for t in self._repo.list()]}

    def coverage(self, summaries: Sequence[JsonObject]) -> JsonObject:
        """Per-plan, per-group coverage for plan summaries (plan_id, week_start, week_end, revision)."""
        owner: dict[tuple[str, str], tuple[ShoppingTrip, trips.TripCell]] = {}
        for trip in self._repo.list():
            for week, cell in trip.all_cells:
                owner[(week.plan_id, cell.group)] = (trip, cell)
        items = []
        for s in summaries:
            groups: JsonObject = {}
            for group in trips.GROUPS:
                trip, cell = owner.get((s["plan_id"], group), (None, None))
                coverage, changed = trips.week_coverage(s["revision"], cell)
                groups[group] = {
                    "coverage": coverage,
                    "trip_id": trip.trip_id if trip else None,
                    "changed_since_shopping": changed,
                }
            items.append(
                {
                    "plan_id": s["plan_id"],
                    "week_start": s["week_start"],
                    "week_end": s["week_end"],
                    "groups": groups,
                }
            )
        return {"items": items}

    # ------------------------------------------------------------ mutations

    def create(
        self,
        plan_ids: Sequence[str],
        name: str | None = None,
        groups: Sequence[str] | None = None,
    ) -> JsonObject:
        """A new open list covering the weeks for the groups (default both)."""
        covered = trips.clean_groups(groups)
        weeks = [self._week(plan_id, covered) for plan_id in plan_ids]
        trip = trips.new_trip("trip_" + self._new_id(), name, weeks, self._clock())
        self._repo.create(trip)  # raises WeekAlreadyOnTrip (unique plan_id, group)
        return self._view(trip, self._labels())

    def add_week(
        self,
        trip_id: str,
        plan_id: str,
        expected_version: int,
        groups: Sequence[str] | None = None,
    ) -> JsonObject:
        trip = self._trip(trip_id)
        week = self._week(plan_id, trips.clean_groups(groups))
        updated = trips.add_week(trip, week, expected_version, self._clock())
        self._repo.save(updated, expected_version)
        return self._view(updated, self._labels())

    def remove_week(
        self,
        trip_id: str,
        plan_id: str,
        expected_version: int,
        group: str | None = None,
    ) -> JsonObject:
        """Remove the whole week, or just one group of it."""
        trip = self._trip(trip_id)
        updated = trips.remove_week(trip, plan_id, expected_version, self._clock(), group)
        self._repo.save(updated, expected_version)
        return self._view(updated, self._labels())

    def mark_purchases(
        self, trip_id: str, cells: Sequence[tuple[str, str]], expected_version: int
    ) -> JsonObject:
        """Mark cells bought, freezing each week's current revision at this moment."""
        trip = self._trip(trip_id)
        trips.require_version(trip, expected_version)
        updated = trips.mark_bought(
            trip, cells, self._current_revisions(trip, cells), expected_version, self._clock()
        )
        self._repo.save(updated, expected_version)
        return self._view(updated, self._labels())

    def unmark_purchases(
        self, trip_id: str, cells: Sequence[tuple[str, str]], expected_version: int
    ) -> JsonObject:
        trip = self._trip(trip_id)
        updated = trips.unmark_bought(trip, cells, expected_version, self._clock())
        self._repo.save(updated, expected_version)
        return self._view(updated, self._labels())

    def mark_done(self, trip_id: str, expected_version: int) -> JsonObject:
        """Shortcut: mark every to-buy cell bought."""
        trip = self._trip(trip_id)
        trips.require_version(trip, expected_version)
        cells = trips.cells_in_state(trip, trips.TO_BUY)
        if not cells:
            raise trips.InvalidTransition("every cell is already bought")
        return self.mark_purchases(trip_id, cells, expected_version)

    def reopen(self, trip_id: str, expected_version: int) -> JsonObject:
        """Shortcut: return every bought cell to to-buy."""
        trip = self._trip(trip_id)
        trips.require_version(trip, expected_version)
        cells = trips.cells_in_state(trip, trips.BOUGHT)
        if not cells:
            raise trips.InvalidTransition("no cell is bought")
        return self.unmark_purchases(trip_id, cells, expected_version)

    def delete(self, trip_id: str, expected_version: int) -> None:
        self._repo.delete(trip_id, expected_version)

    # ------------------------------------------------------------ internals

    def _trip(self, trip_id: str) -> ShoppingTrip:
        trip = self._repo.get(trip_id)
        if trip is None:
            raise TripNotFound(trip_id)
        return trip

    def _plan(self, plan_id: str):
        plan = self._plans.get_plan(plan_id)
        if plan is None:
            raise PlanNotFound(plan_id)
        return plan

    def _week(self, plan_id: str, groups: tuple[str, ...]) -> TripWeek:
        return TripWeek(plan_id, self._plan(plan_id).week_start, groups=groups)

    def _current_revisions(
        self, trip: ShoppingTrip, cells: Sequence[tuple[str, str]]
    ) -> dict[str, tuple[int, str]]:
        """(revision, snapshot_id) of the current revision of each referenced plan on the trip."""
        frozen: dict[str, tuple[int, str]] = {}
        for plan_id, _ in cells:
            if plan_id in frozen or plan_id not in trip.plan_ids:
                continue  # unknown cells are rejected by the domain
            plan = self._plan(plan_id)
            rev = self._plans.get_revision(plan_id, plan.current_revision)
            assert rev is not None
            frozen[plan_id] = (rev.revision, rev.snapshot.plan["snapshot_id"])
        return frozen

    def _view(self, trip: ShoppingTrip, labels: Mapping[str, str]) -> JsonObject:
        """The ShoppingTrip DTO. items = to-buy cells at current revisions, bought_items = bought
        cells at their frozen revisions."""
        weeks: list[JsonObject] = []
        to_buy: list[tuple[str, int | None, JsonObject, str]] = []
        bought: list[tuple[str, int | None, JsonObject, str]] = []
        for week in trip.weeks:
            plan = self._plan(week.plan_id)
            current = self._plans.get_revision(week.plan_id, plan.current_revision)
            assert current is not None
            cells: list[JsonObject] = []
            for cell in week.cells:
                _, changed = trips.week_coverage(plan.current_revision, cell)
                cells.append(
                    {
                        "group": cell.group,
                        "state": cell.state,
                        "bought_at": cell.bought_at.isoformat() if cell.bought_at else None,
                        "revision": cell.revision,
                        "snapshot_id": cell.snapshot_id,
                        "changed_since_shopping": changed,
                    }
                )
                if cell.state == trips.TO_BUY:
                    to_buy.append((week.plan_id, None, current.snapshot.shopping_list, cell.group))
                else:
                    assert cell.revision is not None
                    frozen = self._plans.get_revision(week.plan_id, cell.revision)
                    assert frozen is not None
                    bought.append((week.plan_id, cell.revision, frozen.snapshot.shopping_list, cell.group))
            weeks.append(
                {
                    "plan_id": week.plan_id,
                    "week_start": week.week_start.isoformat(),
                    "week_end": (week.week_start + timedelta(days=6)).isoformat(),
                    "current_revision": plan.current_revision,
                    "groups": list(week.groups),
                    "cells": cells,
                }
            )
        return {
            "trip_id": trip.trip_id,
            "name": trip.display_name,
            "status": trip.status,
            "version": trip.version,
            "created_at": trip.created_at.isoformat(),
            "updated_at": trip.updated_at.isoformat(),
            "completed_at": trip.completed_at.isoformat() if trip.completed_at else None,
            "weeks": weeks,
            "items": trips.sum_items(trips.merge_cells(to_buy), labels),
            "bought_items": trips.sum_items(trips.merge_cells(bought), labels),
        }
