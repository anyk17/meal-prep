"""Loopback-only Flask app factory and dev server entry point.

Wires adapters (JSON snapshot or SQLite) to the application services and
registers the read and write route modules.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ...adapters.json_snapshot import JsonPlanReadRepository, SourceUnavailableError
from ...application.plan_reads import PlanReadService
from ...application.ports import PlanGenerator, PlanReadRepository
from ...domain.snapshot import SourceInvalidError
from .read_routes import register_read_routes
from .write_routes import GenerationGate, register_write_routes

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_PLAN_PATH = PROJECT_ROOT / "site/public/plan.json"
DEFAULT_SHOPPING_PATH = PROJECT_ROOT / "site/public/shopping_list.json"
DEFAULT_PANTRY_PATH = PROJECT_ROOT / "data/pantry.yml"
DEFAULT_WEEK_START = date(2026, 10, 19)
DEFAULT_SOLVER_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_CONCURRENT_GENERATIONS = 1
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _error(status: int, code: str, message: str, details: list[dict[str, str]] | None = None):
    from flask import jsonify

    response = jsonify({"error": {"code": code, "message": message, "details": details or []}})
    response.status_code = status
    return response


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


def _env_number(name: str, default: float, cast: Callable[[str], Any]) -> Any:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = cast(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _is_loopback_request() -> bool:
    from flask import request

    try:
        return ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        return False


def _build_workflow_service(
    db_path: Path | str,
    timezone: str | None,
    clock: Callable[[], datetime] | None,
    generator: PlanGenerator | None,
    solver_timeout_seconds: float | None,
    proposal_ttl: timedelta | None,
) -> Any:
    from ...adapters.sqlite.store import SqlitePlannerInputReader, SqlitePlanRepository
    from ...adapters.ortools import OrToolsPlanGenerator
    from ...application.plan_workflows import DEFAULT_PROPOSAL_TTL, PlanWorkflowService

    zone = timezone or os.environ.get("MEALPLANNER_TIMEZONE", "America/Bogota")
    tz = ZoneInfo(zone)  # ZoneInfoNotFoundError is a KeyError; surfaced as source failure
    timeout = solver_timeout_seconds or _env_number(
        "MEALPLANNER_SOLVER_TIMEOUT_SECONDS", DEFAULT_SOLVER_TIMEOUT_SECONDS, float
    )
    plan_generator = generator or OrToolsPlanGenerator(max_time_seconds=timeout)
    return PlanWorkflowService(
        SqlitePlanRepository(db_path),
        SqlitePlannerInputReader(db_path),
        clock or (lambda: datetime.now(tz)),
        generator=plan_generator,
        timezone=zone,
        proposal_ttl=proposal_ttl or DEFAULT_PROPOSAL_TTL,
    )


def create_app(
    repository: PlanReadRepository | None = None,
    *,
    plan_path: Path | None = None,
    shopping_path: Path | None = None,
    pantry_path: Path | None = None,
    week_start: str | date | None = None,
    timezone: str | None = None,
    db_path: Path | str | None = None,
    clock: Callable[[], datetime] | None = None,
    generator: PlanGenerator | None = None,
    solver_timeout_seconds: float | None = None,
    max_concurrent_generations: int | None = None,
    proposal_ttl: timedelta | None = None,
) -> Any:
    """Create the API. Source files are loaded and validated once.

    With db_path (or MEALPLANNER_DB) reads come from the SQLite store and the
    plan write routes are enabled (loopback clients only). Otherwise the API is
    read-only. clock must return a timezone-aware datetime (tests inject one);
    the default reads the household timezone (MEALPLANNER_TIMEZONE).
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
    app.config["PLAN_WORKFLOW_SERVICE"] = None
    app.config["PLAN_WRITE_FAILURE"] = False
    sqlite_path = db_path or os.environ.get("MEALPLANNER_DB")
    if repository is not None:
        app.config["PLAN_READ_SERVICE"] = PlanReadService(repository)
    elif sqlite_path:
        # Opt-in: serve current revisions from SQLite instead of the JSON snapshot.
        try:
            from ...adapters.sqlite.migrations import DatabaseError
            from ...adapters.sqlite.store import SqlitePlanReadAdapter

            app.config["PLAN_READ_SERVICE"] = PlanReadService(SqlitePlanReadAdapter(sqlite_path))
            app.config["PLAN_WORKFLOW_SERVICE"] = _build_workflow_service(
                sqlite_path, timezone, clock, generator, solver_timeout_seconds, proposal_ttl
            )
        except DatabaseError:
            app.config["PLAN_SOURCE_FAILURE"] = "source_unavailable"
        except (sqlite3.Error, ValueError, KeyError):  # unreadable or corrupt file, bad timezone
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
        if not _is_loopback_request():
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                return _error(403, "forbidden", "Plan changes are allowed from this machine only.")
            return _error(
                403, "invalid_request", "This development API accepts loopback connections only."
            )
        return None

    limit = max_concurrent_generations or _env_number(
        "MEALPLANNER_MAX_CONCURRENT_GENERATIONS", DEFAULT_MAX_CONCURRENT_GENERATIONS, int
    )
    gate = GenerationGate(limit)
    app.config["GENERATION_GATE"] = gate
    register_write_routes(app, _error, _is_loopback_request, gate)

    register_read_routes(app, _error)

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
