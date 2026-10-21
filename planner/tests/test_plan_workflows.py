"""Lifecycle tests for the WeeklyPlan services, using in-memory storage."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from mealplanner.adapters.memory import InMemoryPlanReadAdapter, InMemoryPlanRepository
from mealplanner.adapters.ortools import OrToolsPlanGenerator
from mealplanner.domain.solution import MealPlanSolution
from mealplanner.domain.weekly_plan import (
    DuplicateWeek,
    GenerationFailed,
    InputBasisChanged,
    InvalidWeek,
    LinkedPairSplit,
    NothingToUndo,
    ProposalExpired,
    ProposalNotFound,
    RetryIdReused,
    StaleRevision,
    WeekNotEditable,
    check_linked_pairs,
    diff_snapshots,
)
from mealplanner.application.plan_workflows import PlanWorkflowService
from mealplanner.domain.planner_inputs import PlannerInputs
from mealplanner.application.plan_reads import PlanReadService
from mealplanner.domain.snapshot import validate_snapshot
from mealplanner.adapters.sqlite.store import SqlitePlanReadAdapter, SqlitePlanRepository
from mealplanner.adapters.yaml_inputs import YamlPlannerInputReader

ROOT = Path(__file__).resolve().parents[2]
INPUTS: PlannerInputs = YamlPlannerInputReader(ROOT / "data").read()
FUTURE = date(2026, 10, 26)
CURRENT = date(2026, 10, 19)
PAST = date(2026, 10, 12)
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)  # Wednesday in Bogota

_cache: dict[int, MealPlanSolution] = {}


def real(inputs: PlannerInputs, seed: int) -> MealPlanSolution:
    if seed not in _cache:
        _cache[seed] = OrToolsPlanGenerator().generate(inputs, seed)
    return _cache[seed]


class Reader:
    def __init__(self, inputs: PlannerInputs = INPUTS) -> None:
        self.inputs = inputs

    def read(self) -> PlannerInputs:
        return self.inputs


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Calls:
    """Stub solver: counts calls, optionally fails, optionally pins the result."""

    def __init__(self) -> None:
        self.count = 0
        self.fail = False
        self.pin: int | None = None

    def generate(self, inputs: PlannerInputs, seed: int) -> MealPlanSolution:
        self.count += 1
        if self.fail:
            raise RuntimeError("infeasible")
        return real(inputs, self.pin if self.pin is not None else seed)


def make_repo(kind: str, tmp_path: Path):
    """Return (repository, read adapter) for the "memory" or "sqlite" backend."""
    if kind == "memory":
        repo = InMemoryPlanRepository()
        return repo, InMemoryPlanReadAdapter(repo)
    repo = SqlitePlanRepository(tmp_path / "plans.db")
    return repo, SqlitePlanReadAdapter(tmp_path / "plans.db")


def proposal_count(repo) -> int:
    return len(repo._proposals) if isinstance(repo, InMemoryPlanRepository) else repo.count_proposals()


class Env:
    def __init__(self, kind: str = "memory", tmp_path: Path | None = None) -> None:
        self.repo, self.read_adapter = make_repo(kind, tmp_path)
        self.clock = Clock()
        self.solver = Calls()
        self.reader = Reader()
        self.svc = PlanWorkflowService(
            self.repo, self.reader, self.clock, generator=self.solver, proposal_ttl=timedelta(minutes=10)
        )


@pytest.fixture(params=["memory", "sqlite"])
def env(request, tmp_path) -> Env:
    """Every lifecycle test runs against both repository implementations."""
    return Env(request.param, tmp_path)


def created(env: Env, seed: int = 1):
    return env.svc.create_plan(FUTURE, seed=seed)


# ---- create / read


def test_create_revision_one_valid_and_readable(env: Env) -> None:
    rev = created(env)
    assert rev.revision == 1 and rev.source == "create"
    plan = rev.snapshot.plan
    assert plan["plan_id"] == "plan_2026_10_26" and plan["revision"] == 1
    validate_snapshot(plan, rev.snapshot.shopping_list, set(rev.pantry))
    assert rev.pantry == tuple(INPUTS.pantry) and rev.input_basis.startswith("sha256:")
    read = PlanReadService(env.read_adapter)
    assert read.get_plan(plan["plan_id"])["snapshot_id"] == plan["snapshot_id"]
    listed = read.list_plans(FUTURE, FUTURE)["items"]
    assert [i["plan_id"] for i in listed] == [plan["plan_id"]]
    sh = read.get_shopping_list(plan["plan_id"], 1, plan["snapshot_id"])
    assert sh["revision"] == 1


def test_duplicate_week_rejected(env: Env) -> None:
    created(env)
    with pytest.raises(DuplicateWeek):
        created(env)
    with pytest.raises(DuplicateWeek):  # storage-level uniqueness too
        wp = env.repo.get_plan_by_week(FUTURE)
        env.repo.create_plan(wp, env.repo.get_revision(wp.plan_id, 1))


def test_create_week_rules(env: Env) -> None:
    with pytest.raises(InvalidWeek):
        env.svc.create_plan(date(2026, 10, 27))
    with pytest.raises(WeekNotEditable):
        env.svc.create_plan(PAST)
    assert env.svc.create_plan(CURRENT).revision == 1  # current week allowed


def test_reads_never_run_solver(env: Env) -> None:
    rev = created(env)
    calls = env.solver.count
    env.svc.get_current(rev.plan_id)
    env.svc.get_plan_for_week(FUTURE)
    PlanReadService(env.read_adapter).get_plan(rev.plan_id)
    assert env.solver.count == calls


# ---- propose / discard / expiry / failure


def test_propose_leaves_current_unchanged(env: Env) -> None:
    rev = created(env)
    p = env.svc.propose_regeneration(rev.plan_id, 1, seed=2)
    assert p.base_revision == 1 and p.base_snapshot_id == rev.snapshot.plan["snapshot_id"]
    assert p.input_basis == rev.input_basis and p.seed == 2
    assert p.changes and p.snapshot.plan["revision"] == 2
    assert {c.kind for c in p.changes} == {"changed"}
    assert env.repo.get_plan(rev.plan_id).current_revision == 1
    assert env.repo.get_revision(rev.plan_id, 2) is None
    assert env.svc.get_current(rev.plan_id).snapshot == rev.snapshot


def test_propose_stale_expected_revision(env: Env) -> None:
    rev = created(env)
    with pytest.raises(StaleRevision):
        env.svc.propose_regeneration(rev.plan_id, 5)


def test_discard_creates_no_revision(env: Env) -> None:
    rev = created(env)
    p = env.svc.propose_regeneration(rev.plan_id, 1, seed=2)
    env.svc.discard_proposal(p.proposal_id)
    assert env.repo.get_proposal(p.proposal_id) is None
    assert env.repo.get_plan(rev.plan_id).current_revision == 1
    with pytest.raises(ProposalNotFound):
        env.svc.accept_proposal(rev.plan_id, p.proposal_id, 1, "r1")
    with pytest.raises(ProposalNotFound):
        env.svc.discard_proposal(p.proposal_id)


def test_expired_proposal_cannot_be_accepted(env: Env) -> None:
    rev = created(env)
    p = env.svc.propose_regeneration(rev.plan_id, 1, seed=2)
    env.clock.now = NOW + timedelta(minutes=10)
    with pytest.raises(ProposalExpired):
        env.svc.accept_proposal(rev.plan_id, p.proposal_id, 1, "r1")
    assert env.repo.get_plan(rev.plan_id).current_revision == 1


def test_solver_failure_creates_no_proposal_or_revision(env: Env) -> None:
    rev = created(env)
    env.solver.fail = True
    with pytest.raises(GenerationFailed):
        env.svc.propose_regeneration(rev.plan_id, 1, seed=2)
    assert proposal_count(env.repo) == 0
    assert env.repo.get_plan(rev.plan_id).current_revision == 1


def test_create_solver_failure_stores_nothing(env: Env) -> None:
    env.solver.fail = True
    with pytest.raises(GenerationFailed):
        created(env)
    assert env.repo.get_plan_by_week(FUTURE) is None


def test_identical_result_accept_is_noop(env: Env) -> None:
    rev = created(env, seed=1)
    env.solver.pin = 1  # same assignments, different seed
    p = env.svc.propose_regeneration(rev.plan_id, 1, seed=99)
    assert p.is_identical and p.changes == ()
    res = env.svc.accept_proposal(rev.plan_id, p.proposal_id, 1, "r1")
    assert res.changed is False and res.revision == 1
    assert res.snapshot_id == rev.snapshot.plan["snapshot_id"]
    assert env.repo.get_revision(rev.plan_id, 2) is None
    again = env.svc.accept_proposal(rev.plan_id, p.proposal_id, 1, "r1")
    assert again.replayed and again.revision == 1


# ---- accept


def test_accept_appends_exactly_one_revision_without_solving(env: Env) -> None:
    rev = created(env)
    p = env.svc.propose_regeneration(rev.plan_id, 1, seed=2)
    calls = env.solver.count
    res = env.svc.accept_proposal(rev.plan_id, p.proposal_id, 1, "r1")
    assert env.solver.count == calls
    assert res.changed and res.revision == 2
    cur = env.svc.get_current(rev.plan_id)
    assert cur.revision == 2 and cur.source == "regenerate"
    assert cur.snapshot == p.snapshot  # exactly the previewed content
    assert env.repo.get_revision(rev.plan_id, 1).snapshot == rev.snapshot  # immutable
    assert env.repo.get_revision(rev.plan_id, 3) is None
    assert cur.snapshot.shopping_list["revision"] == 2
    assert env.repo.get_proposal(p.proposal_id) is None
    validate_snapshot(cur.snapshot.plan, cur.snapshot.shopping_list, set(cur.pantry))


def test_stale_base_rejected(env: Env) -> None:
    rev = created(env)
    p1 = env.svc.propose_regeneration(rev.plan_id, 1, seed=2)
    p2 = env.svc.propose_regeneration(rev.plan_id, 1, seed=3)
    env.svc.accept_proposal(rev.plan_id, p1.proposal_id, 1, "a")
    with pytest.raises(StaleRevision):
        env.svc.accept_proposal(rev.plan_id, p2.proposal_id, 2, "b")
    assert env.repo.get_plan(rev.plan_id).current_revision == 2


def test_expected_revision_mismatch(env: Env) -> None:
    rev = created(env)
    p = env.svc.propose_regeneration(rev.plan_id, 1, seed=2)
    with pytest.raises(StaleRevision) as exc:
        env.svc.accept_proposal(rev.plan_id, p.proposal_id, 7, "r1")
    assert (exc.value.expected, exc.value.actual) == (7, 1)
    with pytest.raises(StaleRevision):
        env.svc.undo(rev.plan_id, 3, "u1")


def test_input_basis_change_rejected(env: Env) -> None:
    rev = created(env)
    p = env.svc.propose_regeneration(rev.plan_id, 1, seed=2)
    env.reader.inputs = PlannerInputs(
        recipes=INPUTS.recipes,
        ingredients=INPUTS.ingredients,
        rules=INPUTS.rules,
        pantry=tuple(INPUTS.pantry[:-1]),
    )
    with pytest.raises(InputBasisChanged):
        env.svc.accept_proposal(rev.plan_id, p.proposal_id, 1, "r1")
    assert env.repo.get_plan(rev.plan_id).current_revision == 1


# ---- idempotency


def test_retry_same_request_is_idempotent(env: Env) -> None:
    rev = created(env)
    p = env.svc.propose_regeneration(rev.plan_id, 1, seed=2)
    first = env.svc.accept_proposal(rev.plan_id, p.proposal_id, 1, "r1")
    second = env.svc.accept_proposal(rev.plan_id, p.proposal_id, 1, "r1")
    assert second.replayed and not first.replayed
    assert (second.revision, second.snapshot_id) == (first.revision, first.snapshot_id)
    assert env.repo.get_revision(rev.plan_id, 3) is None


def test_retry_id_reuse_for_different_request(env: Env) -> None:
    rev = created(env)
    p = env.svc.propose_regeneration(rev.plan_id, 1, seed=2)
    env.svc.accept_proposal(rev.plan_id, p.proposal_id, 1, "r1")
    with pytest.raises(RetryIdReused):
        env.svc.undo(rev.plan_id, 2, "r1")
    q = env.svc.propose_regeneration(rev.plan_id, 2, seed=3)
    with pytest.raises(RetryIdReused):
        env.svc.accept_proposal(rev.plan_id, q.proposal_id, 2, "r1")
    assert env.repo.get_plan(rev.plan_id).current_revision == 2


# ---- started-week policy


def test_regeneration_only_for_future_weeks(env: Env) -> None:
    cur = env.svc.create_plan(CURRENT, seed=1)
    with pytest.raises(WeekNotEditable):
        env.svc.propose_regeneration(cur.plan_id, 1, seed=2)
    # a future week becomes started by time passing: cannot accept either
    fut = created(env)
    p = env.svc.propose_regeneration(fut.plan_id, 1, seed=2)
    env.clock.now = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)  # Mon 01:00 Bogota
    with pytest.raises(WeekNotEditable):
        env.svc.accept_proposal(fut.plan_id, p.proposal_id, 1, "r1")
    # past weeks (injected directly) are read-only too
    env.clock.now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    with pytest.raises(WeekNotEditable):
        env.svc.propose_regeneration(cur.plan_id, 1, seed=2)


def test_future_boundary_uses_household_timezone(env: Env) -> None:
    rev = created(env)
    # Sunday 23:00 Bogota is already Monday UTC; the week is still in the future.
    env.clock.now = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)
    env.svc.propose_regeneration(rev.plan_id, 1, seed=2)


# ---- stable ids, linked pairs, undo, snapshot ids


def test_assignment_ids_stable_across_revisions(env: Env) -> None:
    rev = created(env)
    p = env.svc.propose_regeneration(rev.plan_id, 1, seed=2)
    env.svc.accept_proposal(rev.plan_id, p.proposal_id, 1, "r1")
    cur = env.svc.get_current(rev.plan_id)
    ids = lambda r: {(s["date"], s["meal"]): s["assignment_id"] for s in r.snapshot.plan["slots"]}
    assert ids(cur) == ids(rev)
    assert all(
        s["assignment_id"] == f"slot_{s['date']}_{s['meal']}" for s in cur.snapshot.plan["slots"]
    )


def test_linked_pair_changes_atomically(env: Env) -> None:
    rev = created(env, seed=1)
    p = env.svc.propose_regeneration(rev.plan_id, 1, seed=2)
    by_id = {c.assignment_id: c for c in p.changes}
    # 2026-10-27 lunch+dinner share a base recipe and both change in one proposal
    lunch, dinner = by_id["slot_2026-10-27_lunch"], by_id["slot_2026-10-27_dinner"]
    assert lunch.after["linked_assignment_id"] == dinner.assignment_id
    env.svc.accept_proposal(rev.plan_id, p.proposal_id, 1, "r1")
    slots = {s["assignment_id"]: s for s in env.svc.get_current(rev.plan_id).snapshot.plan["slots"]}
    assert slots["slot_2026-10-27_lunch"]["recipe_id"] == slots["slot_2026-10-27_dinner"]["recipe_id"]
    assert slots["slot_2026-10-27_lunch"]["recipe_id"] == lunch.after["recipe_id"]
    assert env.repo.get_revision(rev.plan_id, 2) is not None


def test_linked_pair_split_is_rejected() -> None:
    from mealplanner.application.generation import generate

    a = generate(INPUTS, Calls(), 1, None)
    b = generate(INPUTS, Calls(), 2, None)
    from mealplanner.domain.snapshot import normalize_snapshot

    def snap(g):
        return normalize_snapshot(g.plan.model_dump(), g.shopping_list.model_dump(), set(INPUTS.pantry), FUTURE)

    changes = diff_snapshots(snap(a), snap(b))
    check_linked_pairs(changes)  # whole diff is consistent
    partial = tuple(c for c in changes if c.assignment_id != "slot_2026-10-27_dinner")
    with pytest.raises(LinkedPairSplit):
        check_linked_pairs(partial)


def test_undo_appends_restoring_revision(env: Env) -> None:
    rev = created(env)
    with pytest.raises(NothingToUndo):
        env.svc.undo(rev.plan_id, 1, "u0")
    p = env.svc.propose_regeneration(rev.plan_id, 1, seed=2)
    env.svc.accept_proposal(rev.plan_id, p.proposal_id, 1, "r1")
    res = env.svc.undo(rev.plan_id, 2, "u1")
    assert res.changed and res.revision == 3
    cur = env.svc.get_current(rev.plan_id)
    assert cur.source == "undo" and cur.revision == 3
    assert cur.snapshot.plan["slots"] == rev.snapshot.plan["slots"]
    assert cur.snapshot.shopping_list["sections"] == rev.snapshot.shopping_list["sections"]
    for n in (1, 2, 3):  # history kept
        assert env.repo.get_revision(rev.plan_id, n) is not None
    assert env.svc.undo(rev.plan_id, 2, "u1").replayed
    assert env.repo.get_revision(rev.plan_id, 4) is None


def test_snapshot_id_semantics(env: Env) -> None:
    rev = created(env, seed=1)
    token = rev.snapshot.plan["snapshot_id"]
    assert rev.snapshot.shopping_list["snapshot_id"] == token
    # identical regeneration keeps revision and token
    env.solver.pin = 1
    p = env.svc.propose_regeneration(rev.plan_id, 1, seed=50)
    assert p.snapshot.plan["snapshot_id"] == token
    env.solver.pin = None
    q = env.svc.propose_regeneration(rev.plan_id, 1, seed=2)
    assert q.snapshot.plan["snapshot_id"] != token
    env.svc.accept_proposal(rev.plan_id, q.proposal_id, 1, "r1")
    # reading never changes the token
    assert env.svc.get_current(rev.plan_id).snapshot.plan["snapshot_id"] == q.snapshot.plan["snapshot_id"]
    # undo restores the earlier content under a new revision, so its token is new
    env.svc.undo(rev.plan_id, 2, "u1")
    restored = env.svc.get_current(rev.plan_id).snapshot
    assert restored.plan["snapshot_id"] not in (token, q.snapshot.plan["snapshot_id"])


# ---- real generation service end to end


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_real_generation_service_on_repo_inputs(kind: str, tmp_path: Path) -> None:
    repo, adapter = make_repo(kind, tmp_path)
    clock = Clock()
    svc = PlanWorkflowService(repo, Reader(), clock, OrToolsPlanGenerator())
    rev = svc.create_plan(FUTURE, seed=42)
    assert len(rev.snapshot.plan["slots"]) == 10
    p = svc.propose_regeneration(rev.plan_id, 1, seed=7)
    res = svc.accept_proposal(rev.plan_id, p.proposal_id, 1, "k1")
    expected = 2 if p.changes else 1
    assert res.revision == expected
    read = PlanReadService(adapter)
    assert read.get_plan(rev.plan_id)["revision"] == expected
