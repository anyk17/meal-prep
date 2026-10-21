"""Month fill: a batch of independent weekly plans (no monthly or cross-week rules)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker, RefResolver

from mealplanner.adapters.memory import InMemoryPlanRepository
from mealplanner.adapters.ortools import OrToolsPlanGenerator
from mealplanner.adapters.yaml_inputs import YamlPlannerInputReader
from mealplanner.application.errors import InvalidRequest
from mealplanner.application.plan_workflows import PlanWorkflowService
from mealplanner.domain.month import week_seed, weeks_of_month
from mealplanner.domain.planner_inputs import PlannerInputs
from mealplanner.domain.solution import MealPlanSolution
from mealplanner.domain.weekly_rules import check_weekly_rules
from mealplanner.entrypoints.db_cli import import_inputs
from mealplanner.entrypoints.http.app import create_app

ROOT = Path(__file__).resolve().parents[2]
SPEC = yaml.safe_load((ROOT / "docs/migration-to-cloud/openapi.yaml").read_text())
INPUTS: PlannerInputs = YamlPlannerInputReader(ROOT / "data").read()
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)  # Wednesday in Bogota
LAN = {"REMOTE_ADDR": "192.168.1.20"}

_cache: dict[int, MealPlanSolution] = {}


def real(inputs: PlannerInputs, seed: int) -> MealPlanSolution:
    if seed not in _cache:
        _cache[seed] = OrToolsPlanGenerator().generate(inputs, seed)
    return _cache[seed]


class Stub:
    """Records seeds; returns one real solution (fast); fails for chosen seeds."""

    def __init__(self) -> None:
        self.seeds: list[int] = []
        self.fail_seeds: set[int] = set()

    def generate(self, inputs: PlannerInputs, seed: int) -> MealPlanSolution:
        self.seeds.append(seed)
        if seed in self.fail_seeds:
            raise RuntimeError("infeasible")
        return real(inputs, 1)


class RealCached:
    def generate(self, inputs: PlannerInputs, seed: int) -> MealPlanSolution:
        return real(inputs, seed)


class Reader:
    def read(self) -> PlannerInputs:
        return INPUTS


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def service(generator=None, clock=None, repo=None) -> PlanWorkflowService:
    return PlanWorkflowService(
        repo or InMemoryPlanRepository(), Reader(), clock or Clock(), generator or Stub()
    )


def statuses(result) -> dict[str, str]:
    return {w.week_start.isoformat(): w.status for w in result.weeks}


# ---- week enumeration (pure)


@pytest.mark.parametrize(
    "month,first,count",
    [
        ("2026-02", "2026-01-26", 5),  # Feb 1 is Sunday: Jan 26 week; Feb 28 is Saturday
        ("2026-03", "2026-02-23", 6),  # Mar 1 Sunday, Mar 31 Tuesday
        ("2027-02", "2027-02-01", 4),  # exactly four weeks, Monday to Sunday
        ("2026-10", "2026-09-28", 5),
        ("2026-12", "2026-11-30", 5),  # December -> January boundary
        ("2027-01", "2026-12-28", 5),
        ("2026-11", "2026-10-26", 6),
    ],
)
def test_weeks_of_month(month: str, first: str, count: int) -> None:
    weeks = weeks_of_month(month)
    assert weeks[0] == date.fromisoformat(first) and len(weeks) == count
    assert all(w.weekday() == 0 for w in weeks)
    assert all(b - a == timedelta(days=7) for a, b in zip(weeks, weeks[1:]))
    y, m = map(int, month.split("-"))
    assert weeks[0] <= date(y, m, 1) <= weeks[0] + timedelta(days=6)
    last = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
    assert weeks[-1] <= last <= weeks[-1] + timedelta(days=6)


def test_week_count_is_always_four_to_six() -> None:
    for year in (2026, 2027, 2028):
        for m in range(1, 13):
            assert 4 <= len(weeks_of_month(f"{year}-{m:02d}")) <= 6


def test_week_seed_is_documented_pure_function() -> None:
    assert week_seed(date(2026, 10, 26)) == 20261026
    assert all(0 <= week_seed(w) < 2**31 for w in weeks_of_month("2026-12"))


@pytest.mark.parametrize("bad", ["2026-13", "2026-00", "26-10", "2026-1", "2026-10-01", "", "abcd-ef", "0000-01"])
def test_invalid_month_rejected(bad: str) -> None:
    with pytest.raises(InvalidRequest):
        service().fill_month(bad)


# ---- service behaviour


def test_fill_creates_current_and_future_skips_past() -> None:
    stub = Stub()
    result = service(stub).fill_month("2026-10")
    assert statuses(result) == {
        "2026-09-28": "skipped_past",
        "2026-10-05": "skipped_past",
        "2026-10-12": "skipped_past",
        "2026-10-19": "created",  # current week follows the create policy
        "2026-10-26": "created",
    }
    created = [w for w in result.weeks if w.status == "created"]
    assert [(w.plan_id, w.revision) for w in created] == [("plan_2026_10_19", 1), ("plan_2026_10_26", 1)]
    assert stub.seeds == [20261019, 20261026]  # past weeks never reach the solver


def test_existing_weeks_untouched() -> None:
    svc = service()
    first = svc.create_plan(date(2026, 10, 26), seed=7)
    result = svc.fill_month("2026-10")
    existing = [w for w in result.weeks if w.status == "exists"]
    assert [(w.plan_id, w.revision) for w in existing] == [("plan_2026_10_26", 1)]
    again = svc.get_plan_for_week(date(2026, 10, 26))
    assert again.current_revision == 1
    assert svc.get_current(first.plan_id).snapshot.plan["snapshot_id"] == first.snapshot.plan["snapshot_id"]
    assert svc.get_current(first.plan_id).snapshot.plan["seed"] == 7  # not regenerated


def test_refill_is_idempotent_and_does_not_solve_again() -> None:
    stub = Stub()
    svc = service(stub)
    svc.fill_month("2026-11")
    calls = len(stub.seeds)
    result = svc.fill_month("2026-11")
    assert set(statuses(result).values()) == {"exists"} and len(stub.seeds) == calls


def test_exists_reports_current_revision_after_edit() -> None:
    svc = service()
    svc.fill_month("2026-11")
    plan = svc.get_plan_for_week(date(2026, 11, 9))
    svc._generator = RealCached()  # a different seed gives a different plan
    proposal = svc.propose_regeneration(plan.plan_id, 1, seed=2)
    svc.accept_proposal(plan.plan_id, proposal.proposal_id, 1, "k1")
    result = svc.fill_month("2026-11")
    week = next(w for w in result.weeks if w.week_start == date(2026, 11, 9))
    assert week.status == "exists" and week.revision == 2


def test_boundary_week_created_once_across_adjacent_months() -> None:
    svc = service()
    nov = svc.fill_month("2026-11")
    dec = svc.fill_month("2026-12")
    boundary = date(2026, 11, 30)  # spans Nov 30 - Dec 6
    assert statuses(nov)[boundary.isoformat()] == "created"
    assert statuses(dec)[boundary.isoformat()] == "exists"
    plan = svc.get_plan_for_week(boundary)
    assert plan.current_revision == 1  # created once, untouched by the second fill


def test_december_january_boundary_single_plan() -> None:
    svc = service()
    dec = svc.fill_month("2026-12")
    jan = svc.fill_month("2027-01")
    assert statuses(dec)["2026-12-28"] == "created"
    assert statuses(jan)["2026-12-28"] == "exists"


def test_deterministic_seeds_identical_plans_on_fresh_databases() -> None:
    a, b = Stub(), Stub()
    ra = service(a).fill_month("2026-11")
    rb = service(b).fill_month("2026-11")
    assert a.seeds == b.seeds == [week_seed(w) for w in weeks_of_month("2026-11")]
    ids = lambda svc, r: [  # noqa: E731
        svc.get_current(w.plan_id).snapshot.plan["snapshot_id"] for w in r.weeks if w.plan_id
    ]
    sa, sb = service(Stub()), service(Stub())
    assert ids(sa, sa.fill_month("2026-11")) == ids(sb, sb.fill_month("2026-11"))
    assert ra == rb


def test_one_failing_week_does_not_stop_others() -> None:
    stub = Stub()
    stub.fail_seeds = {week_seed(date(2026, 11, 9))}
    svc = service(stub)
    result = svc.fill_month("2026-11")
    by_start = {w.week_start.isoformat(): w for w in result.weeks}
    failed = by_start["2026-11-09"]
    assert failed.status == "failed" and failed.error_code == "generation_failed" and failed.error_message
    assert failed.plan_id is None and svc.get_plan_for_week(date(2026, 11, 9)) is None
    assert [w.status for w in result.weeks].count("created") == 5
    # a later fill retries only the failed week
    stub.fail_seeds = set()
    stub.seeds.clear()
    assert statuses(svc.fill_month("2026-11"))["2026-11-09"] == "created"
    assert stub.seeds == [week_seed(date(2026, 11, 9))]


def test_rule_violation_recorded_as_failed(monkeypatch) -> None:
    from mealplanner.application import plan_workflows as pw
    from mealplanner.domain.weekly_rules import RuleViolation

    calls = {"n": 0}
    real_require = pw.require_weekly_rules

    def flaky(snapshot, inputs):
        calls["n"] += 1
        if calls["n"] == 2:
            from mealplanner.domain.weekly_plan import RuleViolationError

            raise RuleViolationError([RuleViolation("rule_x", "broken")])
        return real_require(snapshot, inputs)

    monkeypatch.setattr(pw, "require_weekly_rules", flaky)
    result = service().fill_month("2026-11")
    failed = [w for w in result.weeks if w.status == "failed"]
    assert [(w.error_code, "rule_x" in w.error_message) for w in failed] == [("rule_violation", True)]
    assert [w.status for w in result.weeks].count("created") == 5


def test_duplicate_race_is_reported_as_exists() -> None:
    repo = InMemoryPlanRepository()
    svc = service(repo=repo)
    original = repo.create_plan

    def racing(plan, revision):
        repo.create_plan = original  # another writer wins exactly once
        original(plan, revision)
        original(plan, revision)  # raises DuplicateWeek

    repo.create_plan = racing
    result = svc.fill_month("2026-12")
    first = next(w for w in result.weeks if w.status == "exists")
    assert first.plan_id and first.revision == 1


def test_timezone_decides_past_week() -> None:
    # 2026-10-26 03:00 UTC is still Sunday 2026-10-25 22:00 in Bogota: week of 10-19 is current.
    svc = service(clock=Clock(datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)))
    assert statuses(svc.fill_month("2026-10"))["2026-10-19"] == "created"
    svc = service(clock=Clock(datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)))
    assert statuses(svc.fill_month("2026-10"))["2026-10-19"] == "skipped_past"


def test_real_solver_month_each_week_passes_weekly_rules() -> None:
    svc = service(RealCached())
    result = svc.fill_month("2026-10")
    created = [w for w in result.weeks if w.status == "created"]
    assert len(created) == 2
    for w in created:
        rev = svc.get_current(w.plan_id)
        assert rev.snapshot.plan["seed"] == week_seed(w.week_start)
        assert check_weekly_rules(rev.snapshot, INPUTS) == []


# ---- HTTP


def check_schema(name: str, instance) -> None:
    validator = Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}"},
        resolver=RefResolver.from_schema(SPEC),
        format_checker=FormatChecker(),
    )
    errors = list(validator.iter_errors(instance))
    assert not errors, [e.message for e in errors]


def fail(response, status: int, code: str) -> None:
    assert response.status_code == status, response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"
    body = response.get_json()
    check_schema("ErrorResponse", body)
    assert body["error"]["code"] == code


@pytest.fixture
def stub() -> Stub:
    return Stub()


@pytest.fixture
def app(tmp_path, stub):
    db = tmp_path / "plans.db"
    import_inputs(db, ROOT / "data")
    return create_app(db_path=db, clock=Clock(), generator=stub)


@pytest.fixture
def client(app):
    return app.test_client()


def test_http_fill_ok_and_schema(client) -> None:
    response = client.post("/api/v1/months/2026-10/fill", json={})
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    body = response.get_json()
    check_schema("MonthFillResult", body)
    assert body["month"] == "2026-10" and len(body["weeks"]) == 5
    assert [w["status"] for w in body["weeks"]] == ["skipped_past"] * 3 + ["created"] * 2
    assert body["weeks"][3] == {
        "week_start": "2026-10-19", "week_end": "2026-10-25", "status": "created",
        "plan_id": "plan_2026_10_19", "revision": 1,
    }
    assert client.get("/api/v1/plans/plan_2026_10_26").status_code == 200
    again = client.post("/api/v1/months/2026-10/fill", json={}).get_json()
    check_schema("MonthFillResult", again)
    assert [w["status"] for w in again["weeks"]] == ["skipped_past"] * 3 + ["exists"] * 2


def test_http_failed_week_in_body(client, stub) -> None:
    stub.fail_seeds = {week_seed(date(2026, 11, 9))}
    body = client.post("/api/v1/months/2026-11/fill", json={}).get_json()
    check_schema("MonthFillResult", body)
    failed = [w for w in body["weeks"] if w["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["error"]["code"] == "generation_failed"
    assert "plan_id" not in failed[0]


def test_http_bad_month_and_bad_body(client) -> None:
    fail(client.post("/api/v1/months/2026-13/fill", json={}), 400, "invalid_request")
    fail(client.post("/api/v1/months/oct/fill", json={}), 400, "invalid_request")
    fail(client.post("/api/v1/months/2026-10/fill", json={"seed": 1}), 400, "invalid_request")
    fail(client.post("/api/v1/months/2026-10/fill", data="x", content_type="text/plain"), 400, "invalid_request")


def test_http_forbidden_and_read_only(client) -> None:
    fail(client.post("/api/v1/months/2026-10/fill", json={}, environ_base=LAN), 403, "forbidden")
    assert client.get("/api/v1/plans?from=2026-10-01&to=2026-12-31").get_json()["items"] == []
    json_client = create_app(week_start="2026-10-19").test_client()
    fail(json_client.post("/api/v1/months/2026-10/fill", json={}), 403, "read_only")


def test_http_busy(app, client, stub) -> None:
    gate = app.config["GENERATION_GATE"]
    assert gate.try_acquire()
    try:
        response = client.post("/api/v1/months/2026-10/fill", json={})
        fail(response, 503, "generation_busy")
        assert response.headers["Retry-After"] and stub.seeds == []
    finally:
        gate.release()
    assert client.post("/api/v1/months/2026-10/fill", json={}).status_code == 200


def test_http_gate_released_after_fill(app, client) -> None:
    client.post("/api/v1/months/2026-10/fill", json={})
    gate = app.config["GENERATION_GATE"]
    assert gate.try_acquire()
    gate.release()
