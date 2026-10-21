"""Loopback-only Flask API for the current plan snapshot."""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import date
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from .json_snapshot import (
    JsonPlanReadRepository,
    SourceInvalidError,
    SourceUnavailableError,
)
from .read_api import (
    InvalidRequest,
    PlanNotFound,
    PlanReadRepository,
    PlanReadService,
    SnapshotChanged,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLAN_PATH = PROJECT_ROOT / "site/public/plan.json"
DEFAULT_SHOPPING_PATH = PROJECT_ROOT / "site/public/shopping_list.json"
DEFAULT_PANTRY_PATH = PROJECT_ROOT / "data/pantry.yml"
DEFAULT_WEEK_START = date(2026, 10, 19)
PLAN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INTEGER_PATTERN = re.compile(r"^[0-9]+$")


def _error(status: int, code: str, message: str, details: list[dict[str, str]] | None = None):
    from flask import jsonify

    response = jsonify({"error": {"code": code, "message": message, "details": details or []}})
    response.status_code = status
    return response


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


def _configured_week_start(value: str | date | None) -> date:
    if value is None:
        value = os.environ.get("MEALPLANNER_WEEK_START", DEFAULT_WEEK_START.isoformat())
    if isinstance(value, date):
        result = value
    elif isinstance(value, str) and DATE_PATTERN.fullmatch(value):
        try:
            result = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("MEALPLANNER_WEEK_START must be a real ISO date") from exc
    else:
        raise ValueError("MEALPLANNER_WEEK_START must use YYYY-MM-DD")
    if result.weekday() != 0:
        raise ValueError("MEALPLANNER_WEEK_START must be a Monday")
    return result


def create_app(
    repository: PlanReadRepository | None = None,
    *,
    plan_path: Path | None = None,
    shopping_path: Path | None = None,
    pantry_path: Path | None = None,
    week_start: str | date | None = None,
    timezone: str | None = None,
    db_path: Path | str | None = None,
) -> Any:
    """Create the read-only API. Source files are loaded and validated once.

    With db_path (or MEALPLANNER_DB) reads come from the SQLite store instead.
    """
    try:
        from flask import Flask, jsonify, request
    except ModuleNotFoundError as exc:
        if exc.name == "flask":
            raise RuntimeError(
                "Flask is required for the HTTP API; install mealplanner[api]"
            ) from exc
        raise
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    app.config["PLAN_READ_SERVICE"] = None
    app.config["PLAN_SOURCE_FAILURE"] = None
    sqlite_path = db_path or os.environ.get("MEALPLANNER_DB")
    if repository is not None:
        app.config["PLAN_READ_SERVICE"] = PlanReadService(repository)
    elif sqlite_path:
        # Opt-in: serve current revisions from SQLite instead of the JSON snapshot.
        try:
            from .sqlite_store import DatabaseError, SqlitePlanReadAdapter

            app.config["PLAN_READ_SERVICE"] = PlanReadService(SqlitePlanReadAdapter(sqlite_path))
        except DatabaseError:
            app.config["PLAN_SOURCE_FAILURE"] = "source_unavailable"
        except (sqlite3.Error, ValueError):  # unreadable or corrupt file
            app.config["PLAN_SOURCE_FAILURE"] = "source_invalid"
    else:
        try:
            configured_start = _configured_week_start(week_start)
            zone = timezone or os.environ.get("MEALPLANNER_TIMEZONE", "America/Bogota")
            source_repository = JsonPlanReadRepository.from_files(
                Path(plan_path or DEFAULT_PLAN_PATH),
                Path(shopping_path or DEFAULT_SHOPPING_PATH),
                Path(pantry_path or DEFAULT_PANTRY_PATH),
                configured_start,
                zone,
            )
            app.config["PLAN_READ_SERVICE"] = PlanReadService(source_repository)
        except SourceUnavailableError:
            app.config["PLAN_SOURCE_FAILURE"] = "source_unavailable"
        except (SourceInvalidError, ValueError):
            app.config["PLAN_SOURCE_FAILURE"] = "source_invalid"

    @app.after_request
    def no_store(response):
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.before_request
    def reject_non_loopback_api_clients():
        if not request.path.startswith("/api/"):
            return None
        try:
            remote_ip = ip_address(request.remote_addr or "")
        except ValueError:
            remote_ip = None
        if remote_ip is None or not remote_ip.is_loopback:
            return _error(
                403, "invalid_request", "This development API accepts loopback connections only."
            )
        return None

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

    return app


def main() -> None:
    """Run the local development server, refusing non-loopback bindings."""
    host = os.environ.get("MEALPLANNER_HOST", "127.0.0.1")
    if host != "127.0.0.1":
        raise SystemExit("The unauthenticated phase 2 API may bind only to 127.0.0.1")
    port_text = os.environ.get("MEALPLANNER_PORT", "5000")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise SystemExit("MEALPLANNER_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise SystemExit("MEALPLANNER_PORT must be between 1 and 65535")
    create_app().run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
