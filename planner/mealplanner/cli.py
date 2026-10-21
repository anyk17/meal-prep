"""Command-line interface for meal planner."""

import sys
from pathlib import Path

from .generation import generate as generate_outputs
from .load import load_data, validate_recipe_file
from .render import render_plan_output, write_shopping_output
from .yaml_input import YamlPlannerInputReader


def validate_data(data_dir: Path) -> int:
    """Validate all data files."""
    try:
        print(f"Loading data from {data_dir}...")
        loader = load_data(data_dir)

        print(f"✓ Loaded {len(loader.ingredients)} ingredients")
        print(f"✓ Loaded {len(loader.recipes)} recipes")
        print(f"✓ Loaded rules with {len(loader.rules.week.days)} days, {len(loader.rules.week.meals)} meals")
        print(f"✓ Loaded {len(loader.pantry)} pantry items")
        print("\n✓ All validation passed!")
        return 0

    except Exception as e:
        print(f"\n✗ Validation failed: {e}", file=sys.stderr)
        return 1


def generate_plan(data_dir: Path, output_dir: Path, seed: int) -> int:
    """Generate plan and shopping list."""
    try:
        # 1. Read and validate data through the YAML source adapter
        print(f"Loading data from {data_dir}...")
        inputs = YamlPlannerInputReader(data_dir).read()
        
        # 2. Solve and construct DTOs in memory
        print(f"Generating plan with seed {seed}...")
        generated = generate_outputs(inputs, seed)
        print("✓ Solution found!")
        
        print("Generating shopping list...")
        print(f"Rendering output to {output_dir}...")
        render_plan_output(generated.plan, output_dir)
        write_shopping_output(generated.shopping_list, output_dir)
        print(f"✓ Wrote shopping_list.json to {output_dir}")
        
        return 0
        
    except Exception as e:
        print(f"\n✗ Plan generation failed: {e}", file=sys.stderr)
        return 1


def validate_recipe(recipe_file: Path, data_dir: Path | None = None) -> int:
    """Validate a single recipe file."""
    try:
        print(f"Validating recipe {recipe_file}...")
        loader, recipe = validate_recipe_file(recipe_file, data_dir)

        print(f"✓ Loaded shared ingredients from {loader.data_dir}")
        print(f"✓ Recipe id: {recipe.id}")
        print(f"✓ Recipe name: {recipe.name}")
        print(f"✓ Meals: {', '.join(meal.value for meal in recipe.meal_types)}")
        print(f"✓ Ingredients: {len(recipe.ingredients)}")
        print("\n✓ Recipe validation passed!")
        return 0

    except Exception as e:
        print(f"\n✗ Recipe validation failed: {e}", file=sys.stderr)
        return 1


def main() -> None:
    """Main CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage: mealplanner <command> [args]")
        print("\nCommands:")
        print("  validate-data <data_dir>")
        print("  validate-recipe <recipe_file> [--data-dir DATA_DIR]")
        print("  generate-plan <data_dir> <output_dir> [--seed SEED]")
        sys.exit(1)

    command = sys.argv[1]

    if command == "validate-data":
        if len(sys.argv) < 3:
            print("Usage: mealplanner validate-data <data_dir>")
            sys.exit(1)
        data_dir = Path(sys.argv[2])
        sys.exit(validate_data(data_dir))

    elif command == "validate-recipe":
        if len(sys.argv) < 3:
            print("Usage: mealplanner validate-recipe <recipe_file> [--data-dir DATA_DIR]")
            sys.exit(1)

        recipe_file = Path(sys.argv[2])
        data_dir = None

        if "--data-dir" in sys.argv:
            idx = sys.argv.index("--data-dir")
            if idx + 1 < len(sys.argv):
                data_dir = Path(sys.argv[idx + 1])

        sys.exit(validate_recipe(recipe_file, data_dir))

    elif command == "generate-plan":
        if len(sys.argv) < 4:
            print("Usage: mealplanner generate-plan <data_dir> <output_dir> [--seed SEED]")
            sys.exit(1)
            
        data_dir = Path(sys.argv[2])
        output_dir = Path(sys.argv[3])
        
        seed = 42
        if "--seed" in sys.argv:
            idx = sys.argv.index("--seed")
            if idx + 1 < len(sys.argv):
                seed = int(sys.argv[idx + 1])
                
        sys.exit(generate_plan(data_dir, output_dir, seed))

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
