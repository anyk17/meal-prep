"""Filesystem adapter: writes plan.json and shopping_list.json in the legacy layout."""

from pathlib import Path

from ..application.plan_output import PlanOutput
from ..application.shopping import ShoppingList


def write_plan_output(plan: PlanOutput, output_dir: Path) -> None:
    """Write a previously built plan DTO to the legacy JSON location."""
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(output_dir / "plan.json", "w") as f:
        f.write(plan.model_dump_json(indent=2))


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
