"""Explicit, validated inputs consumed by planner generation services."""

from collections.abc import Mapping
from dataclasses import dataclass

from .catalog import Ingredient, Recipe, Rules
from .policies import planner_input_errors, validate_weekly_protein_counts


@dataclass(frozen=True)
class PlannerInputs:
    """All configuration needed to generate a plan, independent of its source."""

    recipes: Mapping[str, Recipe]
    ingredients: Mapping[str, Ingredient]
    rules: Rules
    pantry: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_weekly_protein_counts(self.rules)
        errors = [
            f"Recipe mapping key {recipe_id!r} does not match recipe id {recipe.id!r}"
            for recipe_id, recipe in self.recipes.items()
            if recipe_id != recipe.id
        ]
        errors.extend(
            planner_input_errors(
                self.recipes,
                self.ingredients,
                self.rules,
                self.pantry,
            )
        )
        if errors:
            raise ValueError(
                "Invalid planner inputs:\n" + "\n".join(f"- {error}" for error in errors)
            )
