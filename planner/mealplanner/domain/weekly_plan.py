"""Domain records, errors, and pure helpers for the WeeklyPlan lifecycle."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .planner_inputs import PlannerInputs
from .snapshot import JsonObject, PlanSnapshot, canonical_snapshot_id, validate_snapshot

# ---------------------------------------------------------------- errors


class WorkflowError(Exception):
    """Base class for expected plan lifecycle failures."""


class DuplicateWeek(WorkflowError):
    """A plan already exists for that household week."""


class InvalidWeek(WorkflowError):
    """The week start is not a Monday or the timezone is unknown."""


class WeekNotEditable(WorkflowError):
    """The week's start date is not allowed for this action (started-week policy)."""


class StaleRevision(WorkflowError):
    """The caller's expected revision is no longer the current revision."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"expected revision {expected} but current revision is {actual}")
        self.expected = expected
        self.actual = actual


class GenerationFailed(WorkflowError):
    """The solver could not produce a valid plan; nothing was stored."""


class ProposalNotFound(WorkflowError):
    """The proposal was discarded, already accepted, or never existed."""


class ProposalExpired(WorkflowError):
    """The proposal is older than its time-to-live and cannot be accepted."""


class InputBasisChanged(WorkflowError):
    """Recipes, rules, or pantry changed since the proposal was previewed."""


class RetryIdReused(WorkflowError):
    """A retry id was reused for a different request."""


class NothingToUndo(WorkflowError):
    """Revision 1 has no earlier revision to restore."""


class LinkedPairSplit(WorkflowError):
    """A change touches one half of a linked lunch/dinner pair but not the other."""


# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class PlanRevision:
    """Immutable full snapshot of one accepted revision plus its calculation context."""

    plan_id: str
    revision: int
    snapshot: PlanSnapshot
    pantry: tuple[str, ...]
    input_basis: str
    created_at: datetime
    source: str  # "create" | "regenerate" | "undo"


@dataclass(frozen=True)
class WeeklyPlan:
    """Aggregate header: one per household week, with a current-revision pointer."""

    plan_id: str
    week_start: date
    timezone: str
    current_revision: int


@dataclass(frozen=True)
class SlotChange:
    """One per-assignment difference between the current and proposed snapshot."""

    assignment_id: str
    date: str
    meal: str
    kind: str  # "changed" | "added" | "removed"
    before: JsonObject | None
    after: JsonObject | None


@dataclass(frozen=True)
class Proposal:
    """Previewed regeneration based on a particular current revision."""

    proposal_id: str
    plan_id: str
    base_revision: int
    base_snapshot_id: str
    input_basis: str
    seed: int
    created_at: datetime
    expires_at: datetime
    pantry: tuple[str, ...]
    snapshot: PlanSnapshot  # complete candidate content, already stamped base_revision + 1
    changes: tuple[SlotChange, ...]

    @property
    def is_identical(self) -> bool:
        return not self.changes


@dataclass(frozen=True)
class RetryRecord:
    """Persisted outcome of a retry-identified mutation."""

    plan_id: str
    retry_id: str
    fingerprint: str
    result_revision: int
    changed: bool


@dataclass(frozen=True)
class MutationResult:
    """Outcome of accept or undo; revision is the (possibly unchanged) current one."""

    plan_id: str
    revision: int
    snapshot_id: str
    changed: bool
    replayed: bool = field(default=False, compare=False)


# ---------------------------------------------------------------- pure helpers


def fingerprint(*parts: object) -> str:
    """Stable digest identifying a request, used to detect retry-id misuse."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def input_basis(inputs: PlannerInputs) -> str:
    """Digest of the recipe, ingredient, rule, and pantry inputs a result depends on."""
    payload: dict[str, Any] = {
        "recipes": {k: v.model_dump(mode="json") for k, v in sorted(inputs.recipes.items())},
        "ingredients": {
            k: v.model_dump(mode="json") for k, v in sorted(inputs.ingredients.items())
        },
        "rules": inputs.rules.model_dump(mode="json"),
        "pantry": sorted(inputs.pantry),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def stamp_revision(snapshot: PlanSnapshot, revision: int, pantry: tuple[str, ...]) -> PlanSnapshot:
    """Copy a snapshot under a new revision number, recompute its token, and validate."""
    plan = deepcopy(snapshot.plan)
    shopping = deepcopy(snapshot.shopping_list)
    plan["revision"] = shopping["revision"] = revision
    token = canonical_snapshot_id(plan, shopping)
    plan["snapshot_id"] = shopping["snapshot_id"] = token
    validate_snapshot(plan, shopping, set(pantry))
    return PlanSnapshot(plan=plan, shopping_list=shopping)


def content_equal(a: PlanSnapshot, b: PlanSnapshot) -> bool:
    """True when assignments, derived counts, and groceries match (ignores revision,
    seed, generated_at, and snapshot token)."""
    return (
        a.plan["slots"] == b.plan["slots"]
        and a.plan["derived"] == b.plan["derived"]
        and a.shopping_list["sections"] == b.shopping_list["sections"]
    )


def diff_snapshots(before: PlanSnapshot, after: PlanSnapshot) -> tuple[SlotChange, ...]:
    """Per-slot differences keyed by assignment_id, ordered by date then meal."""
    old = {s["assignment_id"]: s for s in before.plan["slots"]}
    new = {s["assignment_id"]: s for s in after.plan["slots"]}
    changes: list[SlotChange] = []
    for aid in old.keys() | new.keys():
        o, n = old.get(aid), new.get(aid)
        if o == n:
            continue
        ref = n or o
        assert ref is not None
        kind = "added" if o is None else "removed" if n is None else "changed"
        changes.append(
            SlotChange(aid, ref["date"], ref["meal"], kind, deepcopy(o), deepcopy(n))
        )
    changes.sort(key=lambda c: (c.date, 0 if c.meal == "lunch" else 1))
    return tuple(changes)


def check_linked_pairs(changes: tuple[SlotChange, ...]) -> None:
    """A linked lunch/dinner pair must change its recipe or link together.

    A change to only one half's carb variant is allowed (the partner keeps its own
    variant); a base-recipe or link change must touch both halves in the same revision.
    """
    changed = {c.assignment_id for c in changes}
    for c in changes:
        before, after = c.before or {}, c.after or {}
        if (
            before.get("recipe_id") == after.get("recipe_id")
            and before.get("linked_assignment_id") == after.get("linked_assignment_id")
        ):
            continue
        for side in (c.before, c.after):
            partner = side.get("linked_assignment_id") if side else None
            if partner is not None and partner not in changed:
                raise LinkedPairSplit(
                    f"{c.assignment_id} is linked to {partner}, which is not in the same change"
                )
