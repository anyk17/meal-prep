"""Contract tests for domain/weekly_rules.py against the OR-Tools generator and the saved fixture.

The specifications are the business definition; the CP-SAT encoding must never produce a
plan they reject, and each specification must detect its own hand-made violation.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from mealplanner.adapters.memory import InMemoryPlanRepository
from mealplanner.adapters.ortools import OrToolsPlanGenerator
from mealplanner.adapters.yaml_inputs import YamlPlannerInputReader
from mealplanner.application.generation import generate
from mealplanner.application.plan_workflows import PlanWorkflowService
from mealplanner.domain.planner_inputs import PlannerInputs
from mealplanner.domain.snapshot import PlanSnapshot, normalize_snapshot
from mealplanner.domain.solution import MealPlanSolution
from mealplanner.domain.weekly_plan import (
    GenerationFailed,
    RuleViolationError,
)
from mealplanner.domain.weekly_rules import (
    WEEKLY_RULES,
    RuleViolation,
    check_weekly_rules,
)
from mealplanner.entrypoints.db_cli import import_fixture, import_inputs

ROOT = Path(__file__).resolve().parents[2]
INPUTS: PlannerInputs = YamlPlannerInputReader(ROOT / "data").read()
WEEK = date(2026, 10, 26)  # a Monday
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
SEEDS = range(int(os.environ.get("WEEKLY_RULES_SEEDS", "50")))
ALL_CODES = {rule.code for rule in WEEKLY_RULES}


def snapshot_of(solution: MealPlanSolution, seed: int = 1, inputs: PlannerInputs = INPUTS) -> PlanSnapshot:
    class Fixed:
        def generate(self, _inputs: PlannerInputs, _seed: int) -> MealPlanSolution:
            return solution

    generated = generate(inputs, Fixed(), seed, "2026-10-20T00:00:00+00:00")
    return normalize_snapshot(
        generated.plan.model_dump(),
        generated.shopping_list.model_dump(),
        set(inputs.pantry),
        WEEK,
    )


# ---------------------------------------------------------------- (a) real generator


def test_rule_codes_are_unique_and_stable() -> None:
    codes = [rule.code for rule in WEEKLY_RULES]
    assert len(codes) == len(set(codes))
    assert ALL_CODES == {
        "slot_coverage",
        "meal_type_eligible",
        "variant_consistent",
        "weekly_protein_count",
        "consecutive_same_protein",
        "fish_dinner_weekly_max",
        "fish_dinner_consecutive_max",
        "meal_carbs_forbidden",
        "lunch_carb_required",
        "consecutive_lunch_same_carb",
        "carb_weekly_max",
        "recipe_weekly_max",
        "extend_to_dinner_pair",
        "linked_pair_marked",
    }


@pytest.mark.parametrize("seed", SEEDS)
def test_ortools_generator_satisfies_every_specification(seed: int) -> None:
    solution = OrToolsPlanGenerator().generate(INPUTS, seed)
    snapshot = snapshot_of(solution, seed)
    assert check_weekly_rules(snapshot, INPUTS) == []


# ---------------------------------------------------------------- (c) saved fixture


def test_site_fixture_passes() -> None:
    snapshot = normalize_snapshot(
        json.loads((ROOT / "site/public/plan.json").read_text()),
        json.loads((ROOT / "site/public/shopping_list.json").read_text()),
        set(INPUTS.pantry),
        date(2026, 10, 19),
    )
    assert check_weekly_rules(snapshot, INPUTS) == []
    assert check_weekly_rules(snapshot.plan, INPUTS) == []  # plan JSON mapping works too


# ---------------------------------------------------------------- (b) one violation per rule


def baseline() -> dict:
    """The saved fixture's normalized plan: valid under the real rules."""
    return copy.deepcopy(
        normalize_snapshot(
            json.loads((ROOT / "site/public/plan.json").read_text()),
            json.loads((ROOT / "site/public/shopping_list.json").read_text()),
            set(INPUTS.pantry),
            date(2026, 10, 19),
        ).plan
    )


def slot(plan: dict, day: str, meal: str) -> dict:
    return next(s for s in plan["slots"] if s["day"] == day and s["meal"] == meal)


def place(plan: dict, day: str, meal: str, variant_id: str) -> None:
    """Put a catalog variant in a slot, keeping the linked-pair marking as the generator would."""
    from mealplanner.domain.weekly_rules import RuleContext

    variant = RuleContext.from_inputs(INPUTS).variants[variant_id]
    s = slot(plan, day, meal)
    s["recipe_id"], s["variant_id"] = variant.base_recipe_id, variant_id
    s["protein"] = variant.recipe.tags.protein_group.value
    s["carb"] = variant.carb_ingredient_id
    if meal == "dinner":
        lunch = slot(plan, day, "lunch")
        linked = variant.recipe.extend_to_dinner
        s["extended_from_lunch"] = linked
        s["linked_assignment_id"] = lunch["assignment_id"] if linked else None
        lunch["linked_assignment_id"] = s["assignment_id"] if linked else None


def swap(plan: dict, a: tuple[str, str], b: tuple[str, str]) -> None:
    va, vb = slot(plan, *a)["variant_id"], slot(plan, *b)["variant_id"]
    place(plan, *a, vb)
    place(plan, *b, va)


def with_constraints(**updates) -> PlannerInputs:
    rules = INPUTS.rules.model_copy(deep=True)
    rules.constraints = rules.constraints.model_copy(update=updates)
    return dataclasses.replace(INPUTS, rules=rules)


def with_ingredient(ingredient_id: str, **updates) -> PlannerInputs:
    ingredients = dict(INPUTS.ingredients)
    ingredients[ingredient_id] = ingredients[ingredient_id].model_copy(update=updates)
    return dataclasses.replace(INPUTS, ingredients=ingredients)


def m_slot_coverage(plan: dict) -> None:
    s = slot(plan, "tue", "dinner")  # relabelled to a day outside the week: tue dinner is empty
    s["day"] = "sat"


def m_meal_type(plan: dict) -> None:
    place(plan, "tue", "dinner", "pescado_encocado__carb_none")  # lunch-only recipe at dinner


def m_variant(plan: dict) -> None:
    slot(plan, "wed", "dinner")["variant_id"] = "pollo_guiso_habichuelas_dinner__carb_zzz"


def m_protein_count(plan: dict) -> None:
    place(plan, "tue", "lunch", "carne_encebollada_lunch__carb_potato")  # beef 3, pork 0


def m_consecutive_protein(plan: dict) -> None:
    swap(plan, ("tue", "dinner"), ("wed", "dinner"))  # chicken dinner mon and tue


def m_fish_weekly(plan: dict) -> None:
    pass  # the plan is valid; the limit is lowered below


def m_fish_consecutive(plan: dict) -> None:
    swap(plan, ("wed", "dinner"), ("fri", "dinner"))  # fish dinners tue and wed


def m_dinner_carb(plan: dict) -> None:
    place(plan, "tue", "dinner", "salmon_horneado__carb_rice")


def m_lunch_carb(plan: dict) -> None:
    place(plan, "wed", "lunch", "atun_juanfe__carb_none")


def m_consecutive_carb(plan: dict) -> None:
    place(plan, "wed", "lunch", "atun_juanfe__carb_potato")
    place(plan, "thu", "lunch", "brocoli_con_carne__carb_potato")


def m_carb_max(plan: dict) -> None:
    place(plan, "thu", "lunch", "brocoli_con_carne__carb_yuca")  # yuca also on monday, max once


def m_recipe_max(plan: dict) -> None:
    place(plan, "fri", "dinner", "salmon_horneado__carb_none")  # salmon also on tuesday


def m_pair(plan: dict) -> None:
    swap(plan, ("mon", "dinner"), ("wed", "dinner"))  # toscano lunch without its dinner


def m_marked(plan: dict) -> None:
    dinner = slot(plan, "mon", "dinner")
    dinner["extended_from_lunch"] = False
    dinner["linked_assignment_id"] = None
    slot(plan, "mon", "lunch")["linked_assignment_id"] = None


MUTATIONS = [
    ("slot_coverage", m_slot_coverage, INPUTS),
    ("meal_type_eligible", m_meal_type, INPUTS),
    ("variant_consistent", m_variant, INPUTS),
    ("weekly_protein_count", m_protein_count, INPUTS),
    ("consecutive_same_protein", m_consecutive_protein, INPUTS),
    ("fish_dinner_weekly_max", m_fish_weekly, with_constraints(fish_dinner_max_per_week=1)),
    (
        "fish_dinner_consecutive_max",
        m_fish_consecutive,
        with_constraints(no_consecutive_same_protein=False, fish_dinner_max_consecutive=1),
    ),
    ("meal_carbs_forbidden", m_dinner_carb, INPUTS),
    ("lunch_carb_required", m_lunch_carb, INPUTS),
    ("consecutive_lunch_same_carb", m_consecutive_carb, INPUTS),
    ("carb_weekly_max", m_carb_max, with_ingredient("yuca", max_times_week=1.0)),
    ("recipe_weekly_max", m_recipe_max, INPUTS),
    ("extend_to_dinner_pair", m_pair, INPUTS),
    ("linked_pair_marked", m_marked, INPUTS),
]


def test_every_rule_has_a_mutation() -> None:
    assert {code for code, _, _ in MUTATIONS} == ALL_CODES


@pytest.mark.parametrize("code,mutate,inputs", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_each_specification_detects_exactly_its_violation(code, mutate, inputs) -> None:
    plan = baseline()
    assert check_weekly_rules(plan, inputs) == [] or code == "fish_dinner_weekly_max"
    mutate(plan)
    violations = check_weekly_rules(plan, inputs)
    assert violations, "mutation was not detected"
    assert {v.rule for v in violations} == {code}, violations
    assert all(isinstance(v, RuleViolation) and v.message for v in violations)
    assert any(v.assignment_ids and v.dates for v in violations)
    if code == "fish_dinner_weekly_max":
        assert set(violations[0].assignment_ids) == {
            slot(plan, "tue", "dinner")["assignment_id"],
            slot(plan, "fri", "dinner")["assignment_id"],
        }


def test_violation_reports_affected_assignments_and_dates() -> None:
    plan = baseline()
    m_consecutive_protein(plan)
    (violation,) = check_weekly_rules(plan, INPUTS)
    assert violation.assignment_ids == ("slot_2026-10-19_dinner", "slot_2026-10-20_dinner")
    assert violation.dates == ("2026-10-19", "2026-10-20")


def test_parameters_come_from_rules_not_constants() -> None:
    plan = baseline()
    relaxed = with_constraints(max_recipe_uses_per_week=2)
    m_recipe_max(plan)
    assert [v.rule for v in check_weekly_rules(plan, INPUTS)] == ["recipe_weekly_max"]
    assert check_weekly_rules(plan, relaxed) == []


# ---------------------------------------------------------------- enforcement in the workflow


def swapped_lunch_solution() -> MealPlanSolution:
    """A valid real solution with two lunches swapped: structurally fine, breaks adjacency rules."""
    solution = OrToolsPlanGenerator().generate(INPUTS, 42)
    assignments = dict(solution.assignments)
    assignments["mon_lunch"], assignments["thu_lunch"] = (
        assignments["thu_lunch"],
        assignments["mon_lunch"],
    )
    return MealPlanSolution(assignments=assignments)


class Gen:
    def __init__(self) -> None:
        self.bad = False

    def generate(self, inputs: PlannerInputs, seed: int) -> MealPlanSolution:
        return swapped_lunch_solution() if self.bad else OrToolsPlanGenerator().generate(inputs, seed)


class Reader:
    def __init__(self) -> None:
        self.inputs = INPUTS

    def read(self) -> PlannerInputs:
        return self.inputs


def make_service(generator) -> tuple[PlanWorkflowService, InMemoryPlanRepository, Reader]:
    repo, reader = InMemoryPlanRepository(), Reader()
    svc = PlanWorkflowService(repo, reader, lambda: NOW, generator, id_factory=lambda: "fixed")
    return svc, repo, reader


def tamper(plan: dict) -> None:
    """Corrupt a stored plan so that it breaks a rule whatever its content (unknown variant)."""
    plan["slots"][0]["variant_id"] = "nonexistent__carb_none"


def test_create_rejects_violating_candidate_and_stores_nothing() -> None:
    gen = Gen()
    gen.bad = True
    svc, repo, _ = make_service(gen)
    with pytest.raises(RuleViolationError) as exc:
        svc.create_plan(WEEK, seed=1)
    assert {v.rule for v in exc.value.violations} <= ALL_CODES and exc.value.violations
    assert not isinstance(exc.value, GenerationFailed)
    assert repo.get_plan_by_week(WEEK) is None


def test_proposal_rejects_violating_candidate_and_stores_nothing() -> None:
    gen = Gen()
    svc, repo, _ = make_service(gen)
    rev = svc.create_plan(WEEK, seed=1)
    gen.bad = True
    with pytest.raises(RuleViolationError):
        svc.propose_regeneration(rev.plan_id, 1, seed=2)
    assert repo._proposals == {}
    assert svc.get_current(rev.plan_id).revision == 1


def test_accept_rechecks_a_stored_proposal() -> None:
    svc, repo, _ = make_service(Gen())
    rev = svc.create_plan(WEEK, seed=1)
    proposal = svc.propose_regeneration(rev.plan_id, 1, seed=5)
    assert proposal.changes
    bad_plan = copy.deepcopy(proposal.snapshot.plan)
    tamper(bad_plan)  # as a corrupted or hand-edited stored row would be
    bad = PlanSnapshot(bad_plan, proposal.snapshot.shopping_list)
    repo.save_proposal(dataclasses.replace(proposal, snapshot=bad))
    with pytest.raises(RuleViolationError) as exc:
        svc.accept_proposal(rev.plan_id, proposal.proposal_id, 1, "k1")
    assert any(v.rule == "variant_consistent" for v in exc.value.violations)
    assert svc.get_current(rev.plan_id).revision == 1


def accepted_revision_two() -> tuple[PlanWorkflowService, InMemoryPlanRepository, Reader, str]:
    svc, repo, reader = make_service(Gen())
    rev = svc.create_plan(WEEK, seed=1)
    proposal = svc.propose_regeneration(rev.plan_id, 1, seed=5)
    svc.accept_proposal(rev.plan_id, proposal.proposal_id, 1, "k1")
    assert svc.get_current(rev.plan_id).revision == 2
    return svc, repo, reader, rev.plan_id


def test_undo_restores_valid_history_when_inputs_unchanged() -> None:
    svc, _, _, plan_id = accepted_revision_two()
    assert svc.undo(plan_id, 2, "u1").revision == 3


def test_undo_allows_rule_violating_history_when_inputs_unchanged() -> None:
    svc, repo, _, plan_id = accepted_revision_two()
    stored = repo._revisions[(plan_id, 1)]
    bad_plan = copy.deepcopy(stored.snapshot.plan)
    tamper(bad_plan)
    repo._revisions[(plan_id, 1)] = dataclasses.replace(
        stored, snapshot=PlanSnapshot(bad_plan, stored.snapshot.shopping_list)
    )
    # weekly rules are advisory for undo (phase 5): the violating revision is restored
    assert svc.undo(plan_id, 2, "u1").revision == 3
    assert svc.get_current(plan_id).revision == 3


def test_undo_reports_but_does_not_block_when_inputs_changed(caplog) -> None:
    svc, _, reader, plan_id = accepted_revision_two()
    reader.inputs = with_constraints(fish_dinner_max_per_week=0)  # revision 1 now breaks the rules
    with caplog.at_level("WARNING"):
        assert svc.undo(plan_id, 2, "u1").revision == 3
    assert "fish_dinner_weekly_max" in caplog.text


def test_fixture_import_validates_rules(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    import_inputs(db, ROOT / "data")
    plan = json.loads((ROOT / "site/public/plan.json").read_text())
    lunches = {s["day"]: s for s in plan["slots"] if s["meal"] == "lunch"}
    # swap two whole lunches: still a structurally valid snapshot, but it breaks adjacency rules
    for key in ("recipeId", "recipeName", "variantId", "protein", "carb", "carbQty", "ingredients", "proteinQty"):
        lunches["tue"][key], lunches["fri"][key] = lunches["fri"][key], lunches["tue"][key]
    bad_path = tmp_path / "plan.json"
    bad_path.write_text(json.dumps(plan))
    shopping = ROOT / "site/public/shopping_list.json"
    with pytest.raises(RuleViolationError):
        import_fixture(db, bad_path, shopping, date(2026, 10, 19), now=NOW)
    assert import_fixture(db, ROOT / "site/public/plan.json", shopping, date(2026, 10, 19), now=NOW) == "imported"
