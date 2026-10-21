"""Plan rendering and output generation."""

from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from .config import ProteinType
from .planner_inputs import PlannerInputs
from .shopping import ShoppingList
from .solve import MealPlanSolution


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
        
        # Track protein
        pt = variant.recipe.tags.primary_protein
        protein_counts[pt.value] += 1
        
        # Resolve protein quantity
        protein_portions = inputs.rules.protein_portions_g[pt]
        protein_qty = getattr(protein_portions, meal, 0)
        
        # Track carb and resolve carb quantity
        carb = "none"
        carb_qty = None
        if variant.carb_ingredient_id:
            carb = variant.carb_ingredient_id
            carb_counts[carb] = carb_counts.get(carb, 0) + 1
            # Get carb quantity from ingredients.yml
            if carb in inputs.ingredients:
                carb_qty = inputs.ingredients[carb].default_qty_g or 0
        
        # Build ingredients list
        ingredients_list = []
        for ing in variant.recipe.ingredients:
            if ing.item not in inputs.ingredients:
                continue
            
            ingredient_def = inputs.ingredients[ing.item]
            
            # Resolve quantity
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
            
            ingredients_list.append(IngredientItem(
                item=ing.item,
                display=ingredient_def.display,
                qty=qty,
                unit=ingredient_def.unit,
                role=ing.role
            ))
        
        # Add carb ingredient if present
        if variant.carb_ingredient_id and variant.carb_ingredient_id in inputs.ingredients:
            carb_def = inputs.ingredients[variant.carb_ingredient_id]
            ingredients_list.append(IngredientItem(
                item=variant.carb_ingredient_id,
                display=carb_def.display,
                qty=carb_qty or 0,
                unit=carb_def.unit,
                role="carb"
            ))
            
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
    
def write_plan_output(plan: PlanOutput, output_dir: Path) -> None:
    """Write a previously built plan DTO to the legacy JSON location."""
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(output_dir / "plan.json", "w") as f:
        f.write(plan.model_dump_json(indent=2))


def render_plan(
    solution: MealPlanSolution,
    inputs: PlannerInputs,
    seed: int,
    output_dir: Path,
) -> PlanOutput:
    """Build and write plan.json, retaining the legacy console summary."""
    plan = build_plan_output(solution, inputs, seed)
    render_plan_output(plan, output_dir)
    return plan


def render_plan_output(plan: PlanOutput, output_dir: Path) -> None:
    """Write plan.json and print its established statistics summary."""
    write_plan_output(plan, output_dir)
    print(f"✓ Wrote plan.json to {output_dir}")
    
    # 4. Write stats summary to console
    print("\nPlan Statistics:")
    print("Protein Counts:")
    for pt, count in plan.derived.protein_counts.items():
        print(f"  {pt}: {count}")
        
    print("\nCarb Counts:")
    for carb, count in plan.derived.carb_counts.items():
        print(f"  {carb}: {count}")


def write_shopping_output(shopping_list: ShoppingList, output_dir: Path) -> None:
    """Write shopping_list.json using the established DTO casing and location."""
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(output_dir / "shopping_list.json", "w") as f:
        f.write(shopping_list.model_dump_json(indent=2))
