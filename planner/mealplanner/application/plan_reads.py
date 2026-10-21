"""Storage-independent read use cases for current weekly plan snapshots."""

from __future__ import annotations

from copy import deepcopy
from datetime import date

from ..domain.snapshot import JsonObject
from .errors import InvalidRequest, PlanNotFound, SnapshotChanged
from .ports import PlanReadRepository


class PlanReadService:
    """Validates read arguments and composes current plan views without Flask."""

    def __init__(self, repository: PlanReadRepository) -> None:
        self._repository = repository

    def list_plans(self, from_date: date, to_date: date) -> JsonObject:
        """List plans whose full calendar week overlaps the inclusive range."""
        if from_date > to_date:
            raise InvalidRequest("to must be on or after from")
        if (to_date - from_date).days + 1 > 366:
            raise InvalidRequest("date range must contain at most 366 inclusive days")
        items = []
        for summary in self._repository.list_plan_summaries():
            week_start = date.fromisoformat(summary["week_start"])
            week_end = date.fromisoformat(summary["week_end"])
            if week_start <= to_date and week_end >= from_date:
                items.append(deepcopy(summary))
        items.sort(key=lambda item: (item["week_start"], item["plan_id"]))
        return {"items": items}

    def get_plan(self, plan_id: str) -> JsonObject:
        """Return the current plan detail."""
        plan = self._repository.get_plan(plan_id)
        if plan is None:
            raise PlanNotFound(plan_id)
        return deepcopy(plan)

    def get_shopping_list(self, plan_id: str, revision: int, snapshot_id: str) -> JsonObject:
        """Return groceries only when both expected current tokens match."""
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise InvalidRequest("revision must be a positive integer")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise InvalidRequest("revision must be positive and snapshot_id must be non-empty")
        shopping = self._repository.get_shopping_list(plan_id)
        if shopping is None:
            raise PlanNotFound(plan_id)
        if shopping["revision"] != revision or shopping["snapshot_id"] != snapshot_id:
            raise SnapshotChanged("requested shopping snapshot is no longer current")
        return deepcopy(shopping)
