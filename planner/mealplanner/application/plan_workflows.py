"""WeeklyPlan lifecycle: create, propose regeneration, accept, discard, undo.

Independent of Flask and storage. The solver runs outside any repository
transaction; commits go through the repository's atomic append_revision.
"""

from __future__ import annotations

import logging
import secrets
import threading
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.month import MonthFillResult, WeekFillOutcome, parse_month, week_seed, weeks_of_month
from ..domain.planner_inputs import PlannerInputs
from ..domain.snapshot import JsonObject, PlanSnapshot, normalize_snapshot
from ..domain.shopping_trip import DONE
from ..domain.swap import (
    SCOPES,
    SlotNotEditable,
    SwapOutcome,
    SwapPreview,
    SwapVariant,
    VariantNotFound,
    apply_swap,
    catalog_variants,
    extra_needed,
    find_slot,
    grocery_delta,
    list_swap_variants,
    swapped_assignment_ids,
)
from ..domain.weekly_rules import check_weekly_rules
from ..domain.weekly_plan import (
    DuplicateWeek,
    GenerationFailed,
    InputBasisChanged,
    InvalidWeek,
    MutationResult,
    NothingToUndo,
    PlanRevision,
    Proposal,
    ProposalExpired,
    ProposalNotFound,
    RetryIdReused,
    RetryRecord,
    RuleViolationError,
    SlotChange,
    StaleRevision,
    WeeklyPlan,
    WeekNotEditable,
    WorkflowError,
    check_linked_pairs,
    content_equal,
    diff_snapshots,
    fingerprint,
    input_basis,
    require_weekly_rules,
    stamp_revision,
)
from .errors import InvalidRequest, PlanNotFound
from .generation import generate
from .ports import PlanGenerator, PlannerInputReader, PlanWorkflowRepository, ShoppingTripRepository

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]  # must return a timezone-aware datetime

DEFAULT_PROPOSAL_TTL = timedelta(minutes=30)

# Error codes recorded for a week that failed during fill_month (same codes as the HTTP API).
FILL_ERROR_CODES: tuple[tuple[type[Exception], str], ...] = (
    (RuleViolationError, "rule_violation"),
    (GenerationFailed, "generation_failed"),
    (WeekNotEditable, "week_not_editable"),
    (InvalidWeek, "invalid_week"),
)


class PlanWorkflowService:
    """Application service for the WeeklyPlan lifecycle."""

    def __init__(
        self,
        repository: PlanWorkflowRepository,
        input_reader: PlannerInputReader,
        clock: Clock,
        generator: PlanGenerator,
        timezone: str = "America/Bogota",
        proposal_ttl: timedelta = DEFAULT_PROPOSAL_TTL,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        seed_factory: Callable[[], int] = lambda: secrets.randbelow(2**31),
        trips: ShoppingTripRepository | None = None,
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
        self._new_seed = seed_factory
        self._trips = trips  # read-only: only used to report shopping trip impact of a swap
        self._lock = threading.Lock()  # serialises check-then-write in this process

    # ------------------------------------------------------------ reads (never solve)

    def get_current(self, plan_id: str) -> PlanRevision:
        """Return the current revision. Never runs the solver."""
        return self._current(self._plan(plan_id))[1]

    def get_plan_for_week(self, week_start: date) -> WeeklyPlan | None:
        return self._repo.get_plan_by_week(week_start)

    def list_revisions(self, plan_id: str) -> list[PlanRevision]:
        """Return the full revision history, oldest first. Never runs the solver."""
        self._plan(plan_id)
        return self._repo.list_revisions(plan_id)

    def get_proposal(self, plan_id: str, proposal_id: str) -> Proposal:
        """Return a live proposal for this plan; unknown or foreign ids are not found."""
        proposal = self._repo.get_proposal(proposal_id)
        if proposal is None or proposal.plan_id != plan_id:
            raise ProposalNotFound(proposal_id)
        if self._clock() >= proposal.expires_at:
            raise ProposalExpired(proposal_id)
        return proposal

    # ------------------------------------------------------------ create

    def create_plan(self, week_start: date, seed: int | None = None) -> PlanRevision:
        """Create revision 1 for a current or future week (one plan per week).

        Without a seed the week gets `week_seed(week_start)`, the same seed a month fill
        uses, so different weeks get different plans.
        """
        if seed is None:
            seed = week_seed(week_start)
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

    # ------------------------------------------------------------ fill month

    def fill_month(self, month: str) -> MonthFillResult:
        """Create the missing weekly plans for every week overlapping `month`.

        A batch of independent create_plan calls: no monthly or cross-week rules. Past
        weeks are skipped, existing weeks are never overwritten or regenerated, and a
        failing week is recorded without stopping the rest. Each created week commits
        on its own with seed `week_seed(week_start)`.
        """
        try:
            parse_month(month)
            starts = weeks_of_month(month)
        except (ValueError, OverflowError) as exc:
            raise InvalidRequest("month must be a valid YYYY-MM") from exc
        outcomes = [self._fill_week(start) for start in starts]
        return MonthFillResult(month, tuple(outcomes))

    def _fill_week(self, week_start: date) -> WeekFillOutcome:
        week_end = week_start + timedelta(days=6)

        def outcome(status: str, **fields) -> WeekFillOutcome:
            return WeekFillOutcome(week_start, week_end, status, **fields)

        def existing() -> WeekFillOutcome:
            plan = self._repo.get_plan_by_week(week_start)
            assert plan is not None
            return outcome("exists", plan_id=plan.plan_id, revision=plan.current_revision)

        if week_end < self._today():
            return outcome("skipped_past")
        if self._repo.get_plan_by_week(week_start) is not None:
            return existing()
        try:
            revision = self.create_plan(week_start, seed=week_seed(week_start))
        except DuplicateWeek:  # lost a race with another writer: the week now exists
            return existing()
        except WorkflowError as exc:
            code = next((c for cls, c in FILL_ERROR_CODES if isinstance(exc, cls)), "generation_failed")
            return outcome("failed", error_code=code, error_message=str(exc) or code)
        return outcome("created", plan_id=revision.plan_id, revision=revision.revision)

    # ------------------------------------------------------------ propose / discard

    def propose_regeneration(
        self, plan_id: str, expected_revision: int, seed: int | None = None
    ) -> Proposal:
        """Run the solver and store a preview. Never changes the current revision."""
        plan, current = self._current(self._plan(plan_id))
        self._check_expected(expected_revision, plan.current_revision)
        self._require_future_week(plan)
        if seed is None:
            # A fresh seed per click, never the current one; the proposal records it.
            seed = self._new_seed()
            while seed == int(current.snapshot.plan["seed"]):
                seed = self._new_seed()
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
            replaced_swaps=self._replaced_swaps(plan_id, changes),
        )
        self._repo.save_proposal(proposal)
        return proposal

    def discard_proposal(self, proposal_id: str, plan_id: str | None = None) -> None:
        """Drop a proposal (optionally scoped to a plan). Creates no revision."""
        proposal = self._repo.get_proposal(proposal_id)
        if proposal is None or (plan_id is not None and proposal.plan_id != plan_id):
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
            inputs = self._read_inputs()
            if input_basis(inputs) != proposal.input_basis:
                raise InputBasisChanged("recipes, rules, or pantry changed since the preview")
            require_weekly_rules(proposal.snapshot, inputs)
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
            self._warn_if_rule_breaking(previous, restored)
            return self._append(
                plan, restored, previous.pantry, previous.input_basis, "undo", retry_id, fp, None
            )

    # ------------------------------------------------------------ swaps (phase 5)

    def list_swap_variants(self) -> list[SwapVariant]:
        """Every catalog variant a slot can be swapped to (current planner inputs)."""
        return list_swap_variants(self._read_inputs())

    def preview_swap(
        self, plan_id: str, slot_id: str, variant_id: str, scope: str, expected_revision: int
    ) -> SwapPreview:
        """Stateless: what a swap would change. Stores nothing and never blocks on rules."""
        plan, current = self._current(self._plan(plan_id))
        self._check_expected(expected_revision, plan.current_revision)
        inputs = self._read_inputs()
        candidate = self._swapped(plan, current, slot_id, variant_id, scope, inputs)
        return SwapPreview(
            plan_id=plan_id,
            base_revision=plan.current_revision,
            changes=diff_snapshots(current.snapshot, candidate),
            grocery_delta=tuple(
                grocery_delta(current.snapshot.shopping_list, candidate.shopping_list)
            ),
            rule_warnings=self._rule_warnings(candidate, inputs),
            trips=self._trip_impact(plan_id, candidate, inputs),
        )

    def swap(
        self,
        plan_id: str,
        slot_id: str,
        variant_id: str,
        scope: str,
        expected_revision: int,
        retry_id: str,
    ) -> SwapOutcome:
        """Append exactly one revision with the swap (or none when it changes nothing)."""
        fp = fingerprint("swap", plan_id, slot_id, variant_id, scope, expected_revision)
        with self._lock:
            replay = self._replay(plan_id, retry_id, fp)
            if replay is not None:
                stored = self._repo.get_revision(plan_id, replay.revision)
                assert stored is not None
                return SwapOutcome(
                    replay, stored.snapshot, self._rule_warnings(stored.snapshot, self._read_inputs())
                )
            plan, current = self._current(self._plan(plan_id))
            self._check_expected(expected_revision, plan.current_revision)
            inputs = self._read_inputs()
            candidate = self._swapped(plan, current, slot_id, variant_id, scope, inputs)
            if candidate is current.snapshot:
                self._repo.save_retry(
                    RetryRecord(plan_id, retry_id, fp, plan.current_revision, changed=False)
                )
                result = MutationResult(
                    plan_id, plan.current_revision, current.snapshot.plan["snapshot_id"], False
                )
            else:
                result = self._append(
                    plan, candidate, current.pantry, input_basis(inputs), "swap", retry_id, fp, None
                )
            return SwapOutcome(result, candidate, self._rule_warnings(candidate, inputs))

    def _swapped(
        self,
        plan: WeeklyPlan,
        current: PlanRevision,
        slot_id: str,
        variant_id: str,
        scope: str,
        inputs: PlannerInputs,
    ) -> PlanSnapshot:
        """The validated swap candidate stamped as the next revision, or the current snapshot
        itself when the swap changes nothing."""
        if scope not in SCOPES:
            raise InvalidRequest(f"scope must be one of {', '.join(SCOPES)}")
        slot = find_slot(current.snapshot, slot_id)  # SlotNotFound
        variant = catalog_variants(inputs).get(variant_id)
        if variant is None:
            raise VariantNotFound(variant_id)
        if date.fromisoformat(slot["date"]) < self._today():
            raise SlotNotEditable("meals before today cannot be swapped")
        swapped = apply_swap(current.snapshot, slot_id, variant, scope, inputs, current.pantry)
        if content_equal(current.snapshot, swapped):
            return current.snapshot
        # Groceries use the base revision's captured pantry, never the current one.
        return stamp_revision(swapped, plan.current_revision + 1, current.pantry)

    @staticmethod
    def _rule_warnings(snapshot: PlanSnapshot, inputs: PlannerInputs) -> tuple[JsonObject, ...]:
        return tuple(
            {"rule": v.rule, "message": v.message} for v in check_weekly_rules(snapshot, inputs)
        )

    def _trip_impact(
        self, plan_id: str, swapped: PlanSnapshot, inputs: PlannerInputs
    ) -> tuple[JsonObject, ...]:
        """Every shopping list holding the week, with the groups it covers; a done list also
        lists what is newly needed, limited to the ingredients of its groups."""
        if self._trips is None:
            return ()
        labels = inputs.shelf_lives()
        impact: list[JsonObject] = []
        for trip in self._trips.list():
            week = next((w for w in trip.weeks if w.plan_id == plan_id), None)
            if week is None:
                continue
            extra: list[JsonObject] = []
            if trip.status == DONE and week.covered_revision is not None:
                covered = self._repo.get_revision(plan_id, week.covered_revision)
                if covered is not None:
                    extra = extra_needed(
                        covered.snapshot.shopping_list,
                        swapped.shopping_list,
                        lambda ingredient_id, groups=week.groups: labels.get(ingredient_id) in groups
                        or ingredient_id not in labels,
                    )
            impact.append(
                {
                    "trip_id": trip.trip_id,
                    "name": trip.display_name,
                    "status": trip.status,
                    "groups": list(week.groups),
                    "extra_needed": extra,
                }
            )
        return tuple(impact)

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

    def _warn_if_rule_breaking(self, previous: PlanRevision, restored: PlanSnapshot) -> None:
        """Undo restores historical content, which may be a swapped week: weekly rules are
        advisory there (phase 5), so violations are only logged and never refuse the undo."""
        violations = check_weekly_rules(restored, self._read_inputs())
        if violations:
            logger.warning(
                "undo of %s restores content that breaks weekly rules: %s",
                previous.plan_id,
                "; ".join(f"{v.rule}: {v.message}" for v in violations),
            )

    def _replaced_swaps(self, plan_id: str, changes: tuple[SlotChange, ...]) -> int:
        """How many hand-swapped slots of the current revision the changes would replace."""
        swapped = swapped_assignment_ids(self._repo.list_revisions(plan_id))
        return sum(1 for c in changes if c.assignment_id in swapped)

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
            result = generate(inputs, self._generator, seed, self._clock().isoformat())
            snapshot = normalize_snapshot(
                result.plan.model_dump(),
                result.shopping_list.model_dump(),
                set(inputs.pantry),
                week_start,
                self._timezone,
            )
        except Exception as exc:  # solver infeasible, normalisation invalid, ...
            raise GenerationFailed(f"plan generation failed: {exc}") from exc
        require_weekly_rules(snapshot, inputs)  # a violating candidate is never stored
        return snapshot

    @staticmethod
    def _check_stable_ids(current: PlanSnapshot, proposed: PlanSnapshot) -> None:
        by_slot = {(s["date"], s["meal"]): s["assignment_id"] for s in current.plan["slots"]}
        for s in proposed.plan["slots"]:
            old = by_slot.get((s["date"], s["meal"]))
            if old is not None and old != s["assignment_id"]:
                raise GenerationFailed("assignment ids must stay stable across revisions")

