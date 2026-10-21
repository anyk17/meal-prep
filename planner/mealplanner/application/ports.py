"""Ports: the storage and input boundaries the application layer depends on.

Adapters implement these Protocols; the application never imports an adapter.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from ..domain.planner_inputs import PlannerInputs
from ..domain.shopping_trip import ShoppingTrip
from ..domain.snapshot import JsonObject
from ..domain.solution import MealPlanSolution
from ..domain.weekly_plan import PlanRevision, Proposal, RetryRecord, WeeklyPlan


class PlannerInputReader(Protocol):
    """Source adapter that returns fully validated planner inputs."""

    def read(self) -> PlannerInputs:
        """Read and validate one complete planner input set."""


class PlanGenerator(Protocol):
    """Engine that proposes one weekly solution for explicit inputs (implemented by adapters/ortools).

    The result is only a candidate: the application checks it against
    domain/weekly_rules.py before it can become a revision. Raises
    NoFeasiblePlan (or any exception) when no plan can be produced.
    """

    def generate(self, inputs: PlannerInputs, seed: int) -> MealPlanSolution:
        """Return a candidate solution for the seed."""


class PlanReadRepository(Protocol):
    """Read-only storage boundary consumed by the application service."""

    def list_plan_summaries(self) -> list[JsonObject]:
        """Return summary copies for all currently available plans."""

    def get_plan(self, plan_id: str) -> JsonObject | None:
        """Return a current plan detail copy, or None when it is absent."""

    def get_shopping_list(self, plan_id: str) -> JsonObject | None:
        """Return current shopping data, or None when its plan is absent."""


class PlanWorkflowRepository(Protocol):
    """Storage boundary for the lifecycle service (implemented by the in-memory and SQLite adapters)."""

    def create_plan(self, plan: WeeklyPlan, first_revision: PlanRevision) -> None:
        """Atomically insert the header (current_revision=1) and revision 1.

        Raises DuplicateWeek when a plan already exists for plan.week_start
        (a UNIQUE constraint on the week).
        """

    def get_plan(self, plan_id: str) -> WeeklyPlan | None:
        """Return the plan header, or None."""

    def get_plan_by_week(self, week_start: date) -> WeeklyPlan | None:
        """Return the plan header for a Monday, or None."""

    def get_revision(self, plan_id: str, revision: int) -> PlanRevision | None:
        """Return one stored revision, or None."""

    def append_revision(
        self,
        plan_id: str,
        expected_revision: int,
        new_revision: PlanRevision,
        retry: RetryRecord | None,
    ) -> None:
        """In ONE transaction: check current_revision == expected_revision, insert
        new_revision (revision == expected_revision + 1), move the pointer, and
        store the retry record if given. Raises StaleRevision on mismatch."""

    def list_revisions(self, plan_id: str) -> list[PlanRevision]:
        """Return every stored revision of a plan, oldest first."""

    def get_retry(self, plan_id: str, retry_id: str) -> RetryRecord | None:
        """Return a stored retry record, or None."""

    def save_retry(self, retry: RetryRecord) -> None:
        """Store a retry record for an outcome that appended no revision."""

    def save_proposal(self, proposal: Proposal) -> None:
        """Store (or replace) a proposal."""

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        """Return a proposal, or None."""

    def delete_proposal(self, proposal_id: str) -> None:
        """Remove a proposal if present."""


class ShoppingTripRepository(Protocol):
    """Storage boundary for ShoppingTrip aggregates (in-memory and SQLite adapters).

    Every write is atomic and enforces, in storage as well as in the domain, that a
    plan belongs to at most one trip of any status. Trips only reference plans; they
    never write them.
    """

    def create(self, trip: ShoppingTrip) -> None:
        """Insert a new trip with its weeks. Raises WeekAlreadyOnTrip when any of its
        plans is already on a trip, PlanNotFound when a plan does not exist."""

    def get(self, trip_id: str) -> ShoppingTrip | None:
        """Return one trip, or None."""

    def list(self) -> list[ShoppingTrip]:
        """Return every trip, oldest first."""

    def save(self, trip: ShoppingTrip, expected_version: int) -> None:
        """Replace the trip's fields and weeks if its stored version is expected_version
        (the caller passes trip.version == expected_version + 1). Raises TripNotFound,
        StaleVersion, WeekAlreadyOnTrip, or PlanNotFound."""

    def delete(self, trip_id: str, expected_version: int) -> None:
        """Delete the trip (freeing its weeks). Raises TripNotFound or StaleVersion."""
