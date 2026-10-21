"""Pure generation services shared by command-line and other input adapters."""

from dataclasses import dataclass

from .planner_inputs import PlannerInputs
from .render import PlanOutput, build_plan_output
from .shopping import ShoppingList, generate_shopping_list
from .solve import MealPlanSolution, solve_plan


@dataclass(frozen=True)
class GeneratedPlan:
    """In-memory result of one complete plan generation request."""

    solution: MealPlanSolution
    plan: PlanOutput
    shopping_list: ShoppingList


def generate(
    inputs: PlannerInputs, seed: int = 42, generated_at: str | None = None
) -> GeneratedPlan:
    """Solve and construct all output DTOs without writing files."""
    solution = solve_plan(inputs, seed)
    shopping_list = generate_shopping_list(solution, inputs)
    plan = build_plan_output(solution, inputs, seed, generated_at=generated_at)
    return GeneratedPlan(solution=solution, plan=plan, shopping_list=shopping_list)
