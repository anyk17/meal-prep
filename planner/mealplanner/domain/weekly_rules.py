"""Weekly meal-plan business rules as small, solver-independent specifications.

Each rule is one `WeeklyRule` with a stable `code` and `check(plan, context)`
returning `RuleViolation`s. They run on the dated plan snapshot that is stored
and accepted (`PlanSnapshot.plan`, the "slots" JSON shape), so stored revisions,
proposals, imports and replacements are validated without any solver. Rule
parameters come from the same `Rules` / `Ingredient` / `Recipe` values the
solver reads; nothing is hard-coded here beyond what solve.py hard-codes
(noted below). `check_weekly_rules(plan, inputs)` composes every rule.

The CP-SAT encoding of each rule lives in `adapters/ortools/` (model.py, solve.py).
Rule code -> solver constraint:

    slot_coverage             model.py create_model: exactly one variant per
                              (day, meal) slot of rules.week
    meal_type_eligible        model.py create_model: a variable exists only when
                              slot.meal is in recipe.meal_types
    variant_consistent        model.py/variants.py: variables are the expanded
                              variants (variant_id, base recipe, protein, carb)
    weekly_protein_count      solve.py add_weekly_protein_constraints (exact counts)
    consecutive_same_protein  solve.py add_consecutive_protein_constraints
                              (per meal, adjacent days, when the flag is set)
    fish_dinner_weekly_max    solve.py add_fish_dinner_constraints (constraint 3)
    fish_dinner_consecutive_max
                              solve.py add_fish_dinner_constraints (constraint 4:
                              no run of fish dinners longer than the max)
    meal_carbs_forbidden      solve.py add_meal_carb_rules (meal_rules.allow_carbs false)
    lunch_carb_required       solve.py add_lunch_carb_required (hard-coded to lunch)
    consecutive_lunch_same_carb
                              solve.py add_no_consecutive_same_carb (lunches only)
    carb_weekly_max           solve.py add_carb_frequency_constraints
                              (ingredient.max_times_week)
    recipe_weekly_max         solve.py add_recipe_frequency_constraints (a linked
                              lunch/dinner pair of an extend_to_dinner recipe counts
                              once: only its lunch is counted)
    extend_to_dinner_pair     solve.py add_extend_to_dinner_constraints (an
                              extend_to_dinner recipe is used at lunch and dinner
                              of the same day, or not at all that day)
    linked_pair_marked        no solver constraint: the snapshot representation of
                              the pair (dinner extended_from_lunch + reciprocal
                              linked_assignment_id) derived by plan_output.py and
                              normalize_snapshot

Everything solve.py enforces is expressible from the snapshot, because each
assignment carries recipe_id, variant_id, protein and carb. Day adjacency means
neighbouring entries of rules.week.days (the week is not circular).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from .catalog import Ingredient, IngredientKind, MealType, ProteinType, Recipe, Rules
from .planner_inputs import PlannerInputs
from .variants import RecipeVariant, expand_variants


@dataclass(frozen=True)
class RuleViolation:
    """One broken rule: a stable code, a human message and the affected assignments."""

    rule: str
    message: str
    assignment_ids: tuple[str, ...] = ()
    dates: tuple[str, ...] = ()


@dataclass(frozen=True)
class Assignment:
    """The rule-relevant fields of one snapshot slot."""

    assignment_id: str
    date: str
    day: str
    meal: str
    recipe_id: str | None
    variant_id: str | None
    protein: str | None
    carb: str | None
    extended_from_lunch: bool
    linked_assignment_id: str | None


@dataclass(frozen=True)
class PlanView:
    """A plan's assignments, indexed by (day, meal) for the specifications."""

    assignments: tuple[Assignment, ...]

    @classmethod
    def from_plan(cls, plan: Mapping[str, Any]) -> PlanView:
        return cls(
            tuple(
                Assignment(
                    assignment_id=str(slot.get("assignment_id")),
                    date=str(slot.get("date")),
                    day=str(slot.get("day")),
                    meal=str(slot.get("meal")),
                    recipe_id=slot.get("recipe_id"),
                    variant_id=slot.get("variant_id"),
                    protein=slot.get("protein"),
                    carb=slot.get("carb"),
                    extended_from_lunch=bool(slot.get("extended_from_lunch")),
                    linked_assignment_id=slot.get("linked_assignment_id"),
                )
                for slot in plan.get("slots", [])
            )
        )

    def at(self, day: str, meal: MealType) -> Assignment | None:
        """The first assignment for a day/meal slot, or None."""
        for a in self.assignments:
            if a.day == day and a.meal == meal.value:
                return a
        return None

    def meal(self, meal: MealType) -> list[Assignment]:
        return [a for a in self.assignments if a.meal == meal.value]


@dataclass(frozen=True)
class RuleContext:
    """Catalog values the rules are parameterised by (the same ones the solver reads)."""

    rules: Rules
    recipes: Mapping[str, Recipe]
    ingredients: Mapping[str, Ingredient]
    variants: Mapping[str, RecipeVariant]

    @classmethod
    def from_inputs(cls, inputs: PlannerInputs) -> RuleContext:
        variants = expand_variants(dict(inputs.recipes), dict(inputs.ingredients))
        return cls(
            inputs.rules,
            inputs.recipes,
            inputs.ingredients,
            {v.variant_id: v for v in variants},
        )

    @property
    def days(self) -> list[str]:
        return list(self.rules.week.days)

    def day_pairs(self) -> list[tuple[str, str]]:
        """Adjacent (day, next day) pairs of the configured week."""
        days = self.days
        return list(zip(days, days[1:]))

    def is_extendable(self, recipe_id: str | None) -> bool:
        recipe = self.recipes.get(recipe_id) if recipe_id else None
        return bool(recipe and recipe.extend_to_dinner)

    def supports(self, recipe_id: str | None, meal: MealType) -> bool:
        recipe = self.recipes.get(recipe_id) if recipe_id else None
        return bool(recipe and meal in recipe.meal_types)


class WeeklyRule(Protocol):
    """One business rule over a weekly plan."""

    code: ClassVar[str]

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]: ...


def _violation(
    code: str, message: str, assignments: Sequence[Assignment]
) -> RuleViolation:
    return RuleViolation(
        code,
        message,
        tuple(a.assignment_id for a in assignments),
        tuple(sorted({a.date for a in assignments})),
    )


# ---------------------------------------------------------------- structure


class SlotCoverage:
    """Every (day, meal) slot of the week is filled exactly once, and nothing else is."""

    code = "slot_coverage"

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]:
        expected = [(d, m.value) for d in context.days for m in context.rules.week.meals]
        found: dict[tuple[str, str], list[Assignment]] = defaultdict(list)
        for a in plan.assignments:
            found[(a.day, a.meal)].append(a)
        out: list[RuleViolation] = []
        for key in expected:
            if key not in found:
                out.append(RuleViolation(self.code, f"no assignment for {key[0]} {key[1]}"))
            elif len(found[key]) > 1:
                out.append(_violation(self.code, f"{key[0]} {key[1]} is assigned more than once", found[key]))
        for key, items in found.items():
            if key not in expected:
                out.append(_violation(self.code, f"{key[0]} {key[1]} is not a slot of the week", items))
        return out


class MealTypeEligible:
    """A recipe may only be used for a meal listed in its meal_types."""

    code = "meal_type_eligible"

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]:
        out = []
        for a in plan.assignments:
            recipe = context.recipes.get(a.recipe_id or "")
            if recipe is not None and a.meal not in {m.value for m in recipe.meal_types}:
                out.append(_violation(self.code, f"{a.recipe_id} is not a {a.meal} recipe", [a]))
        return out


class VariantConsistent:
    """The assignment names a real recipe variant and its recipe/protein/carb agree with it."""

    code = "variant_consistent"

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]:
        out = []
        for a in plan.assignments:
            variant = context.variants.get(a.variant_id or "")
            if variant is None:
                out.append(_violation(self.code, f"unknown variant {a.variant_id!r}", [a]))
                continue
            problems = []
            if variant.base_recipe_id != a.recipe_id:
                problems.append(f"recipe {a.recipe_id!r} != {variant.base_recipe_id!r}")
            if variant.recipe.tags.primary_protein.value != a.protein:
                problems.append(f"protein {a.protein!r} != {variant.recipe.tags.primary_protein.value!r}")
            if variant.carb_ingredient_id != a.carb:
                problems.append(f"carb {a.carb!r} != {variant.carb_ingredient_id!r}")
            if problems:
                out.append(
                    _violation(self.code, f"{a.variant_id} disagrees with its assignment: " + "; ".join(problems), [a])
                )
        return out


# ---------------------------------------------------------------- proteins


class WeeklyProteinCount:
    """Each configured protein is used exactly its weekly count."""

    code = "weekly_protein_count"

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]:
        out = []
        for protein, count in context.rules.constraints.weekly_protein_counts.items():
            used = [a for a in plan.assignments if a.protein == protein.value]
            if len(used) != count:
                out.append(
                    _violation(self.code, f"{protein.value} is used {len(used)} times, expected exactly {count}", used)
                )
        return out


class ConsecutiveSameProtein:
    """The same protein may not be served at the same meal on adjacent days."""

    code = "consecutive_same_protein"

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]:
        if not context.rules.constraints.no_consecutive_same_protein:
            return []
        out = []
        for meal in context.rules.week.meals:
            for day, nxt in context.day_pairs():
                a, b = plan.at(day, meal), plan.at(nxt, meal)
                if a and b and a.protein is not None and a.protein == b.protein:
                    out.append(
                        _violation(self.code, f"{a.protein} {meal.value} on consecutive days {day} and {nxt}", [a, b])
                    )
        return out


def _fish_dinners(plan: PlanView, context: RuleContext) -> dict[str, Assignment]:
    return {
        a.day: a
        for a in plan.meal(MealType.DINNER)
        if a.protein == ProteinType.FISH.value and a.day in context.days
    }


class FishDinnerWeeklyMax:
    """At most fish_dinner_max_per_week dinners are fish."""

    code = "fish_dinner_weekly_max"

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]:
        fish = list(_fish_dinners(plan, context).values())
        limit = context.rules.constraints.fish_dinner_max_per_week
        if len(fish) > limit:
            return [_violation(self.code, f"{len(fish)} fish dinners, at most {limit} allowed", fish)]
        return []


class FishDinnerConsecutiveMax:
    """No run of fish dinners on consecutive days is longer than fish_dinner_max_consecutive."""

    code = "fish_dinner_consecutive_max"

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]:
        limit = context.rules.constraints.fish_dinner_max_consecutive
        fish = _fish_dinners(plan, context)
        out, run = [], []
        for day in [*context.days, None]:
            if day is not None and day in fish:
                run.append(fish[day])
                continue
            if len(run) > limit:
                out.append(
                    _violation(self.code, f"{len(run)} consecutive fish dinners, at most {limit} allowed", run)
                )
            run = []
        return out


# ---------------------------------------------------------------- carbs


class MealCarbsForbidden:
    """Meals whose meal_rules disallow carbs (dinner) carry no carb."""

    code = "meal_carbs_forbidden"

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]:
        out = []
        for meal, rule in context.rules.meal_rules.items():
            if rule.allow_carbs:
                continue
            for a in plan.meal(meal):
                if a.carb is not None:
                    out.append(_violation(self.code, f"{meal.value} must not include a carb ({a.carb})", [a]))
        return out


class LunchCarbRequired:
    """Every lunch includes a carb."""

    code = "lunch_carb_required"

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]:
        return [
            _violation(self.code, "lunch must include a carb", [a])
            for a in plan.meal(MealType.LUNCH)
            if a.carb is None
        ]


class ConsecutiveLunchSameCarb:
    """The same carb may not be served at lunch on adjacent days."""

    code = "consecutive_lunch_same_carb"

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]:
        out = []
        for day, nxt in context.day_pairs():
            a, b = plan.at(day, MealType.LUNCH), plan.at(nxt, MealType.LUNCH)
            if a and b and a.carb is not None and a.carb == b.carb:
                out.append(_violation(self.code, f"{a.carb} at lunch on consecutive days {day} and {nxt}", [a, b]))
        return out


class CarbWeeklyMax:
    """A carb with max_times_week is used at most that many times in the week."""

    code = "carb_weekly_max"

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]:
        out = []
        for carb_id, ingredient in context.ingredients.items():
            if ingredient.kind != IngredientKind.CARB or ingredient.max_times_week is None:
                continue
            used = [a for a in plan.assignments if a.carb == carb_id]
            if len(used) > int(ingredient.max_times_week):
                out.append(
                    _violation(
                        self.code,
                        f"{carb_id} used {len(used)} times, at most {int(ingredient.max_times_week)} allowed",
                        used,
                    )
                )
        return out


# ---------------------------------------------------------------- recipes


class RecipeWeeklyMax:
    """A base recipe is used at most max_recipe_uses_per_week times.

    A lunch/dinner pair of an extend_to_dinner recipe is one use, so only the
    lunches of such a recipe are counted.
    """

    code = "recipe_weekly_max"

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]:
        limit = context.rules.constraints.max_recipe_uses_per_week
        groups: dict[str, list[Assignment]] = defaultdict(list)
        for a in plan.assignments:
            if a.recipe_id is None:
                continue
            if context.is_extendable(a.recipe_id) and a.meal != MealType.LUNCH.value:
                continue
            groups[a.recipe_id].append(a)
        return [
            _violation(self.code, f"{recipe_id} used {len(items)} times, at most {limit} allowed", items)
            for recipe_id, items in groups.items()
            if len(items) > limit
        ]


class ExtendToDinnerPair:
    """An extend_to_dinner recipe is served at lunch and dinner of the same day, or not that day."""

    code = "extend_to_dinner_pair"

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]:
        out = []
        for day in context.days:
            lunch, dinner = plan.at(day, MealType.LUNCH), plan.at(day, MealType.DINNER)
            if lunch is None or dinner is None:
                continue
            for recipe_id in dict.fromkeys([lunch.recipe_id, dinner.recipe_id]):
                if not (
                    context.is_extendable(recipe_id)
                    and context.supports(recipe_id, MealType.LUNCH)
                    and context.supports(recipe_id, MealType.DINNER)
                ):
                    continue
                if lunch.recipe_id != recipe_id or dinner.recipe_id != recipe_id:
                    out.append(
                        _violation(
                            self.code,
                            f"{recipe_id} must be served at both lunch and dinner on {day}",
                            [lunch, dinner],
                        )
                    )
                    break
        return out


class LinkedPairMarked:
    """A dinner of an extend_to_dinner recipe is flagged extended and linked both ways to its lunch."""

    code = "linked_pair_marked"

    def check(self, plan: PlanView, context: RuleContext) -> list[RuleViolation]:
        out = []
        for day in context.days:
            lunch, dinner = plan.at(day, MealType.LUNCH), plan.at(day, MealType.DINNER)
            if lunch is None or dinner is None or dinner.recipe_id not in context.recipes:
                continue
            paired = context.is_extendable(dinner.recipe_id)
            ok = (
                dinner.extended_from_lunch == paired
                and dinner.linked_assignment_id == (lunch.assignment_id if paired else None)
                and lunch.linked_assignment_id == (dinner.assignment_id if paired else None)
            )
            if not ok:
                state = "linked" if paired else "not linked"
                out.append(_violation(self.code, f"{day} lunch and dinner should be {state}", [lunch, dinner]))
        return out


WEEKLY_RULES: tuple[WeeklyRule, ...] = (
    SlotCoverage(),
    MealTypeEligible(),
    VariantConsistent(),
    WeeklyProteinCount(),
    ConsecutiveSameProtein(),
    FishDinnerWeeklyMax(),
    FishDinnerConsecutiveMax(),
    MealCarbsForbidden(),
    LunchCarbRequired(),
    ConsecutiveLunchSameCarb(),
    CarbWeeklyMax(),
    RecipeWeeklyMax(),
    ExtendToDinnerPair(),
    LinkedPairMarked(),
)


def check_weekly_rules(
    plan: Any,
    inputs: PlannerInputs,
    rules: Sequence[WeeklyRule] = WEEKLY_RULES,
) -> list[RuleViolation]:
    """Check a dated plan against every weekly rule; an empty list means it is valid.

    `plan` is a PlanSnapshot or its plan JSON mapping (anything with "slots").
    """
    mapping = getattr(plan, "plan", plan)
    view = PlanView.from_plan(mapping)
    context = RuleContext.from_inputs(inputs)
    violations: list[RuleViolation] = []
    for rule in rules:
        violations.extend(rule.check(view, context))
    return violations
