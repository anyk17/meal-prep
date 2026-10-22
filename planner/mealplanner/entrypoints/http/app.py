"""Flask app factory and dev server entry point.

Wires adapters (JSON snapshot or SQLite) to the application services and
registers the route modules. Viewing is open; changes and publishing need a household login.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ...adapters.json_snapshot import JsonPlanReadRepository, SourceUnavailableError
from ...application.plan_reads import PlanReadService
from ...application.ports import PlanGenerator, PlanReadRepository
from ...domain.snapshot import SourceInvalidError
from .auth_routes import register_auth
from .ops_routes import register_health_route, register_static_routes
from .publish_routes import register_publish_routes
from .read_routes import register_read_routes
from .trip_routes import register_trip_routes
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


def _build_workflow_service(
    db_path: Path | str,
    timezone: str | None,
    clock: Callable[[], datetime] | None,
    generator: PlanGenerator | None,
    solver_timeout_seconds: float | None,
    proposal_ttl: timedelta | None,
) -> Any:
    from ...adapters.ortools import OrToolsPlanGenerator
    from ...adapters.sqlite.store import (
        SqlitePlannerInputReader,
        SqlitePlanRepository,
        SqliteShoppingTripRepository,
    )
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
        trips=SqliteShoppingTripRepository(db_path),
    )


def _build_trip_service(
    db_path: Path | str, timezone: str | None, clock: Callable[[], datetime] | None
) -> Any:
    from ...adapters.sqlite.store import SqlitePlanRepository, SqliteShoppingTripRepository
    from ...application.shopping_trips import ShoppingTripService

    tz = ZoneInfo(timezone or os.environ.get("MEALPLANNER_TIMEZONE", "America/Bogota"))
    return ShoppingTripService(
        SqliteShoppingTripRepository(db_path),
        SqlitePlanRepository(db_path),
        clock or (lambda: datetime.now(tz)),
    )


def _env_number_or_zero(name: str) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _build_auth_service(db_path: Path | str, timezone: str | None, clock: Callable[[], datetime] | None) -> Any:
    from ...adapters.sqlite.auth import SqliteSessionRepository, SqliteUserRepository
    from ...application.auth import AuthService

    tz = ZoneInfo(timezone or os.environ.get("MEALPLANNER_TIMEZONE", "America/Bogota"))
    return AuthService(
        SqliteUserRepository(db_path), SqliteSessionRepository(db_path), clock or (lambda: datetime.now(tz))
    )


def _memory_auth_service(clock: Callable[[], datetime] | None) -> Any:
    from ...adapters.memory import InMemorySessionRepository, InMemoryUserRepository
    from ...application.auth import AuthService

    return AuthService(
        InMemoryUserRepository(), InMemorySessionRepository(), clock or (lambda: datetime.now(ZoneInfo("UTC")))
    )


DEFAULT_PUBLISH_KEY = "/run/secrets/deploy_key"
DEFAULT_PUBLISH_KNOWN_HOSTS = "/etc/ssh/ssh_known_hosts"
DEFAULT_PUBLIC_SITE_URL = "https://jflopezo91.github.io/meal-plan/"
DEFAULT_PUBLISH_GITHUB_REPO = "jflopezo91/meal-plan"
FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def resolve_app_revision() -> str | None:
    """The meal-prep commit this code was built from: MEALPLANNER_APP_REVISION, else the checkout's HEAD."""
    configured = (os.environ.get("MEALPLANNER_APP_REVISION") or "").strip().lower()
    if configured:
        return configured if FULL_SHA_PATTERN.fullmatch(configured) else None
    if not (PROJECT_ROOT / ".git").exists():
        return None
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out if FULL_SHA_PATTERN.fullmatch(out) else None


def _lazy_bundle_validator() -> Callable[[Path], Any]:
    """The JSON-schema bundle validator when the checkout has it (the image does not ship scripts/)."""
    state: dict[str, Any] = {}

    def validate(root: Path) -> Any:
        if "fn" not in state:
            from ..db_cli import full_bundle_validator

            try:
                state["fn"] = full_bundle_validator()
            except Exception:  # noqa: BLE001 - missing scripts/jsonschema: coherence checks still run
                state["fn"] = lambda _root: []
        return state["fn"](root)

    return validate


def _build_publishing_service(
    db_path: Path | str,
    timezone: str | None,
    clock: Callable[[], datetime] | None,
    trip_service: Any,
) -> Any:
    """PublishingService from MEALPLANNER_PUBLISH_* settings; None when MEALPLANNER_PUBLISH_REPO is unset."""
    remote = (os.environ.get("MEALPLANNER_PUBLISH_REPO") or "").strip()
    if not remote:
        return None
    from ...adapters.git_publication import GitPublicationRepository
    from ...adapters.github_deployments import GithubDeploymentStatusReader
    from ...adapters.sqlite.publication_jobs import SqlitePublishJobRepository
    from ...adapters.sqlite.store import SqlitePlanReadAdapter
    from ...application.publication import PublicationService
    from ...application.publishing import PublishingService

    zone = timezone or os.environ.get("MEALPLANNER_TIMEZONE", "America/Bogota")
    now = clock or (lambda: datetime.now(ZoneInfo(zone)))
    workdir = os.environ.get("MEALPLANNER_PUBLISH_WORKDIR") or str(Path(db_path).resolve().parent / "publish-repo")
    return PublishingService(
        PublicationService(SqlitePlanReadAdapter(db_path), trip_service, now, zone),
        GitPublicationRepository(
            remote,
            workdir,
            key_path=os.environ.get("MEALPLANNER_PUBLISH_KEY") or DEFAULT_PUBLISH_KEY,
            known_hosts_path=os.environ.get("MEALPLANNER_PUBLISH_KNOWN_HOSTS") or DEFAULT_PUBLISH_KNOWN_HOSTS,
            validate=_lazy_bundle_validator(),
        ),
        SqlitePublishJobRepository(db_path),
        GithubDeploymentStatusReader(os.environ.get("MEALPLANNER_PUBLISH_GITHUB_REPO") or DEFAULT_PUBLISH_GITHUB_REPO),
        now,
        app_revision=resolve_app_revision(),
        site_url=os.environ.get("MEALPLANNER_PUBLIC_SITE_URL") or DEFAULT_PUBLIC_SITE_URL,
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
    static_dir: Path | str | None = None,
    publishing_service: Any = None,
    auth_service: Any = None,
    trusted_proxies: int | None = None,
    cookie_secure: bool | None = None,
) -> Any:
    """Create the API. Source files are loaded and validated once.

    With db_path (or MEALPLANNER_DB) reads come from the SQLite store and the
    plan write routes are enabled. Otherwise the API is read-only. clock must return a
    timezone-aware datetime (tests inject one); the default reads the household timezone
    (MEALPLANNER_TIMEZONE).

    static_dir (or MEALPLANNER_STATIC_DIR) also serves the built frontend from the same origin.

    Household login (phase 6c): GET requests are open, every state-changing request and all of
    /api/v1/publishing need the mp_session cookie. Users live in SQLite (db_cli user ...);
    auth_service injects an AuthService (tests; without a database the default has no users).
    The cookie is Secure unless MEALPLANNER_COOKIE_SECURE=0 (cookie_secure). trusted_proxies
    (MEALPLANNER_TRUSTED_PROXIES=<n>, default 0) trusts n reverse-proxy hops for the client address,
    scheme and host (ProxyFix).

    Publishing (phase 6b) is configured by MEALPLANNER_PUBLISH_REPO (unset = disabled) and the other
    MEALPLANNER_PUBLISH_* / MEALPLANNER_PUBLIC_SITE_URL / MEALPLANNER_APP_REVISION settings;
    publishing_service injects a PublishingService instead (tests).
    """
    try:
        from flask import Flask, request
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
    app.config["SHOPPING_TRIP_SERVICE"] = None
    app.config["PUBLISHING_SERVICE"] = publishing_service
    app.config["PUBLIC_SITE_URL"] = os.environ.get("MEALPLANNER_PUBLIC_SITE_URL") or DEFAULT_PUBLIC_SITE_URL
    sqlite_path = db_path or os.environ.get("MEALPLANNER_DB")
    proxies = trusted_proxies if trusted_proxies is not None else int(_env_number_or_zero("MEALPLANNER_TRUSTED_PROXIES"))
    if proxies > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxies, x_proto=proxies, x_host=proxies)  # type: ignore[method-assign]
    app.config["AUTH_SERVICE"] = auth_service
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
            app.config["SHOPPING_TRIP_SERVICE"] = _build_trip_service(sqlite_path, timezone, clock)
            if publishing_service is None:
                app.config["PUBLISHING_SERVICE"] = _build_publishing_service(
                    sqlite_path, timezone, clock, app.config["SHOPPING_TRIP_SERVICE"]
                )
            if auth_service is None:
                app.config["AUTH_SERVICE"] = _build_auth_service(sqlite_path, timezone, clock)
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

    if app.config["AUTH_SERVICE"] is None:
        # No database (JSON snapshot mode) or an unusable one: nobody can log in, so changes stay refused.
        app.config["AUTH_SERVICE"] = _memory_auth_service(clock)
    secure = cookie_secure if cookie_secure is not None else os.environ.get("MEALPLANNER_COOKIE_SECURE") != "0"
    register_auth(app, _error, cookie_secure=secure)

    limit = max_concurrent_generations or _env_number(
        "MEALPLANNER_MAX_CONCURRENT_GENERATIONS", DEFAULT_MAX_CONCURRENT_GENERATIONS, int
    )
    gate = GenerationGate(limit)
    app.config["GENERATION_GATE"] = gate
    register_write_routes(app, _error, gate)

    register_read_routes(app, _error)
    register_trip_routes(app, _error)
    register_publish_routes(app, _error)
    register_health_route(app)
    static_root = static_dir or os.environ.get("MEALPLANNER_STATIC_DIR")
    if static_root:
        register_static_routes(app, static_root)

    return app


def main() -> None:
    """Run the local development server, refusing non-loopback bindings (the container uses gunicorn)."""
    host = os.environ.get("MEALPLANNER_HOST", "127.0.0.1")
    if host != "127.0.0.1":
        raise SystemExit("The development server may bind only to 127.0.0.1")
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
