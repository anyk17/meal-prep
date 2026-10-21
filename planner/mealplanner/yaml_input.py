"""YAML-backed adapter for the planner input reader interface."""

from pathlib import Path

from .load import load_data
from .planner_inputs import PlannerInputReader, PlannerInputs


class YamlPlannerInputReader:
    """Load the existing YAML data directory and expose its validated values."""

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)

    def read(self) -> PlannerInputs:
        loader = load_data(self.data_dir)
        assert loader.rules is not None
        return PlannerInputs(
            recipes=loader.recipes,
            ingredients=loader.ingredients,
            rules=loader.rules,
            pantry=tuple(loader.pantry),
        )


def read_planner_inputs(reader: PlannerInputReader) -> PlannerInputs:
    """Read a validated input set from any configured source adapter."""
    return reader.read()
