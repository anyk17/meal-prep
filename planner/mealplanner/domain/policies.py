"""Source-independent validation shared by input adapters and domain values."""

from collections.abc import Mapping, Sequence

from .catalog import (
    Ingredient,
    IngredientKind,
    MealType,
    Recipe,
    Rules,
)


def validate_weekly_protein_counts(rules: Rules) -> None:
    """Require configured weekly protein counts to fill all meal slots."""
    total_meals = len(rules.week.days) * len(rules.week.meals)
    protein_sum = sum(rules.constraints.weekly_protein_counts.values())
    if protein_sum != total_meals:
        raise ValueError(
            f"Protein counts sum to {protein_sum} but should equal "
            f"{total_meals} ({len(rules.week.days)} days × "
            f"{len(rules.week.meals)} meals)"
        )


def validate_recipe(
    recipe_id: str,
    recipe: Recipe,
    ingredients: Mapping[str, Ingredient],
    rules: Rules,
) -> None:
    """Validate one recipe against the supplied ingredient and rule values."""
    for ing in recipe.ingredients:
        if ing.item not in ingredients:
            raise ValueError(f"Recipe {recipe_id} references unknown ingredient: {ing.item}")

        if ing.qty == "@portion":
            ingredient = ingredients[ing.item]
            if ingredient.kind != IngredientKind.PROTEIN:
                raise ValueError(
                    f"Recipe {recipe_id}: @portion can only be used on protein "
                    f"ingredients, but {ing.item} is {ingredient.kind}"
                )

    if recipe.carbs.strategy.value == "none":
        if recipe.carbs.allowed or recipe.carbs.default:
            raise ValueError(
                f"Recipe {recipe_id}: strategy 'none' cannot have allowed or default carbs"
            )
    elif recipe.carbs.strategy.value == "fixed":
        if not recipe.carbs.default:
            raise ValueError(f"Recipe {recipe_id}: strategy 'fixed' requires default carb")
        if recipe.carbs.default not in ingredients:
            raise ValueError(f"Recipe {recipe_id}: default carb {recipe.carbs.default} not found")
        if ingredients[recipe.carbs.default].kind != IngredientKind.CARB:
            raise ValueError(f"Recipe {recipe_id}: default {recipe.carbs.default} is not a carb")
    elif recipe.carbs.strategy.value == "optional":
        if not recipe.carbs.allowed:
            raise ValueError(f"Recipe {recipe_id}: strategy 'optional' requires allowed carbs list")
        if not recipe.carbs.default:
            raise ValueError(f"Recipe {recipe_id}: strategy 'optional' requires default carb")
        for carb_id in recipe.carbs.allowed:
            if carb_id not in ingredients:
                raise ValueError(f"Recipe {recipe_id}: allowed carb {carb_id} not found")
            if ingredients[carb_id].kind != IngredientKind.CARB:
                raise ValueError(f"Recipe {recipe_id}: allowed {carb_id} is not a carb")
        if recipe.carbs.default not in recipe.carbs.allowed:
            raise ValueError(f"Recipe {recipe_id}: default carb must be in allowed list")

    protein_type = recipe.tags.protein_group
    if protein_type not in rules.protein_portions_g:
        raise ValueError(
            f"Recipe {recipe_id}: protein type {protein_type.value} not found in rules"
        )

    portions = rules.protein_portions_g[protein_type]
    for meal_type in recipe.meal_types:
        if getattr(portions, meal_type.value, None) is None:
            raise ValueError(
                f"Recipe {recipe_id}: no portion defined for {protein_type.value} "
                f"at {meal_type.value}"
            )

    if recipe.extend_to_dinner and (
        MealType.LUNCH not in recipe.meal_types or MealType.DINNER not in recipe.meal_types
    ):
        raise ValueError(
            f"Recipe {recipe_id}: extend_to_dinner requires both lunch and dinner in meal_types"
        )


def planner_input_errors(
    recipes: Mapping[str, Recipe],
    ingredients: Mapping[str, Ingredient],
    rules: Rules,
    pantry: Sequence[str],
) -> list[str]:
    """Return cross-file domain errors without reading from any source."""
    errors: list[str] = []
    for ingredient_id in pantry:
        if ingredient_id not in ingredients:
            errors.append(f"Pantry references unknown ingredient: {ingredient_id}")

    for recipe_id, recipe in recipes.items():
        try:
            validate_recipe(recipe_id, recipe, ingredients, rules)
        except Exception as exc:
            errors.append(str(exc))
    return errors
