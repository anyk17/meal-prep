"""Protein portions remain consistent in meal content and shopping quantities."""
from pathlib import Path

import pytest

from mealplanner.adapters.yaml_inputs import YamlPlannerInputReader
from mealplanner.application.shopping import generate_shopping_list
from mealplanner.domain.catalog import Recipe, ProteinType
from mealplanner.domain.solution import MealPlanSolution
from mealplanner.domain.variants import RecipeVariant, materialize_variant

DATA = Path(__file__).resolve().parents[2] / "data"


@pytest.mark.parametrize("item,protein,lunch", [
    ("chicken", "chicken", 175),
    ("beef", "beef", 185),
    ("pork", "pork", 230),
    ("fish_salmon", "fish", 200),
    ("fish_tilapia", "fish", 200),
    ("fish_tuna_fillet", "fish", 170),
    ("fish_tuna_canned", "fish", 280),
])
@pytest.mark.parametrize("meal", ["lunch", "dinner"])
def test_materialized_and_shopping_portions(item, protein, lunch, meal):
    inputs = YamlPlannerInputReader(DATA).read()
    recipe = Recipe(
        id="portion_check", name="Portion check", meal_types=["lunch", "dinner"],
        tags={"primary_protein": protein}, carbs={"strategy": "none"},
        ingredients=[{"item": item, "role": "protein", "qty": "@portion"}],
    )
    variant = RecipeVariant(base_recipe_id=recipe.id, variant_id="portion_check__carb_none",
                            recipe=recipe, carb_ingredient_id=None)
    expected = lunch if meal == "lunch" else 100
    content = materialize_variant(variant, meal, inputs.rules, inputs.ingredients)
    assert content.protein_qty == expected
    assert content.ingredients[0].qty == expected
    solution = MealPlanSolution(assignments={f"mon_{meal}": variant})
    shopping = generate_shopping_list(solution, inputs)
    row = next(row for rows in shopping.sections.values() for row in rows if row.item == item)
    assert row.quantity == expected


def test_legacy_rules_without_overrides_keep_type_defaults():
    rules = YamlPlannerInputReader(DATA).read().rules.model_copy(update={"protein_portion_overrides_g": {}})
    assert rules.protein_portion(ProteinType.FISH, "fish_salmon", "lunch") == 280


@pytest.mark.parametrize("recipe_id,label,lunch", [
    ("salmon_horneado", "fish_salmon", 200),
    ("pescado_encocado", "fish_tilapia", 200),
    ("atun_juanfe", "fish_tuna_fillet", 170),
    ("pasta_tefy", "fish_tuna_canned", 280),
    ("atun_guisito_dinner", "fish_tuna_canned", 280),
])
def test_fish_recipe_labels_and_portions(recipe_id, label, lunch):
    inputs = YamlPlannerInputReader(DATA).read()
    recipe = inputs.recipes[recipe_id]
    assert recipe.tags.primary_protein == label
    assert recipe.tags.protein_group == ProteinType.FISH
    variant = RecipeVariant(base_recipe_id=recipe.id, variant_id="label_check",
                            recipe=recipe, carb_ingredient_id=None)
    for meal, expected in [("lunch", lunch), ("dinner", 100)]:
        content = materialize_variant(variant, meal, inputs.rules, inputs.ingredients)
        assert content.protein == "fish"
        assert content.protein_qty == expected


def test_fish_label_rejects_mismatched_portion_ingredient():
    recipe = YamlPlannerInputReader(DATA).read().recipes["salmon_horneado"].model_dump()
    recipe["tags"]["primary_protein"] = "fish_tilapia"
    with pytest.raises(ValueError, match="label must match"):
        Recipe.model_validate(recipe)
