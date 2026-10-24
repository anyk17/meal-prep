# Data Directory Structure

This directory contains all the configuration and recipe data for the meal planner.

## Overview

All measurements are for **RAW (uncooked) ingredients** unless otherwise specified.

## Files

### `rules.yml`
Global planning rules and constraints:
- **Week structure**: 7 days, 3 meals (breakfast, lunch, dinner)
- **Meal rules**: Carb policies per meal type
- **Protein portions**: Fixed portions by protein type and meal (in grams, raw)
- **Carb portions**: Fixed portions by meal with specific overrides (in grams, raw)
- **Constraints**: Weekly protein distribution, consecutive protein rules, fish limits, recipe frequency

### `ingredients.yml`
Canonical ingredient definitions (single source of truth):
- **Ingredient ID**: Lowercase canonical identifier (e.g., `chicken`, `rice`, `platano_verde`)
- **Display name**: Human-readable name in Spanish
- **Unit**: g, ml, or units
- **Section**: protein, carb, vegetable, dairy, fat, condiment, spice, other
- **Kind**: `protein`, `carb`, or `other`
- **Carb-specific fields**:
  - `default_qty_g`: Default serving size
  - `max_times_week`: Maximum weekly frequency

### `pantry.yml`
List of staple ingredients to exclude from shopping lists (always in stock).

### `recipes/*.yml`
Individual recipe definitions:
- **id**: Unique recipe identifier
- **name**: Display name
- **meal_types**: Allowed meals (breakfast, lunch, dinner)
- **tags.primary_protein**: Protein type (chicken, beef, pork, fish, egg)
- **carbs.strategy**: `none`, `fixed`, or `optional`
  - `none`: No carbs (typically for dinner)
  - `fixed`: Always includes a specific carb
  - `optional`: Solver chooses from allowed carbs
- **ingredients**: List with roles (protein, veg, fat, etc.)
  - Use `qty: "@portion"` for protein to resolve at planning time
  - Use `qty_g`, `qty_ml`, or `qty_units` for fixed quantities

## Recipe Naming Convention

For recipes that can be used at multiple meals with different carb strategies:
- Create **separate recipe files** for each variant
- Naming: `{recipe_name}_{meal_type}.yml`
- Examples:
  - `pollo_asiatico_lunch.yml` (with optional carbs)
  - `pollo_asiatico_dinner.yml` (no carbs)

## Current Recipes

The following recipes are currently implemented:

1.  **arroz_con_pollo.yml** - Arroz con pollo (fixed: rice)
2.  **arroz_salteado_cerdo.yml** - Arroz salteado con cerdo (fixed: rice)
3.  **atun_guisito_dinner.yml** - Atún con guisito (dinner, no carbs)
4.  **atun_juanfe.yml** - Atún a la JuanFe (lunch/dinner variants)
5.  **carne_encebollada_lunch.yml** - Carne encebollada (lunch)
6.  **chicken_fricassee.yml** - Chicken Fricassée (lunch/dinner variants)
7.  **lentejas.yml** - Lentejas (fixed: lentils)
8.  **pasta_tefy.yml** - Pasta Tefy (fixed: pasta)
9.  **pescado_encocado.yml** - Pescado encocado (lunch, optional carbs)
10. **pollo_asiatico.yml** - Pollo asiático (lunch/dinner variants)
11. **pollo_guiso_habichuelas_dinner.yml** - Pollo con guiso (no carbs)
12. **pollo_toscano_lunch.yml** - Pollo toscano (optional carbs)
13. **salmon_horneado.yml** - Salmón horneado (lunch/dinner variants)
14. **sudado_carne.yml** - Sudado de carne (lunch, fixed: potato)

## Key Design Decisions

1. **All measurements are RAW**: Simplifies shopping and preparation
2. **Flexible Meal Variants**: Recipes can handle different roles (lunch/dinner) via tags and carb strategies.
3. **Protein portions resolved at planning time**: Using `@portion` sentinel
4. **Carb portions from global rules**: Consistent across all recipes
5. **Canonical ingredient IDs**: Lowercase with underscores for consistency

## Validation

Before running the planner, validate the data structure:

```bash
# From the project root
export PYTHONPATH=$PYTHONPATH:$(pwd)/planner
python3 -m mealplanner.entrypoints.cli validate-data data
python3 -m mealplanner.entrypoints.cli validate-recipe data/recipes/pollo_asiatico.yml
```

This will check:
- Unique recipe IDs
- Valid ingredient references
- Correct carb strategies
- Protein portion rules
- Constraint consistency
- Filename matches recipe `id`
- Exactly one quantity field per ingredient line
- Ingredient role uses a supported value

### Ingredient-specific protein portions

`rules.yml` supports `protein_portion_overrides_g`, keyed by ingredient ID, with
optional `lunch` and `dinner` values. A missing meal override uses the corresponding
`protein_portions_g` default. These raw quantities apply to generated meals,
manual swaps, and shopping lists. Salmon and tilapia lunches use 200 g; fresh tuna
uses 170 g. Canned tuna retains the fish default, and dinner portions stay at 100 g.
