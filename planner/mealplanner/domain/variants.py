"""Recipe variant expansion."""

from dataclasses import dataclass
from typing import Mapping, Optional
from pydantic import BaseModel

from .catalog import Recipe, CarbStrategy, IngredientKind, Ingredient, Rules


class RecipeVariant(BaseModel):
    """A specific variant of a recipe with a chosen carb option."""

    base_recipe_id: str
    variant_id: str
    recipe: Recipe
    carb_ingredient_id: Optional[str]  # None if no carb
    
    @property
    def is_carb_variant(self) -> bool:
        """Return True if this variant has a carb."""
        return self.carb_ingredient_id is not None


def expand_variants(
    recipes: dict[str, Recipe], ingredients: dict[str, Ingredient]
) -> list[RecipeVariant]:
    """Expand recipes into variants based on carb strategy.
    
    Returns a list of all possible recipe variants.
    """
    variants: list[RecipeVariant] = []

    for recipe_id, recipe in recipes.items():
        strategy = recipe.carbs.strategy
        
        if strategy == CarbStrategy.NONE:
            # Single variant: no carb
            variants.append(
                RecipeVariant(
                    base_recipe_id=recipe_id,
                    variant_id=f"{recipe_id}__carb_none",
                    recipe=recipe,
                    carb_ingredient_id=None,
                )
            )
            
        elif strategy == CarbStrategy.FIXED:
            # Single variant: fixed default carb
            carb_id = recipe.carbs.default
            if not carb_id:
                 # Should be caught by validation, but safe fallback
                 continue
                 
            variants.append(
                RecipeVariant(
                    base_recipe_id=recipe_id,
                    variant_id=f"{recipe_id}__carb_{carb_id}",
                    recipe=recipe,
                    carb_ingredient_id=carb_id,
                )
            )
            
        elif strategy == CarbStrategy.OPTIONAL:
            # Multiple variants: one for each allowed carb + one for no carb
            # The no-carb variant is needed for unified recipes that support both lunch and dinner
            # (dinner slots will only use the carb_none variant due to meal_rules)
            
            # Create a variant for each allowed carb
            for carb_id in recipe.carbs.allowed:
                variants.append(
                    RecipeVariant(
                        base_recipe_id=recipe_id,
                        variant_id=f"{recipe_id}__carb_{carb_id}",
                        recipe=recipe,
                        carb_ingredient_id=carb_id,
                    )
                )
            
            # Also create a no-carb variant for dinner compatibility
            variants.append(
                RecipeVariant(
                    base_recipe_id=recipe_id,
                    variant_id=f"{recipe_id}__carb_none",
                    recipe=recipe,
                    carb_ingredient_id=None,
                )
            )

    return variants


@dataclass(frozen=True)
class IngredientLine:
    """One resolved ingredient row of a materialized slot."""

    item: str
    display: str
    qty: float
    unit: str
    role: str


@dataclass(frozen=True)
class MaterializedSlot:
    """A variant resolved for one meal: portions, carb and the full ingredient rows."""

    protein: str
    protein_qty: float
    carb: Optional[str]  # carb ingredient id, None when the variant has no carb
    carb_qty: Optional[float]
    ingredients: tuple[IngredientLine, ...]


def materialize_variant(
    variant: RecipeVariant,
    meal: str,
    rules: Rules,
    ingredients: Mapping[str, Ingredient],
) -> MaterializedSlot:
    """Resolve a variant for a meal into protein/carb quantities and ingredient rows.

    The single place that decides how a variant becomes slot content; generation output
    (plan_output.py) and manual swaps (swap.py) both use it.
    """
    pt = variant.recipe.tags.primary_protein
    protein_qty = getattr(rules.protein_portions_g[pt], meal, 0)

    carb: Optional[str] = None
    carb_qty: Optional[float] = None
    if variant.carb_ingredient_id:
        carb = variant.carb_ingredient_id
        if carb in ingredients:
            carb_qty = ingredients[carb].default_qty_g or 0

    lines: list[IngredientLine] = []
    for ing in variant.recipe.ingredients:
        if ing.item not in ingredients:
            continue
        definition = ingredients[ing.item]
        if ing.qty == "@portion":
            qty = protein_qty
        elif ing.qty_g is not None:
            qty = ing.qty_g
        elif ing.qty_ml is not None:
            qty = ing.qty_ml
        elif ing.qty_units is not None:
            qty = ing.qty_units
        else:
            qty = 0
        lines.append(IngredientLine(ing.item, definition.display, qty, definition.unit, ing.role))

    if carb and carb in ingredients:
        carb_def = ingredients[carb]
        lines.append(IngredientLine(carb, carb_def.display, carb_qty or 0, carb_def.unit, "carb"))

    return MaterializedSlot(pt.value, protein_qty, carb, carb_qty, tuple(lines))
