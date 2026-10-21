"""WeeklyPlan lifecycle: create, propose regeneration, accept, discard, undo.

Independent of Flask and storage. The solver runs outside any repository
transaction; commits go through the repository's atomic append_revision.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .generation import GeneratedPlan, generate
from .json_snapshot import normalize_snapshot
from .plan_domain import (
    DuplicateWeek,
    GenerationFailed,
    InputBasisChanged,
    InvalidWeek,
    MutationResult,
    NothingToUndo,
    PlanRevision,
    PlanWorkflowRepository,
    Proposal,
    ProposalExpired,
    ProposalNotFound,
    RetryIdReused,
    RetryRecord,
    StaleRevision,
    WeeklyPlan,
    WeekNotEditable,
    check_linked_pairs,
    content_equal,
    diff_snapshots,
    fingerprint,
    input_basis,
    stamp_revision,
)
from .planner_inputs import PlannerInputReader, PlannerInputs
from .read_api import InvalidRequest, PlanNotFound
from .snapshot import PlanSnapshot

Generator = Callable[[PlannerInputs, int, str | None], GeneratedPlan]
Clock = Callable[[], datetime]  # must return a timezone-aware datetime

DEFAULT_PROPOSAL_TTL = timedelta(minutes=30)


class PlanWorkflowService:
    """Application service for the WeeklyPlan lifecycle."""

    def __init__(
        self,
        repository: PlanWorkflowRepository,
        input_reader: PlannerInputReader,
        clock: Clock,
        generator: Generator = generate,
        timezone: str = "America/Bogota",
        proposal_ttl: timedelta = DEFAULT_PROPOSAL_TTL,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        try:
            self._tz = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InvalidWeek(f"unknown timezone {timezone!r}") from exc
        self._timezone = timezone
        self._repo = repository
        self._reader = input_reader
        self._clock = clock
        self._generator = generator
        self._ttl = proposal_ttl
        self._new_id = id_factory
        self._lock = threading.Lock()  # serialises check-then-write in this process

    # ------------------------------------------------------------ reads (never solve)

    def get_current(self, plan_id: str) -> PlanRevision:
        """Return the current revision. Never runs the solver."""
        return self._current(self._plan(plan_id))[1]

    def get_plan_for_week(self, week_start: date) -> WeeklyPlan | None:
        return self._repo.get_plan_by_week(week_start)

    # ------------------------------------------------------------ create

    def create_plan(self, week_start: date, seed: int = 42) -> PlanRevision:
        """Create revision 1 for a current or future week (one plan per week)."""
        if week_start.weekday() != 0:
            raise InvalidWeek("week_start must be a Monday")
        if self._repo.get_plan_by_week(week_start) is not None:
            raise DuplicateWeek(f"a plan already exists for week {week_start.isoformat()}")
        week_end = week_start + timedelta(days=6)
        if week_end < self._today():
            raise WeekNotEditable("a plan cannot be created for a week that has already ended")
        inputs = self._read_inputs()
        snapshot = self._solve(inputs, seed, week_start)
        now = self._clock()
        pantry = tuple(inputs.pantry)
        plan_id = snapshot.plan["plan_id"]
        revision = PlanRevision(
            plan_id=plan_id,
            revision=1,
            snapshot=stamp_revision(snapshot, 1, pantry),
            pantry=pantry,
            input_basis=input_basis(inputs),
            created_at=now,
            source="create",
        )
        with self._lock:
            self._repo.create_plan(
                WeeklyPlan(plan_id, week_start, self._timezone, 1), revision
            )  # raises DuplicateWeek on a race
        return revision

    # ------------------------------------------------------------ propose / discard

    def propose_regeneration(
        self, plan_id: str, expected_revision: int, seed: int | None = None
    ) -> Proposal:
        """Run the solver and store a preview. Never changes the current revision."""
        plan, current = self._current(self._plan(plan_id))
        self._check_expected(expected_revision, plan.current_revision)
        self._require_future_week(plan)
        if seed is None:
            seed = int(current.snapshot.plan["seed"]) + 1
        inputs = self._read_inputs()  # solver runs below, outside any transaction
        candidate = self._solve(inputs, seed, plan.week_start)
        pantry = tuple(inputs.pantry)
        changes = diff_snapshots(current.snapshot, candidate)
        # Identical content keeps the current token/revision; nothing new to stamp.
        stamped = (
            current.snapshot
            if not changes and content_equal(current.snapshot, candidate)
            else stamp_revision(candidate, plan.current_revision + 1, pantry)
        )
        now = self._clock()
        proposal = Proposal(
            proposal_id="prop_" + self._new_id(),
            plan_id=plan_id,
            base_revision=plan.current_revision,
            base_snapshot_id=current.snapshot.plan["snapshot_id"],
            input_basis=input_basis(inputs),
            seed=seed,
            created_at=now,
            expires_at=now + self._ttl,
            pantry=pantry,
            snapshot=stamped,
            changes=changes,
        )
        self._repo.save_proposal(proposal)
        return proposal

    def discard_proposal(self, proposal_id: str) -> None:
        """Drop a proposal. Creates no revision; unknown ids are an error."""
        if self._repo.get_proposal(proposal_id) is None:
            raise ProposalNotFound(proposal_id)
        self._repo.delete_proposal(proposal_id)

    # ------------------------------------------------------------ accept

    def accept_proposal(
        self, plan_id: str, proposal_id: str, expected_revision: int, retry_id: str
    ) -> MutationResult:
        """Commit a previewed proposal as exactly one new revision (never re-solves)."""
        proposal = self._repo.get_proposal(proposal_id)
        if proposal is not None and proposal.plan_id != plan_id:
            raise ProposalNotFound(proposal_id)
        fp = fingerprint("accept", proposal_id, expected_revision)
        with self._lock:
            replay = self._replay(plan_id, retry_id, fp)
            if replay is not None:
                return replay
            if proposal is None:
                raise ProposalNotFound(proposal_id)
            plan, current = self._current(self._plan(plan_id))
            self._check_expected(expected_revision, plan.current_revision)
            if proposal.base_revision != plan.current_revision:
                raise StaleRevision(proposal.base_revision, plan.current_revision)
            self._require_future_week(plan)
            if self._clock() >= proposal.expires_at:
                raise ProposalExpired(proposal_id)
            if input_basis(self._read_inputs()) != proposal.input_basis:
                raise InputBasisChanged("recipes, rules, or pantry changed since the preview")
            if proposal.is_identical:
                retry = RetryRecord(
                    plan_id, retry_id, fp, plan.current_revision, changed=False
                )
                self._repo.save_retry(retry)
                self._repo.delete_proposal(proposal_id)
                return MutationResult(
                    plan_id, plan.current_revision, current.snapshot.plan["snapshot_id"], False
                )
            check_linked_pairs(proposal.changes)
            self._check_stable_ids(current.snapshot, proposal.snapshot)
            return self._append(
                plan,
                proposal.snapshot,
                proposal.pantry,
                proposal.input_basis,
                "regenerate",
                retry_id,
                fp,
                proposal_id,
            )

    # ------------------------------------------------------------ undo

    def undo(self, plan_id: str, expected_revision: int, retry_id: str) -> MutationResult:
        """Append a revision restoring the content of revision N-1 (history is kept)."""
        fp = fingerprint("undo", plan_id, expected_revision)
        with self._lock:
            replay = self._replay(plan_id, retry_id, fp)
            if replay is not None:
                return replay
            plan = self._plan(plan_id)
            self._check_expected(expected_revision, plan.current_revision)
            if plan.current_revision < 2:
                raise NothingToUndo("revision 1 has no earlier revision")
            previous = self._repo.get_revision(plan_id, plan.current_revision - 1)
            assert previous is not None
            restored = stamp_revision(previous.snapshot, plan.current_revision + 1, previous.pantry)
            return self._append(
                plan, restored, previous.pantry, previous.input_basis, "undo", retry_id, fp, None
            )

    # ------------------------------------------------------------ internals

    def _append(
        self,
        plan: WeeklyPlan,
        snapshot: PlanSnapshot,
        pantry: tuple[str, ...],
        basis: str,
        source: str,
        retry_id: str,
        fp: str,
        proposal_id: str | None,
    ) -> MutationResult:
        new_rev = plan.current_revision + 1
        revision = PlanRevision(
            plan.plan_id, new_rev, snapshot, pantry, basis, self._clock(), source
        )
        retry = RetryRecord(plan.plan_id, retry_id, fp, new_rev, changed=True)
        self._repo.append_revision(plan.plan_id, plan.current_revision, revision, retry)
        if proposal_id is not None:
            self._repo.delete_proposal(proposal_id)
        return MutationResult(plan.plan_id, new_rev, snapshot.plan["snapshot_id"], True)

    def _replay(self, plan_id: str, retry_id: str, fp: str) -> MutationResult | None:
        if not retry_id:
            raise InvalidRequest("retry_id is required")
        record = self._repo.get_retry(plan_id, retry_id)
        if record is None:
            return None
        if record.fingerprint != fp:
            raise RetryIdReused(f"retry id {retry_id!r} was used for a different request")
        stored = self._repo.get_revision(plan_id, record.result_revision)
        assert stored is not None
        return MutationResult(
            plan_id,
            record.result_revision,
            stored.snapshot.plan["snapshot_id"],
            record.changed,
            replayed=True,
        )

    def _plan(self, plan_id: str) -> WeeklyPlan:
        plan = self._repo.get_plan(plan_id)
        if plan is None:
            raise PlanNotFound(plan_id)
        return plan

    def _current(self, plan: WeeklyPlan) -> tuple[WeeklyPlan, PlanRevision]:
        revision = self._repo.get_revision(plan.plan_id, plan.current_revision)
        assert revision is not None, "current revision pointer must reference a stored revision"
        return plan, revision

    @staticmethod
    def _check_expected(expected: int, actual: int) -> None:
        if expected != actual:
            raise StaleRevision(expected, actual)

    def _today(self) -> date:
        return self._clock().astimezone(self._tz).date()

    def _require_future_week(self, plan: WeeklyPlan) -> None:
        if plan.week_start <= self._today():
            raise WeekNotEditable("regeneration is only allowed for future weeks")

    def _read_inputs(self) -> PlannerInputs:
        return self._reader.read()

    def _solve(self, inputs: PlannerInputs, seed: int, week_start: date) -> PlanSnapshot:
        try:
            result = self._generator(inputs, seed, self._clock().isoformat())
            return normalize_snapshot(
                result.plan.model_dump(),
                result.shopping_list.model_dump(),
                set(inputs.pantry),
                week_start,
                self._timezone,
            )
        except Exception as exc:  # solver infeasible, normalisation invalid, ...
            raise GenerationFailed(f"plan generation failed: {exc}") from exc

    @staticmethod
    def _check_stable_ids(current: PlanSnapshot, proposed: PlanSnapshot) -> None:
        by_slot = {(s["date"], s["meal"]): s["assignment_id"] for s in current.plan["slots"]}
        for s in proposed.plan["slots"]:
            old = by_slot.get((s["date"], s["meal"]))
            if old is not None and old != s["assignment_id"]:
                raise GenerationFailed("assignment ids must stay stable across revisions")

