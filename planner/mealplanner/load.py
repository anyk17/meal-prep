"""Data loading and validation module."""

from pathlib import Path

import yaml

from .config import Ingredient, Recipe, Rules
from .validation import planner_input_errors, validate_recipe, validate_weekly_protein_counts


class ValidationErrorGroup(ValueError):
    """Collection of validation errors reported together."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        count = len(self.errors)
        joined = "\n".join(f"- {error}" for error in self.errors)
        return f"Found {count} validation error(s):\n{joined}"


class DataLoader:
    """Loads and validates YAML configuration files."""

    def __init__(self, data_dir: Path):
        """Initialize with data directory path."""
        self.data_dir = data_dir
        self.ingredients: dict[str, Ingredient] = {}
        self.recipes: dict[str, Recipe] = {}
        self.rules: Rules | None = None
        self.pantry: list[str] = []

    def load_all(self) -> None:
        """Load and validate all configuration files."""
        self.load_ingredients()
        self.load_rules()
        self.load_pantry()
        self.load_recipes()
        self.cross_validate()

    def load_ingredients(self) -> None:
        """Load ingredients.yml."""
        path = self.data_dir / "ingredients.yml"
        with open(path) as f:
            data = yaml.safe_load(f)

        # Validate each ingredient
        self.ingredients = {}
        for ing_id, ing_data in data.items():
            self.ingredients[ing_id] = Ingredient(**ing_data)

    def load_rules(self) -> None:
        """Load rules.yml."""
        path = self.data_dir / "rules.yml"
        with open(path) as f:
            data = yaml.safe_load(f)

        self.rules = Rules(**data)

        validate_weekly_protein_counts(self.rules)

    def load_pantry(self) -> None:
        """Load pantry.yml."""
        path = self.data_dir / "pantry.yml"
        with open(path) as f:
            data = yaml.safe_load(f)

        self.pantry = data if isinstance(data, list) else []

    def load_recipes(self) -> None:
        """Load all recipe files from recipes/ directory."""
        recipes_dir = self.data_dir / "recipes"
        if not recipes_dir.exists():
            raise FileNotFoundError(f"Recipes directory not found: {recipes_dir}")

        self.recipes = {}
        errors: list[str] = []
        for recipe_file in sorted(recipes_dir.glob("*.yml")):
            try:
                recipe = self.load_recipe_file(recipe_file)
            except Exception as exc:
                errors.append(f"{recipe_file.name}: {exc}")
                continue

            # Check for duplicate IDs
            if recipe.id in self.recipes:
                errors.append(f"{recipe_file.name}: Duplicate recipe ID: {recipe.id}")
                continue

            self.recipes[recipe.id] = recipe

        if errors:
            raise ValidationErrorGroup(errors)

    def load_recipe_file(self, recipe_file: Path) -> Recipe:
        """Load and schema-validate a single recipe file."""
        with open(recipe_file) as f:
            data = yaml.safe_load(f)

        recipe = Recipe(**data)
        self.validate_recipe_filename(recipe_file, recipe)
        return recipe

    def validate_recipe_filename(self, recipe_file: Path, recipe: Recipe) -> None:
        """Validate filename conventions for contributor-submitted recipes."""
        if recipe_file.suffix != ".yml":
            raise ValueError(f"Recipe {recipe.id}: file must use .yml extension")

        if recipe_file.stem != recipe.id:
            raise ValueError(
                f"Recipe {recipe.id}: filename '{recipe_file.name}' must match id '{recipe.id}.yml'"
            )

    def cross_validate(self) -> None:
        """Cross-validate references between files."""
        if not self.rules:
            raise ValueError("Rules must be loaded before cross-validation")

        errors = planner_input_errors(
            self.recipes,
            self.ingredients,
            self.rules,
            self.pantry,
        )

        if errors:
            raise ValidationErrorGroup(errors)

    def validate_recipe(self, recipe_id: str, recipe: Recipe) -> None:
        """Validate a single recipe against loaded ingredients and rules."""
        if not self.rules:
            raise ValueError("Rules must be loaded before recipe validation")
        validate_recipe(recipe_id, recipe, self.ingredients, self.rules)


def infer_data_dir(recipe_file: Path) -> Path:
    """Infer the data directory for a recipe file from its location."""
    candidate_dirs = [recipe_file.parent.parent, recipe_file.parent]
    for candidate in candidate_dirs:
        if (candidate / "ingredients.yml").exists() and (candidate / "rules.yml").exists():
            return candidate
    raise FileNotFoundError(
        f"Could not infer data directory for recipe file: {recipe_file}"
    )


def validate_recipe_file(
    recipe_file: Path | str, data_dir: Path | str | None = None
) -> tuple[DataLoader, Recipe]:
    """Load and validate a single recipe file against the shared data definitions."""
    recipe_path = Path(recipe_file)
    resolved_data_dir = infer_data_dir(recipe_path) if data_dir is None else Path(data_dir)

    loader = DataLoader(resolved_data_dir)
    loader.load_ingredients()
    loader.load_rules()
    loader.load_pantry()

    recipe = loader.load_recipe_file(recipe_path)
    loader.validate_recipe(recipe.id, recipe)
    return loader, recipe


def load_data(data_dir: Path | str) -> DataLoader:
    """Load and validate all data files.

    Args:
        data_dir: Path to data directory

    Returns:
        DataLoader instance with loaded data

    Raises:
        ValueError: If validation fails
        FileNotFoundError: If required files are missing
    """
    if isinstance(data_dir, str):
        data_dir = Path(data_dir)

    loader = DataLoader(data_dir)
    loader.load_all()
    return loader
