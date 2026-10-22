"""In-memory reference implementations of the workflow and read ports (tests, prototyping)."""

from __future__ import annotations

from copy import deepcopy
import threading
from dataclasses import replace
from datetime import date, datetime

from ..domain.auth import SessionRecord, User, UsernameTaken
from ..domain.shopping_trip import ShoppingTrip, StaleVersion, TripNotFound, WeekAlreadyOnTrip
from ..domain.snapshot import JsonObject, PlanSnapshot
from ..domain.weekly_plan import (
    DuplicateWeek,
    PlanRevision,
    Proposal,
    RetryRecord,
    StaleRevision,
    WeeklyPlan,
)


class InMemoryPlanRepository:
    """Reference implementation of PlanWorkflowRepository, read via InMemoryPlanReadAdapter."""

    def __init__(self) -> None:
        self._plans: dict[str, WeeklyPlan] = {}
        self._week_index: dict[date, str] = {}
        self._revisions: dict[tuple[str, int], PlanRevision] = {}
        self._retries: dict[tuple[str, str], RetryRecord] = {}
        self._proposals: dict[str, Proposal] = {}

    # workflow repository
    def create_plan(self, plan: WeeklyPlan, first_revision: PlanRevision) -> None:
        if plan.week_start in self._week_index or plan.plan_id in self._plans:
            raise DuplicateWeek(f"a plan already exists for week {plan.week_start.isoformat()}")
        self._plans[plan.plan_id] = plan
        self._week_index[plan.week_start] = plan.plan_id
        self._revisions[(plan.plan_id, first_revision.revision)] = first_revision

    def get_plan(self, plan_id: str) -> WeeklyPlan | None:
        return self._plans.get(plan_id)

    def get_plan_by_week(self, week_start: date) -> WeeklyPlan | None:
        plan_id = self._week_index.get(week_start)
        return self._plans[plan_id] if plan_id else None

    def get_revision(self, plan_id: str, revision: int) -> PlanRevision | None:
        return self._revisions.get((plan_id, revision))

    def list_revisions(self, plan_id: str) -> list[PlanRevision]:
        return [
            rev for (pid, _), rev in sorted(self._revisions.items()) if pid == plan_id
        ]

    def append_revision(
        self,
        plan_id: str,
        expected_revision: int,
        new_revision: PlanRevision,
        retry: RetryRecord | None,
    ) -> None:
        plan = self._plans[plan_id]
        if plan.current_revision != expected_revision:
            raise StaleRevision(expected_revision, plan.current_revision)
        if new_revision.revision != expected_revision + 1:
            raise ValueError("revisions must be appended sequentially")
        self._revisions[(plan_id, new_revision.revision)] = new_revision
        self._plans[plan_id] = WeeklyPlan(
            plan.plan_id, plan.week_start, plan.timezone, new_revision.revision
        )
        if retry is not None:
            self._retries[(retry.plan_id, retry.retry_id)] = retry

    def get_retry(self, plan_id: str, retry_id: str) -> RetryRecord | None:
        return self._retries.get((plan_id, retry_id))

    def save_retry(self, retry: RetryRecord) -> None:
        self._retries[(retry.plan_id, retry.retry_id)] = retry

    def save_proposal(self, proposal: Proposal) -> None:
        self._proposals[proposal.proposal_id] = proposal

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        return self._proposals.get(proposal_id)

    def delete_proposal(self, proposal_id: str) -> None:
        self._proposals.pop(proposal_id, None)

    def current_snapshot(self, plan_id: str) -> PlanSnapshot | None:
        plan = self._plans.get(plan_id)
        if plan is None:
            return None
        return self._revisions[(plan_id, plan.current_revision)].snapshot

    def plan_ids(self) -> list[str]:
        return list(self._plans)


class InMemoryPlanReadAdapter:
    """Serves current revisions through the PlanReadRepository port."""

    def __init__(self, repository: InMemoryPlanRepository) -> None:
        self._repo = repository

    def list_plan_summaries(self) -> list[JsonObject]:
        snaps = (self._repo.current_snapshot(pid) for pid in self._repo.plan_ids())
        return [s.summary for s in snaps if s is not None]

    def get_plan(self, plan_id: str) -> JsonObject | None:
        snap = self._repo.current_snapshot(plan_id)
        return deepcopy(snap.plan) if snap else None

    def get_shopping_list(self, plan_id: str) -> JsonObject | None:
        snap = self._repo.current_snapshot(plan_id)
        return deepcopy(snap.shopping_list) if snap else None


class InMemoryShoppingTripRepository:
    """Reference implementation of ShoppingTripRepository (plan existence is the service's job)."""

    def __init__(self) -> None:
        self._trips: dict[str, ShoppingTrip] = {}
        self._owner: dict[str, str] = {}  # plan_id -> trip_id (the one-trip-per-week index)
        self._lock = threading.Lock()

    def _claim(self, trip: ShoppingTrip) -> None:
        for plan_id in trip.plan_ids:
            owner = self._owner.get(plan_id)
            if owner is not None and owner != trip.trip_id:
                raise WeekAlreadyOnTrip(plan_id, owner)

    def _release(self, trip_id: str) -> None:
        for plan_id in [p for p, t in self._owner.items() if t == trip_id]:
            del self._owner[plan_id]

    def _index(self, trip: ShoppingTrip) -> None:
        self._release(trip.trip_id)
        for plan_id in trip.plan_ids:
            self._owner[plan_id] = trip.trip_id

    def create(self, trip: ShoppingTrip) -> None:
        with self._lock:
            if trip.trip_id in self._trips:
                raise ValueError(f"trip {trip.trip_id} already exists")
            self._claim(trip)
            self._trips[trip.trip_id] = trip
            self._index(trip)

    def get(self, trip_id: str) -> ShoppingTrip | None:
        return self._trips.get(trip_id)

    def list(self) -> list[ShoppingTrip]:
        return sorted(self._trips.values(), key=lambda t: (t.created_at, t.trip_id))

    def save(self, trip: ShoppingTrip, expected_version: int) -> None:
        with self._lock:
            stored = self._trips.get(trip.trip_id)
            if stored is None:
                raise TripNotFound(trip.trip_id)
            if stored.version != expected_version:
                raise StaleVersion(expected_version, stored.version)
            self._claim(trip)
            self._trips[trip.trip_id] = trip
            self._index(trip)

    def delete(self, trip_id: str, expected_version: int) -> None:
        with self._lock:
            stored = self._trips.get(trip_id)
            if stored is None:
                raise TripNotFound(trip_id)
            if stored.version != expected_version:
                raise StaleVersion(expected_version, stored.version)
            del self._trips[trip_id]
            self._release(trip_id)


class InMemoryUserRepository:
    """Reference implementation of UserRepository."""

    def __init__(self) -> None:
        self._users: dict[str, User] = {}

    def add(self, user: User) -> None:
        if any(u.username == user.username for u in self._users.values()):
            raise UsernameTaken(f"username already exists: {user.username}")
        self._users[user.user_id] = user

    def get_by_username(self, username: str) -> User | None:
        return next((u for u in self._users.values() if u.username == username), None)

    def get_by_id(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    def list(self) -> list[User]:
        return list(self._users.values())

    def set_password_hash(self, user_id: str, password_hash: str) -> None:
        self._users[user_id] = replace(self._users[user_id], password_hash=password_hash)

    def set_disabled(self, user_id: str, disabled: bool) -> None:
        self._users[user_id] = replace(self._users[user_id], disabled=disabled)

    def delete(self, user_id: str) -> None:
        self._users.pop(user_id, None)


class InMemorySessionRepository:
    """Reference implementation of SessionRepository."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}

    def add(self, session: SessionRecord) -> None:
        self._sessions[session.session_hash] = session

    def get(self, session_hash: str) -> SessionRecord | None:
        return self._sessions.get(session_hash)

    def touch(self, session_hash: str, seen_at: datetime) -> None:
        if session_hash in self._sessions:
            self._sessions[session_hash] = replace(self._sessions[session_hash], last_seen_at=seen_at)

    def delete(self, session_hash: str) -> None:
        self._sessions.pop(session_hash, None)

    def delete_for_user(self, user_id: str) -> None:
        self._sessions = {h: s for h, s in self._sessions.items() if s.user_id != user_id}

    def delete_expired(self, now: datetime, idle_before: datetime) -> None:
        self._sessions = {
            h: s for h, s in self._sessions.items() if s.expires_at > now and s.last_seen_at >= idle_before
        }

    def count(self) -> int:
        return len(self._sessions)
