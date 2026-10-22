"""Ports: the storage and input boundaries the application layer depends on.

Adapters implement these Protocols; the application never imports an adapter.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol

from ..domain.auth import SessionRecord, User
from ..domain.planner_inputs import PlannerInputs
from ..domain.shopping_trip import ShoppingTrip
from ..domain.snapshot import JsonObject
from ..domain.solution import MealPlanSolution
from ..domain.weekly_plan import PlanRevision, Proposal, RetryRecord, WeeklyPlan

if TYPE_CHECKING:  # annotation only; application.publication imports this module
    from .publication import PublicationBundle
    from .publishing import DeploymentStatus, PublishedContent, PublishJob


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
    (plan, shelf-life group) pair belongs to at most one trip of any status. Trips only reference plans; they
    never write them.
    """

    def create(self, trip: ShoppingTrip) -> None:
        """Insert a new trip with its weeks. Raises WeekAlreadyOnTrip when any of its
        (plan, group) pairs is already on a trip, PlanNotFound when a plan does not exist."""

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


class PublicationWriter(Protocol):
    """Output boundary for a checked publication bundle (implemented by adapters/publication_files)."""

    def write(self, bundle: "PublicationBundle") -> None:
        """Make the bundle the active publication, or raise PublicationError leaving the
        previously active publication untouched. The new publication becomes active last."""


class PublicationRepository(Protocol):
    """The public repository as a local working copy (implemented by adapters/git_publication).

    The remote is authoritative: ``sync`` discards anything local that is not on it. Failures
    raise ``PublishRepoError`` with a message that is safe to show (no paths, no secrets).
    """

    def sync(self) -> PublishedContent | None:
        """Clone or fast-forward/reset to the remote main and return what it publishes (None if nothing)."""

    def write(self, bundle: PublicationBundle, app_ref: str) -> None:
        """Write ``publication/`` (new bundle, retention applied) and ``app-ref`` into the working copy."""

    def commit_and_push(self, message: str) -> str:
        """Commit the working copy changes, push to the remote main (never forced), return the commit SHA."""


class DeploymentStatusReader(Protocol):
    def status(self, commit_sha: str) -> DeploymentStatus:
        """pending / success / failure of the Pages deployment for this commit.

        Raises ``DeploymentStatusError`` when it cannot be determined right now."""


class PublishJobRepository(Protocol):
    """Private publication job records."""

    def start(self, job: PublishJob, stale_before: datetime) -> None:
        """Atomically insert a running job. A running job started before ``stale_before`` is marked
        failed first; any other running job raises ``PublishInProgress``."""

    def save(self, job: PublishJob) -> None:
        """Replace the stored job with the same id."""

    def get(self, job_id: str) -> PublishJob | None: ...

    def latest(self) -> PublishJob | None:
        """The most recently started job."""

    def last_with_status(self, statuses: tuple[str, ...]) -> PublishJob | None:
        """The most recently started job whose status is one of ``statuses``."""


class UserRepository(Protocol):
    """Household login accounts."""

    def add(self, user: User) -> None:
        """Insert a user. Raises ``UsernameTaken`` when the (normalized) username exists."""

    def get_by_username(self, username: str) -> User | None: ...

    def get_by_id(self, user_id: str) -> User | None: ...

    def list(self) -> list[User]:
        """Every user, oldest first."""

    def set_password_hash(self, user_id: str, password_hash: str) -> None: ...

    def set_disabled(self, user_id: str, disabled: bool) -> None: ...

    def delete(self, user_id: str) -> None:
        """Remove the user (its sessions go with it)."""


class SessionRepository(Protocol):
    """Login sessions, keyed by the SHA-256 of the cookie token."""

    def add(self, session: SessionRecord) -> None: ...

    def get(self, session_hash: str) -> SessionRecord | None: ...

    def touch(self, session_hash: str, seen_at: datetime) -> None: ...

    def delete(self, session_hash: str) -> None: ...

    def delete_for_user(self, user_id: str) -> None: ...

    def delete_expired(self, now: datetime, idle_before: datetime) -> None:
        """Drop sessions past their absolute expiry or last seen before ``idle_before``."""
