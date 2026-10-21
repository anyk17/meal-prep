"""Pure generation services shared by command-line and other input adapters."""

from dataclasses import dataclass

from ..domain.planner_inputs import PlannerInputs
from ..domain.solution import MealPlanSolution
from .plan_output import PlanOutput, build_plan_output
from .ports import PlanGenerator
from .shopping import ShoppingList, generate_shopping_list


@dataclass(frozen=True)
class GeneratedPlan:
    """In-memory result of one complete plan generation request."""

    solution: MealPlanSolution
    plan: PlanOutput
    shopping_list: ShoppingList


def generate(
    inputs: PlannerInputs,
    generator: PlanGenerator,
    seed: int = 42,
    generated_at: str | None = None,
) -> GeneratedPlan:
    """Generate a solution through the port and construct all output DTOs without writing files."""
    solution = generator.generate(inputs, seed)
    shopping_list = generate_shopping_list(solution, inputs)
    plan = build_plan_output(solution, inputs, seed, generated_at=generated_at)
    return GeneratedPlan(solution=solution, plan=plan, shopping_list=shopping_list)
