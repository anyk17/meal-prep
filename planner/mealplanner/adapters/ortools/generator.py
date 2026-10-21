"""OR-Tools implementation of the PlanGenerator port."""

from __future__ import annotations

from ...domain.planner_inputs import PlannerInputs
from ...domain.solution import MealPlanSolution
from .solve import solve_plan


class OrToolsPlanGenerator:
    """Generates a weekly solution with CP-SAT. max_time_seconds bounds each solve (None = unbounded)."""

    def __init__(self, max_time_seconds: float | None = None) -> None:
        self.max_time_seconds = max_time_seconds

    def generate(self, inputs: PlannerInputs, seed: int) -> MealPlanSolution:
        return solve_plan(inputs, seed, self.max_time_seconds)
