"""The result of plan generation: one chosen recipe variant per weekly slot."""

from pydantic import BaseModel

from .variants import RecipeVariant


class MealPlanSolution(BaseModel):
    """A generated meal plan, independent of the engine that produced it."""

    # Map slot ID ("mon_lunch") -> chosen variant
    assignments: dict[str, RecipeVariant]
