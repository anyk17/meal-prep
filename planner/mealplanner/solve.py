"""Solver constraint implementation."""

from ortools.sat.python import cp_model
from pydantic import BaseModel

from .config import Rules, MealType, ProteinType, IngredientKind, Ingredient
from .model import PlannerModel, Slot, create_model
from .variants import RecipeVariant, expand_variants
from .planner_inputs import PlannerInputs


class MealPlanSolution(BaseModel):
    """A valid meal plan solution."""
    
    # Map slot ID -> Chosen Variant
    assignments: dict[str, RecipeVariant]
    
    class Config:
        arbitrary_types_allowed = True


def add_weekly_protein_constraints(model: PlannerModel, rules: Rules) -> None:
    """Implement constraint 1: Weekly protein counts."""
    # Sum of all slots for each protein type must equal the configured count
    
    for protein_type, count in rules.constraints.weekly_protein_counts.items():
        relevant_vars = []
        
        for slot in model.slots:
            slot_vars = model.vars[slot.id]
            for variant in model.variants:
                if variant.variant_id in slot_vars:
                    # check if this variant uses this protein
                    if variant.recipe.tags.primary_protein == protein_type:
                        relevant_vars.append(slot_vars[variant.variant_id])
                        
        if relevant_vars:
            model.model.Add(sum(relevant_vars) == count)


def add_consecutive_protein_constraints(model: PlannerModel, rules: Rules) -> None:
    """Implement constraint 2: No consecutive same protein."""
    if not rules.constraints.no_consecutive_same_protein:
        return
        
    for protein_type in ProteinType:
        # Check per meal type (planning doc says "Applied per meal type")
        for meal in rules.week.meals:
            # For each day d and d+1
            for day_idx in range(len(rules.week.days) - 1):
                # Get vars for (day, meal) and (day+1, meal)
                current_slot = model.slots[day_idx * len(rules.week.meals) + rules.week.meals.index(meal)]
                next_slot = model.slots[(day_idx + 1) * len(rules.week.meals) + rules.week.meals.index(meal)]
                
                # Verify we got the right slots (sanity check)
                assert current_slot.day_index == day_idx and current_slot.meal == meal
                assert next_slot.day_index == day_idx + 1 and next_slot.meal == meal
                
                current_vars = []
                next_vars = []
                
                # Collect vars for this protein type
                for variant in model.variants:
                    if variant.recipe.tags.primary_protein == protein_type:
                        if variant.variant_id in model.vars[current_slot.id]:
                            current_vars.append(model.vars[current_slot.id][variant.variant_id])
                        if variant.variant_id in model.vars[next_slot.id]:
                            next_vars.append(model.vars[next_slot.id][variant.variant_id])
                            
                # Constraint: term1 + term2 <= 1
                # If both have protein P, sum is 2, which is > 1 -> disallowed
                if current_vars and next_vars:
                    # We sum all variants of protein P for the slot (should only be 1 selected anyway)
                    # Let A = sum(current_vars), B = sum(next_vars)
                    # A + B <= 1
                    model.model.Add(sum(current_vars) + sum(next_vars) <= 1)


def add_fish_dinner_constraints(model: PlannerModel, rules: Rules) -> None:
    """Implement constraints 3 & 4: Fish dinner limits."""
    
    # Collect all fish dinner variables by day
    fish_vars_by_day = []
    
    for day_idx in range(len(rules.week.days)):
        # Find dinner slot for this day
        # Assumes meals are ordered and dinner is last, but safe way is to find it
        dinner_slot = next(s for s in model.slots if s.day_index == day_idx and s.meal == MealType.DINNER)
        
        day_fish_vars = []
        for variant in model.variants:
            if variant.recipe.tags.primary_protein == ProteinType.FISH:
                if variant.variant_id in model.vars[dinner_slot.id]:
                    day_fish_vars.append(model.vars[dinner_slot.id][variant.variant_id])
        
        fish_vars_by_day.append(sum(day_fish_vars))
        
    # Constraint 3: Max total fish dinners
    model.model.Add(sum(fish_vars_by_day) <= rules.constraints.fish_dinner_max_per_week)
    
    # Constraint 4: Max consecutive fish dinners (sliding window)
    window_size = 3 # d, d+1, d+2 (max 2 means sum <= 2)
    # The requirement is "max 2 consecutive", meaning NO 3 in a row.
    # So sum of any 3 consecutive days <= 2
    
    for i in range(len(fish_vars_by_day) - 2):
        model.model.Add(
            fish_vars_by_day[i] + 
            fish_vars_by_day[i+1] + 
            fish_vars_by_day[i+2] 
            <= rules.constraints.fish_dinner_max_consecutive
        )


def add_meal_carb_rules(model: PlannerModel, rules: Rules) -> None:
    """Implement constraint 5: Meal carb rules (no carbs at dinner)."""
    
    for meal_type, rule in rules.meal_rules.items():
        if not rule.allow_carbs:
            # Find all slots of this meal type
            for slot in model.slots:
                if slot.meal == meal_type:
                    # Ensure NO variant with carbs is selected
                    carb_vars = []
                    for variant in model.variants:
                        if variant.is_carb_variant:
                            if variant.variant_id in model.vars[slot.id]:
                                carb_vars.append(model.vars[slot.id][variant.variant_id])
                                
                    if carb_vars:
                         model.model.Add(sum(carb_vars) == 0)


def add_carb_frequency_constraints(
    model: PlannerModel, ingredients: dict[str, Ingredient]
) -> None:
    """Implement constraint 6: Carb frequency limits."""
    
    # Identify all limited carbs
    limited_carbs = {
        ing_id: ing.max_times_week 
        for ing_id, ing in ingredients.items() 
        if ing.kind == IngredientKind.CARB and ing.max_times_week is not None
    }
    
    for carb_id, limit in limited_carbs.items():
        relevant_vars = []
        
        for slot in model.slots:
            slot_vars = model.vars[slot.id]
            for variant in model.variants:
                if variant.carb_ingredient_id == carb_id:
                    if variant.variant_id in slot_vars:
                        relevant_vars.append(slot_vars[variant.variant_id])
                        
        if relevant_vars:
            model.model.Add(sum(relevant_vars) <= int(limit))


def add_lunch_carb_required(model: PlannerModel, rules: Rules) -> None:
    """Implement constraint: All lunch slots must have a carb variant.
    
    For each lunch slot, block the carb_none variants so the solver
    must pick a variant that includes a carbohydrate.
    """
    for slot in model.slots:
        if slot.meal == MealType.LUNCH:
            no_carb_vars = []
            for variant in model.variants:
                if not variant.is_carb_variant:
                    if variant.variant_id in model.vars[slot.id]:
                        no_carb_vars.append(model.vars[slot.id][variant.variant_id])
            if no_carb_vars:
                model.model.Add(sum(no_carb_vars) == 0)


def add_no_consecutive_same_carb(
    model: PlannerModel, rules: Rules
) -> None:
    """Implement constraint: No same carb on consecutive-day lunches.
    
    For each pair of consecutive days and for each carb type, at most
    one of the two lunch slots can use that carb.
    """
    # Collect lunch slots ordered by day index
    lunch_slots = sorted(
        [s for s in model.slots if s.meal == MealType.LUNCH],
        key=lambda s: s.day_index,
    )
    
    # Collect all distinct carb IDs from variants
    carb_ids = {v.carb_ingredient_id for v in model.variants if v.carb_ingredient_id}
    
    for i in range(len(lunch_slots) - 1):
        slot_a = lunch_slots[i]
        slot_b = lunch_slots[i + 1]
        
        for carb_id in carb_ids:
            vars_a = []
            vars_b = []
            for variant in model.variants:
                if variant.carb_ingredient_id == carb_id:
                    if variant.variant_id in model.vars[slot_a.id]:
                        vars_a.append(model.vars[slot_a.id][variant.variant_id])
                    if variant.variant_id in model.vars[slot_b.id]:
                        vars_b.append(model.vars[slot_b.id][variant.variant_id])
            
            if vars_a and vars_b:
                model.model.Add(sum(vars_a) + sum(vars_b) <= 1)


def add_recipe_frequency_constraints(model: PlannerModel, rules: Rules) -> None:
    """Implement constraint 7: Max once per week per base recipe."""
    
    # Group variants by base recipe
    base_recipes: dict[str, list[RecipeVariant]] = {}
    for variant in model.variants:
        base_recipes.setdefault(variant.base_recipe_id, []).append(variant)
        
    for base_id, variants in base_recipes.items():
        is_extendable = any(variant.recipe.extend_to_dinner for variant in variants)

        if is_extendable:
            day_uses = []
            for day_idx in range(len(rules.week.days)):
                lunch_slot = next(
                    s for s in model.slots
                    if s.day_index == day_idx and s.meal == MealType.LUNCH
                )
                lunch_vars = [
                    model.vars[lunch_slot.id][variant.variant_id]
                    for variant in variants
                    if variant.variant_id in model.vars[lunch_slot.id]
                ]
                if lunch_vars:
                    day_uses.append(sum(lunch_vars))

            if day_uses:
                model.model.Add(
                    sum(day_uses) <= rules.constraints.max_recipe_uses_per_week
                )
            continue

        all_vars = []
        for slot in model.slots:
            for variant in variants:
                if variant.variant_id in model.vars[slot.id]:
                    all_vars.append(model.vars[slot.id][variant.variant_id])

        if all_vars:
            model.model.Add(
                sum(all_vars) <= rules.constraints.max_recipe_uses_per_week
            )


def add_extend_to_dinner_constraints(model: PlannerModel, rules: Rules) -> None:
    """Link lunch and dinner slots for extend_to_dinner recipes.
    
    If a recipe with extend_to_dinner is assigned for lunch on day X,
    the same base recipe must also be assigned for dinner on day X,
    and vice versa (they always appear as a pair or not at all).
    """
    # Identify extendable base recipe IDs
    extendable_ids = {
        v.base_recipe_id for v in model.variants
        if v.recipe.extend_to_dinner
    }
    
    if not extendable_ids:
        return
    
    for day_idx in range(len(rules.week.days)):
        lunch_slot = next(
            s for s in model.slots
            if s.day_index == day_idx and s.meal == MealType.LUNCH
        )
        dinner_slot = next(
            s for s in model.slots
            if s.day_index == day_idx and s.meal == MealType.DINNER
        )
        
        for base_id in extendable_ids:
            # Collect lunch vars for this base recipe
            lunch_vars = [
                model.vars[lunch_slot.id][v.variant_id]
                for v in model.variants
                if v.base_recipe_id == base_id
                and v.variant_id in model.vars[lunch_slot.id]
            ]
            # Collect dinner vars for this base recipe
            dinner_vars = [
                model.vars[dinner_slot.id][v.variant_id]
                for v in model.variants
                if v.base_recipe_id == base_id
                and v.variant_id in model.vars[dinner_slot.id]
            ]
            
            if lunch_vars and dinner_vars:
                # lunch_used == dinner_used
                # Both are 0 (recipe not used) or both are 1 (paired)
                model.model.Add(sum(lunch_vars) == sum(dinner_vars))


def solve_plan(inputs: PlannerInputs, seed: int = 42) -> MealPlanSolution:
    """Generate a meal plan."""
    
    # 1. Expand variants
    variants = expand_variants(dict(inputs.recipes), dict(inputs.ingredients))
    
    # 2. Create model
    planner_model = create_model(inputs.rules, variants)
    
    # 3. Add constraints
    add_weekly_protein_constraints(planner_model, inputs.rules)
    add_consecutive_protein_constraints(planner_model, inputs.rules)
    add_fish_dinner_constraints(planner_model, inputs.rules)
    add_meal_carb_rules(planner_model, inputs.rules)
    add_lunch_carb_required(planner_model, inputs.rules)
    add_no_consecutive_same_carb(planner_model, inputs.rules)
    add_carb_frequency_constraints(planner_model, inputs.ingredients)
    add_recipe_frequency_constraints(planner_model, inputs.rules)
    add_extend_to_dinner_constraints(planner_model, inputs.rules)
    
    # 4. Solve
    solver = cp_model.CpSolver()
    solver.parameters.random_seed = seed
    solver.parameters.num_workers = 1  # single-thread for deterministic results
    
    status = solver.Solve(planner_model.model)
    
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assignments = {}
        for slot in planner_model.slots:
            slot_vars = planner_model.vars[slot.id]
            for variant in planner_model.variants:
                 if variant.variant_id in slot_vars:
                     if solver.Value(slot_vars[variant.variant_id]) == 1:
                         assignments[slot.id] = variant
                         break
                         
        return MealPlanSolution(assignments=assignments)
        
    else:
        raise RuntimeError("No feasible solution found")
