"""Shopping list generation."""

from collections import defaultdict
from typing import Optional

from pydantic import BaseModel

from ..domain.planner_inputs import PlannerInputs
from ..solver.solve import MealPlanSolution


class ShoppingItem(BaseModel):
    """Item on the shopping list."""
    item: str
    display: str
    quantity: float
    unit: str
    section: str
    notes: Optional[str] = None


class ShoppingList(BaseModel):
    """Categorized shopping list."""
    sections: dict[str, list[ShoppingItem]]
    
    class Config:
        arbitrary_types_allowed = True


def generate_shopping_list(
    solution: MealPlanSolution, inputs: PlannerInputs
) -> ShoppingList:
    """Generate shopping list from meal plan."""
    
    # map: ingredient_id -> quantity
    aggregated: dict[str, float] = defaultdict(float)
    
    # 1. Iterate through all assigned slots
    for slot_id, variant in solution.assignments.items():
        base_recipe = variant.recipe
        
        # Get the meal type for this slot (from slot ID or variant context if available)
        # We need the meal type to look up protein portions
        # Slot ID format: "day_meal" (e.g. "mon_lunch")
        parts = slot_id.split("_")
        meal_name = parts[-1] # "breakfast", "lunch", "dinner"
        
        # 2. Add recipe ingredients
        for ing in base_recipe.ingredients:
            qty = 0.0
            
            if ing.qty == "@portion":
                # Resolve protein portion
                protein_type = base_recipe.tags.primary_protein
                portions = inputs.rules.protein_portions_g[protein_type]
                qty = getattr(portions, meal_name, 0)
                
            elif ing.qty_g:
                qty = ing.qty_g
            elif ing.qty_ml:
                qty = ing.qty_ml
            elif ing.qty_units:
                qty = ing.qty_units
                
            if qty > 0:
                aggregated[ing.item] += qty
                
        # 3. Add carb ingredient (if variant has one)
        if variant.carb_ingredient_id:
            carb_id = variant.carb_ingredient_id
            
            # Resolve carb portion from ingredients.yml
            if carb_id in inputs.ingredients:
                ingredient = inputs.ingredients[carb_id]
                qty = ingredient.default_qty_g or 0
                
                if qty > 0:
                    aggregated[carb_id] += qty
                
    # 4. Remove pantry items
    for pantry_item in inputs.pantry:
        if pantry_item in aggregated:
            del aggregated[pantry_item]
            
    # 5. Build final list objects
    items_by_section: dict[str, list[ShoppingItem]] = defaultdict(list)
    
    for ing_id, qty in aggregated.items():
        if ing_id not in inputs.ingredients:
             # Should not happen due to validation
             continue
             
        ingredient = inputs.ingredients[ing_id]
        
        item = ShoppingItem(
            item=ing_id,
            display=ingredient.display,
            quantity=round(qty, 2),
            unit=ingredient.unit,
            section=ingredient.section
        )
        items_by_section[ingredient.section].append(item)
        
    return ShoppingList(sections=dict(items_by_section))
