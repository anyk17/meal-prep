"""Read routes: plan list, plan detail, and shopping list. Parsing, error mapping, serialization only."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any

from ...adapters.sqlite.migrations import DatabaseError
from ...application.errors import InvalidRequest, PlanNotFound, SnapshotChanged
from ...application.plan_reads import PlanReadService

PLAN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INTEGER_PATTERN = re.compile(r"^[0-9]+$")


def _read_date_argument(name: str) -> date:
    from flask import request

    values = request.args.getlist(name)
    if len(values) != 1 or not DATE_PATTERN.fullmatch(values[0]):
        raise InvalidRequest(f"{name} must be an ISO calendar date in YYYY-MM-DD format")
    try:
        return date.fromisoformat(values[0])
    except ValueError as exc:
        raise InvalidRequest(f"{name} must be a real calendar date") from exc


def _read_single_query(name: str) -> str:
    from flask import request

    values = request.args.getlist(name)
    if len(values) != 1:
        raise InvalidRequest(f"{name} is required exactly once")
    return values[0]


def register_read_routes(
    app: Any,
    _error: Callable[..., Any],
) -> None:
    """Register the read-only plan routes on app (state is read from app.config)."""
    from flask import jsonify

    def service() -> PlanReadService | None:
        failure = app.config["PLAN_SOURCE_FAILURE"]
        if failure:
            return None
        current = app.config["PLAN_READ_SERVICE"]
        if current is None:
            return None
        return current

    @app.get("/api/v1/plans")
    def list_plans():
        current = service()
        if current is None:
            code = app.config["PLAN_SOURCE_FAILURE"] or "source_unavailable"
            message = (
                "The plan data source is temporarily unavailable."
                if code == "source_unavailable"
                else "The configured plan and shopping data could not be validated."
            )
            return _error(503, code, message)
        try:
            from_date = _read_date_argument("from")
            to_date = _read_date_argument("to")
            result = current.list_plans(from_date, to_date)
        except InvalidRequest as exc:
            message = str(exc)
            field = (
                "to"
                if message.startswith("to must be on or after from")
                or "at most 366 inclusive days" in message
                else "from"
                if message.startswith("from ")
                else "to"
            )
            return _error(
                400,
                "invalid_request",
                "The date range is invalid.",
                [{"field": field, "message": str(exc)}],
            )
        return jsonify(result)

    @app.get("/api/v1/plans/<plan_id>")
    def get_plan(plan_id: str):
        current = service()
        if current is None:
            code = app.config["PLAN_SOURCE_FAILURE"] or "source_unavailable"
            message = (
                "The plan data source is temporarily unavailable."
                if code == "source_unavailable"
                else "The configured plan and shopping data could not be validated."
            )
            return _error(503, code, message)
        if not PLAN_ID_PATTERN.fullmatch(plan_id):
            return _error(
                400,
                "invalid_request",
                "The plan identifier is invalid.",
                [
                    {
                        "field": "plan_id",
                        "message": "Must contain 1 to 128 ASCII letters, digits, underscores, or hyphens and begin with a letter or digit.",
                    }
                ],
            )
        try:
            result = current.get_plan(plan_id)
        except PlanNotFound:
            return _error(404, "plan_not_found", "No plan exists for the requested plan_id.")
        return jsonify(result)

    @app.get("/api/v1/plans/<plan_id>/shopping-list")
    def get_shopping_list(plan_id: str):
        current = service()
        if current is None:
            code = app.config["PLAN_SOURCE_FAILURE"] or "source_unavailable"
            message = (
                "The plan data source is temporarily unavailable."
                if code == "source_unavailable"
                else "The configured plan and shopping data could not be validated."
            )
            return _error(503, code, message)
        if not PLAN_ID_PATTERN.fullmatch(plan_id):
            return _error(
                400,
                "invalid_request",
                "The plan identifier is invalid.",
                [
                    {
                        "field": "plan_id",
                        "message": "Must contain 1 to 128 ASCII letters, digits, underscores, or hyphens and begin with a letter or digit.",
                    }
                ],
            )
        try:
            revision_text = _read_single_query("revision")
            if not INTEGER_PATTERN.fullmatch(revision_text):
                raise InvalidRequest("revision must be an integer greater than or equal to 1")
            try:
                revision = int(revision_text)
            except ValueError:
                # Python may reject extremely long decimal strings; any such value
                # differs from the small current revision integers used here.
                if current.get_plan(plan_id) is None:
                    raise PlanNotFound(plan_id)
                raise SnapshotChanged("requested shopping revision is not current")
            if revision < 1:
                raise InvalidRequest("revision must be an integer greater than or equal to 1")
            snapshot_id = _read_single_query("snapshot_id")
            if not snapshot_id:
                raise InvalidRequest("snapshot_id must be non-empty")
            result = current.get_shopping_list(plan_id, revision, snapshot_id)
        except InvalidRequest as exc:
            field = "revision" if "revision" in str(exc) else "snapshot_id"
            return _error(
                400,
                "invalid_request",
                "The request parameters are invalid.",
                [{"field": field, "message": str(exc)}],
            )
        except PlanNotFound:
            return _error(404, "plan_not_found", "No plan exists for the requested plan_id.")
        except SnapshotChanged:
            return _error(
                409,
                "snapshot_changed",
                "The requested shopping snapshot is no longer current. Refetch the plan and its shopping list.",
            )
        return jsonify(result)

    @app.get("/api/v1/catalog/variants")
    def list_swap_variants():
        """Swappable recipe variants from the current planner inputs (SQLite-backed API only)."""
        failure = app.config["PLAN_SOURCE_FAILURE"]
        workflow = app.config.get("PLAN_WORKFLOW_SERVICE")
        if failure or workflow is None:
            # JSON snapshot mode has no planner inputs to list: same 503 as an unavailable source.
            return _error(
                503,
                failure or "source_unavailable",
                "The recipe catalog needs the SQLite-backed API (MEALPLANNER_DB)."
                if not failure
                else "The plan data source is temporarily unavailable.",
            )
        try:
            items = workflow.list_swap_variants()
        except (DatabaseError, sqlite3.Error, OSError):
            return _error(503, "source_unavailable", "The plan data source is temporarily unavailable.")
        return jsonify(
            {
                "items": [
                    {
                        "recipe_id": v.recipe_id,
                        "variant_id": v.variant_id,
                        "name": v.name,
                        "protein": v.protein,
                        "carb": v.carb,
                    }
                    for v in items
                ]
            }
        )
