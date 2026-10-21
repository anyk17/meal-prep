"""CP-SAT model definition."""

from typing import NamedTuple
from ortools.sat.python import cp_model
from pydantic import BaseModel

from ...domain.catalog import Rules, MealType, ProteinType
from ...domain.variants import RecipeVariant


class Slot(NamedTuple):
    """A specific meal slot in the week."""
    day_index: int
    day_name: str
    meal: MealType
    
    @property
    def id(self) -> str:
        """Unique slot ID."""
        return f"{self.day_name}_{self.meal.value}"


class PlannerModel(BaseModel):
    """Encapsulates the CP-SAT model and decision variables."""
    
    model: cp_model.CpModel
    slots: list[Slot]
    variants: list[RecipeVariant]
    
    # Decision variables: vars[slot_index][variant_index] -> BoolVar
    # True if slot i uses variant j
    vars: dict[str, dict[str, cp_model.IntVar]]
    
    class Config:
        arbitrary_types_allowed = True


def create_model(rules: Rules, variants: list[RecipeVariant]) -> PlannerModel:
    """Initialize the CP-SAT model and decision variables."""
    model = cp_model.CpModel()
    
    # define slots
    slots: list[Slot] = []
    for day_idx, day in enumerate(rules.week.days):
        for meal in rules.week.meals:
            slots.append(Slot(day_idx, day, meal))
            
    # Create variables
    # For each slot, we have a boolean variable for each recipe variant
    # slot_day_meal_variant -> bool
    
    vars: dict[str, dict[str, cp_model.IntVar]] = {}
    
    for slot in slots:
        slot_vars = {}
        vars[slot.id] = slot_vars
        
        # Create a variable for each variant in this slot
        variant_vars = []
        for variant in variants:
            # Check if this variant is allowed for this meal type
            if slot.meal in variant.recipe.meal_types:
                var_name = f"{slot.id}_{variant.variant_id}"
                var = model.NewBoolVar(var_name)
                slot_vars[variant.variant_id] = var
                variant_vars.append(var)
            
            # If recipe not allowed for this meal, we don't create a variable (effectively 0)
            
        # Constraint: Exactly one recipe per slot
        if variant_vars:
            model.Add(sum(variant_vars) == 1)
        else:
             # Should not happen if data is valid (at least one recipe per meal type)
             # But if it does, the model is infeasible
             pass
             
    return PlannerModel(
        model=model,
        slots=slots,
        variants=variants,
        vars=vars
    )
