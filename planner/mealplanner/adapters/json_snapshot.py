"""Load and validate the legacy JSON plan as one immutable read snapshot."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from ..application.ports import PlanReadRepository
from ..domain.snapshot import JsonObject, PlanSnapshot, SourceInvalidError, normalize_snapshot

class SourceUnavailableError(Exception):
    """A configured JSON or pantry input file could not be read."""


class JsonPlanReadRepository(PlanReadRepository):
    """In-memory immutable repository initialized from a coherent JSON fixture."""

    def __init__(self, snapshots: list[PlanSnapshot]) -> None:
        self._snapshots = {
            snapshot.plan["plan_id"]: PlanSnapshot(
                plan=deepcopy(snapshot.plan), shopping_list=deepcopy(snapshot.shopping_list)
            )
            for snapshot in snapshots
        }

    @classmethod
    def from_files(
        cls,
        plan_path: Path,
        shopping_path: Path,
        pantry_path: Path,
        week_start: date,
        timezone: str = "America/Bogota",
    ) -> JsonPlanReadRepository:
        """Read all source files once and publish only a validated snapshot."""
        paths = (plan_path, shopping_path, pantry_path)
        try:
            before = tuple(_file_signature(path) for path in paths)
            plan_text, shopping_text, pantry_text = (
                path.read_text(encoding="utf-8") for path in paths
            )
            after = tuple(_file_signature(path) for path in paths)
        except OSError as exc:
            raise SourceUnavailableError("configured plan source is unavailable") from exc
        except UnicodeDecodeError as exc:
            raise SourceInvalidError("configured plan source is invalid") from exc
        if before != after:
            raise SourceUnavailableError("configured plan source changed during startup capture")
        try:
            plan_data = json.loads(plan_text)
            shopping_data = json.loads(shopping_text)
            pantry_data = yaml.safe_load(pantry_text)
        except (json.JSONDecodeError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise SourceInvalidError("configured plan source is invalid") from exc
        if not isinstance(pantry_data, list) or any(
            not isinstance(item, str) for item in pantry_data
        ):
            raise SourceInvalidError("pantry source must be a list of ingredient IDs")
        try:
            snapshot = normalize_snapshot(
                plan_data, shopping_data, set(pantry_data), week_start, timezone
            )
        except SourceInvalidError:
            raise
        except (TypeError, KeyError, IndexError, AttributeError) as exc:
            raise SourceInvalidError("configured plan source is invalid") from exc
        return cls([snapshot])

    def list_plan_summaries(self) -> list[JsonObject]:
        return [snapshot.summary for snapshot in self._snapshots.values()]

    def get_plan(self, plan_id: str) -> JsonObject | None:
        snapshot = self._snapshots.get(plan_id)
        return deepcopy(snapshot.plan) if snapshot else None

    def get_shopping_list(self, plan_id: str) -> JsonObject | None:
        snapshot = self._snapshots.get(plan_id)
        return deepcopy(snapshot.shopping_list) if snapshot else None


def _file_signature(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns
