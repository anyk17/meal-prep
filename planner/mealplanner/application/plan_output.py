"""Pure construction of the plan output DTOs from a solution and explicit inputs."""

from datetime import datetime, timezone

from pydantic import BaseModel

from ..domain.catalog import ProteinType
from ..domain.planner_inputs import PlannerInputs
from ..domain.solution import MealPlanSolution
from ..domain.variants import materialize_variant


class IngredientItem(BaseModel):
    """Ingredient with display name and quantity."""
    item: str
    display: str
    qty: float
    unit: str
    role: str


class PlanSlot(BaseModel):
    """Output format for a single slot."""
    day: str
    meal: str
    recipeId: str
    recipeName: str
    variantId: str
    protein: str
    carb: str # "none" or carb_id
    proteinQty: float  # grams of protein
    carbQty: float | None  # grams of carb (None if carb == "none")
    ingredients: list[IngredientItem]  # full ingredients list
    extendedFromLunch: bool = False  # True if dinner is extension of lunch


class PlanDerivedStats(BaseModel):
    """Derived statistics from the plan."""
    protein_counts: dict[str, int]
    carb_counts: dict[str, int]


class PlanOutput(BaseModel):
    """Final JSON output structure."""
    seed: int
    generated_at: str
    slots: list[PlanSlot]
    derived: PlanDerivedStats


def build_plan_output(
    solution: MealPlanSolution,
    inputs: PlannerInputs,
    seed: int,
    generated_at: str | None = None,
) -> PlanOutput:
    """Build the plan DTO entirely in memory from a solution and explicit inputs."""
    slots: list[PlanSlot] = []
    
    # Stats tracking
    protein_counts = {pt.value: 0 for pt in ProteinType}
    carb_counts = {}
    
    # Sort slots by day then meal
    sorted_slots = sorted(
        solution.assignments.keys(),
        key=lambda s: (
            inputs.rules.week.days.index(s.split('_')[0]),
            inputs.rules.week.meals.index(s.split('_')[1])
        )
    )
    
    for slot_id in sorted_slots:
        variant = solution.assignments[slot_id]
        parts = slot_id.split('_')
        day = parts[0]
        meal = parts[1]
        
        content = materialize_variant(
            variant, meal, inputs.rules, inputs.ingredients
        )
        pt = variant.recipe.tags.primary_protein
        protein_counts[pt.value] += 1
        carb = content.carb or "none"
        if content.carb:
            carb_counts[carb] = carb_counts.get(carb, 0) + 1
        protein_qty = content.protein_qty
        carb_qty = content.carb_qty
        ingredients_list = [
            IngredientItem(
                item=line.item,
                display=line.display,
                qty=line.qty,
                unit=line.unit,
                role=line.role,
            )
            for line in content.ingredients
        ]

        # Check if this is a dinner slot extended from lunch
        is_extended = (
            meal == "dinner"
            and variant.recipe.extend_to_dinner
        )
        
        slots.append(PlanSlot(
            day=day,
            meal=meal,
            recipeId=variant.base_recipe_id,
            recipeName=variant.recipe.name,
            variantId=variant.variant_id,
            protein=pt.value,
            carb=carb,
            proteinQty=protein_qty,
            carbQty=carb_qty,
            ingredients=ingredients_list,
            extendedFromLunch=is_extended
        ))
        
    return PlanOutput(
        seed=seed,
        generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
        slots=slots,
        derived=PlanDerivedStats(
            protein_counts=protein_counts,
            carb_counts=carb_counts
        )
    )
