# Meal Prep Planner

An automated meal planning system that generates optimal weekly menus based on nutritional constraints, ingredient availability, and variety rules. It includes a modern web interface for viewing your plan and shopping list.

## Features

-   **Constraint-Based Planning**: Uses Google OR-Tools to ensure nutritional balance (protein targets, carb limits) and variety (no consecutive proteins, recipe repetition limits).
-   **Data-First Configuration**: Define everything in YAML (`data/recipes/*.yml`, `data/ingredients.yml`, `data/rules.yml`).
-   **Automated Shopping List**: Aggregates ingredients from the generated plan, grouping them by category (Protein, Produce, Pantry, etc.).
-   **Modern UI**: A responsive React + Tailwind CSS web application to view your weekly schedule and check off grocery items.
-   **Dark Mode**: Sleek Slate-900 based dark theme with persistent user preference.
-   **Flexible**: Supports fixed vs. optional carbs and custom dietary rules.

## Prerequisites

-   **Python 3.10+** (for the planner)
-   **Linux Node.js 22.12+** (for the UI; `.nvmrc` selects Node 22)

## Installation

### 1. Setup the Planner (Python)
```bash
# Create a virtual environment
python3 -m venv .venv

# Activate the virtual environment
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -e 'planner[api,dev]'
```

### 2. Setup the UI (React)
```bash
cd site
npm ci
cd ..
```

## Workflow: How to Generate a Weekly Plan

### Step 1: Generate the Plan
Run the planner command to solve for a new week. This will write the data directly to the UI's public folder.

```bash
# Generate a plan using a seed for reproducibility
.venv/bin/mealplanner generate-plan data site/public --seed 123
```

To validate recipe changes before generating a plan:

```bash
.venv/bin/mealplanner validate-data data
.venv/bin/mealplanner validate-recipe data/recipes/pollo_asiatico.yml
```

This command will:
1.  Read configuration from `data/`.
2.  Solve for a valid schedule satisfying all rules in `data/rules.yml`.
3.  Output `plan.json` and `shopping_list.json` directly to `site/public/`.

### Step 2: View the Plan
Start Flask from the repository root in one terminal:

```bash
.venv/bin/python -m mealplanner.api
```

In another terminal, start the frontend:

```bash
cd site
npm run dev:api
```
Open `http://127.0.0.1:5173/meal-prep/`. The current fixture is explicitly anchored to October 19–25, 2026. Restart Flask after generating new files.

For the read-only static sample without Flask, use `npm run dev:static` instead. It displays the checked-in publication example, not the latest CLI output. Detailed setup, date configuration, validation, and migration phases are in the [migration plan](docs/migration-to-cloud/README.md#local-development-environment-wsllinux).

This branch requires explicit frontend modes. Existing Generate Plan/Build Frontend GitHub workflows still use the old generic build command and cannot produce a deployable artifact from this branch yet; the real publication workflow is phase 6.

## Configuration

-   **`data/rules.yml`**: Define global constraints (e.g., "no carbs at dinner", protein targets per week).
-   **`data/ingredients.yml`**: Master list of ingredients with units and categories.
-   **`data/pantry.yml`**: Items you already have (excluded from shopping list).
-   **`data/recipes/*.yml`**: Individual recipe definitions (ingredients, tags, meal types).
-   **`.github/workflows/validate_recipes.yml`**: Manual workflow to validate repo data or a specific PR on demand.

## Troubleshooting

-   **Optimization Failed**: If the solver cannot find a solution, try relaxing constraints in `data/rules.yml` (e.g., increase `max_recipe_uses_per_week` or `fish_dinner_max_per_week`).
-   **UI Not Updating**: In API mode, generate into `site/public`, restart Flask, then refresh the frontend. Static mode uses a separate prepared sample bundle.
