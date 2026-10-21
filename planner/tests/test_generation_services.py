"""Behavior checks for in-memory generation and rendering services."""

import unittest
from pathlib import Path
from unittest.mock import patch

from mealplanner.domain.catalog import MealType
from mealplanner.application.generation import generate
from mealplanner.domain.planner_inputs import PlannerInputs
from mealplanner.application.plan_output import build_plan_output
from mealplanner.application.shopping import generate_shopping_list
from mealplanner.solver.solve import MealPlanSolution
from mealplanner.domain.variants import RecipeVariant
from mealplanner.adapters.yaml_inputs import YamlPlannerInputReader

ROOT = Path(__file__).resolve().parents[2]


class GenerationServicesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.inputs: PlannerInputs = YamlPlannerInputReader(ROOT / "data").read()
        recipe = cls.inputs.recipes["pollo_toscano_lunch"]
        variant = RecipeVariant(
            base_recipe_id=recipe.id,
            variant_id=f"{recipe.id}__carb_yuca",
            recipe=recipe,
            carb_ingredient_id="yuca",
        )
        cls.solution = MealPlanSolution(assignments={"mon_lunch": variant})

    def test_plan_builder_preserves_legacy_dto_fields_and_quantities(self) -> None:
        plan = build_plan_output(
            self.solution,
            self.inputs,
            seed=5,
            generated_at="2026-01-01T00:00:00+00:00",
        )
        payload = plan.model_dump()

        self.assertEqual(payload["seed"], 5)
        self.assertEqual(payload["generated_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(
            payload["derived"]["protein_counts"],
            {
                "chicken": 1,
                "beef": 0,
                "pork": 0,
                "fish": 0,
                "egg": 0,
            },
        )
        self.assertEqual(payload["derived"]["carb_counts"], {"yuca": 1})
        slot = payload["slots"][0]
        self.assertEqual(
            {
                key: slot[key]
                for key in (
                    "day",
                    "meal",
                    "recipeId",
                    "recipeName",
                    "variantId",
                    "protein",
                    "carb",
                    "proteinQty",
                    "carbQty",
                    "extendedFromLunch",
                )
            },
            {
                "day": "mon",
                "meal": "lunch",
                "recipeId": "pollo_toscano_lunch",
                "recipeName": "Pollo toscano",
                "variantId": "pollo_toscano_lunch__carb_yuca",
                "protein": "chicken",
                "carb": "yuca",
                "proteinQty": 260.0,
                "carbQty": 70.0,
                "extendedFromLunch": False,
            },
        )
        self.assertEqual(
            [
                (item["item"], item["qty"], item["unit"], item["role"])
                for item in slot["ingredients"]
            ],
            [
                ("chicken", 260.0, "g", "protein"),
                ("cherry_tomato", 100.0, "g", "veg"),
                ("spinach", 80.0, "g", "veg"),
                ("onion", 50.0, "g", "veg"),
                ("green_onion", 20.0, "g", "veg"),
                ("garlic", 10.0, "g", "veg"),
                ("olives", 30.0, "g", "other"),
                ("basil", 5.0, "g", "herb"),
                ("greek_yogurt", 50.0, "g", "dairy"),
                ("olive_oil", 10.0, "ml", "fat"),
                ("yuca", 70.0, "g", "carb"),
            ],
        )

    def test_shopping_builder_applies_pantry_and_keeps_explicit_units(self) -> None:
        shopping = generate_shopping_list(self.solution, self.inputs)
        rows = {
            row.item: (row.display, row.quantity, row.unit, row.section, row.notes)
            for section_rows in shopping.sections.values()
            for row in section_rows
        }

        self.assertEqual(
            rows,
            {
                "chicken": ("Pollo", 260.0, "g", "protein", None),
                "cherry_tomato": ("Tomate cherry", 100.0, "g", "vegetable", None),
                "spinach": ("Espinaca", 80.0, "g", "vegetable", None),
                "olives": ("Aceitunas", 30.0, "g", "condiment", None),
                "greek_yogurt": ("Yogurt griego", 50.0, "g", "dairy", None),
                "yuca": ("Yuca", 70.0, "g", "carb", None),
            },
        )
        self.assertNotIn("onion", rows)
        self.assertNotIn("olive_oil", rows)

    def test_explicit_inputs_enforce_shared_recipe_rules(self) -> None:
        recipe = self.inputs.recipes["pollo_toscano_lunch"].model_copy(
            update={"meal_types": [MealType.LUNCH]}
        )
        recipes = dict(self.inputs.recipes)
        recipes[recipe.id] = recipe

        with self.assertRaisesRegex(
            ValueError,
            "extend_to_dinner requires both lunch and dinner in meal_types",
        ):
            PlannerInputs(
                recipes=recipes,
                ingredients=self.inputs.ingredients,
                rules=self.inputs.rules,
                pantry=self.inputs.pantry,
            )

    def test_generation_service_returns_dtos_without_writing_files(self) -> None:
        with (
            patch.object(Path, "mkdir", side_effect=AssertionError("unexpected file output")),
            patch.object(Path, "write_text", side_effect=AssertionError("unexpected file output")),
            patch.object(Path, "write_bytes", side_effect=AssertionError("unexpected file output")),
            patch.object(Path, "open", side_effect=AssertionError("unexpected file output")),
            patch("builtins.open", side_effect=AssertionError("unexpected file output")),
        ):
            result = generate(self.inputs, seed=42)
            self.assertTrue(result.plan.slots)
            self.assertTrue(result.shopping_list.sections)


if __name__ == "__main__":
    unittest.main()
