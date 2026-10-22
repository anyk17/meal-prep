"""Shopping trip routes and week coverage. Parsing, error mapping, and serialization only.

All trip rules live in ShoppingTripService and domain/shopping_trip.py. Reads are open
like other reads (an empty list when the API is not SQLite-backed); every mutation is
gated exactly like the plan writes (a logged-in user and a SQLite-backed API, else 401 or 403).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from typing import Any

from ...application.errors import InvalidRequest, PlanNotFound
from ...application.shopping_trips import ShoppingTripService
from ...domain import shopping_trip as trips
from .read_routes import _read_date_argument
from .write_routes import ID_PATTERN, MAX_REVISION, BodyError, _int_field, _parse_body

MAX_TRIP_WEEKS = 60
INTEGER_PATTERN = re.compile(r"^[0-9]+$")

# domain error -> (HTTP status, stable code)
TRIP_ERROR_MAP: list[tuple[type[Exception], int, str]] = [
    (trips.StaleVersion, 409, "stale_version"),
    (trips.TripNotFound, 404, "trip_not_found"),
    (trips.WeekAlreadyOnTrip, 409, "week_already_on_trip"),
    (trips.TripDone, 409, "trip_done"),
    (trips.TripNotDone, 409, "trip_not_done"),
    (trips.WeekNotOnTrip, 404, "week_not_on_trip"),
    (trips.LastWeek, 409, "last_week"),
    (trips.InvalidTrip, 400, "invalid_request"),
]

MESSAGES = {
    "stale_version": "The shopping list changed since you loaded it. Refetch and try again.",
    "trip_not_found": "No shopping list exists for the requested trip_id.",
    "week_already_on_trip": "A week can be on only one shopping list. Remove it from the other list first.",
    "trip_done": "This shopping list is done. Reopen it to change its weeks.",
    "trip_not_done": "This shopping list is not in the state this action needs.",
    "week_not_on_trip": "That week is not on this shopping list.",
    "last_week": "A shopping list needs at least one week. Delete the list instead.",
}


def register_trip_routes(app: Any, error: Callable[..., Any]) -> None:
    from flask import jsonify, request

    def service() -> ShoppingTripService | None:
        return app.config.get("SHOPPING_TRIP_SERVICE")

    def source_failure():
        code = app.config["PLAN_SOURCE_FAILURE"]
        if not code:
            return None
        message = (
            "The plan data source is temporarily unavailable."
            if code == "source_unavailable"
            else "The configured plan and shopping data could not be validated."
        )
        return error(503, code, message)

    def guard():
        """Mutations need the SQLite-backed API; the login check itself is app.py's before_request guard."""
        if service() is None:
            return error(
                403, "read_only", "Plan changes need the SQLite-backed API (MEALPLANNER_DB)."
            )
        return None

    def check_id(name: str, value: str):
        if not ID_PATTERN.fullmatch(value):
            return error(
                400, "invalid_request", f"The {name} identifier is invalid.",
                [{"field": name, "message": "Must be 1 to 128 ASCII letters, digits, underscores, or hyphens."}],
            )
        return None

    def expected_version_query() -> int:
        values = request.args.getlist("expected_version")
        if len(values) != 1 or not INTEGER_PATTERN.fullmatch(values[0]) or len(values[0]) > 10:
            raise BodyError("expected_version", "expected_version is required once as a positive integer")
        value = int(values[0])
        if not 1 <= value <= MAX_REVISION:
            raise BodyError("expected_version", "expected_version must be a positive integer")
        return value

    def mapped(fn: Callable[[], Any]):
        try:
            return fn()
        except BodyError as exc:
            return error(
                400, "invalid_request", "The request is invalid.",
                [{"field": exc.field, "message": str(exc)}],
            )
        except PlanNotFound:
            return error(404, "plan_not_found", "No plan exists for the requested plan_id.")
        except trips.TripError as exc:
            for cls, status, code in TRIP_ERROR_MAP:
                if isinstance(exc, cls):
                    details: list[dict[str, str]] = []
                    if isinstance(exc, trips.StaleVersion):
                        details = [
                            {"field": "expected_version", "message": str(exc.expected)},
                            {"field": "actual_version", "message": str(exc.actual)},
                        ]
                    elif isinstance(exc, trips.WeekAlreadyOnTrip):
                        details = [{"field": "plan_id", "message": exc.plan_id}]
                        if exc.trip_id:
                            details.append({"field": "trip_id", "message": exc.trip_id})
                    return error(status, code, MESSAGES.get(code, str(exc)), details)
            raise
        except (sqlite3.Error, OSError):
            return error(503, "source_unavailable", "The plan data source is temporarily unavailable.")

    @app.get("/api/v1/shopping-trips")
    def list_trips():
        failed = source_failure()
        if failed:
            return failed
        current = service()
        return mapped(lambda: jsonify(current.list() if current else {"items": []}))

    @app.get("/api/v1/shopping-trips/<trip_id>")
    def get_trip(trip_id: str):
        failed = source_failure() or check_id("trip_id", trip_id)
        if failed:
            return failed
        current = service()

        def run():
            if current is None:
                raise trips.TripNotFound(trip_id)
            return jsonify(current.get(trip_id))

        return mapped(run)

    @app.get("/api/v1/coverage")
    def coverage():
        failed = source_failure()
        if failed:
            return failed
        reader = app.config["PLAN_READ_SERVICE"]
        try:
            from_date = _read_date_argument("from")
            to_date = _read_date_argument("to")
            listed = reader.list_plans(from_date, to_date)["items"]
        except InvalidRequest as exc:
            field = "from" if str(exc).startswith("from ") else "to"
            return error(400, "invalid_request", "The date range is invalid.",
                         [{"field": field, "message": str(exc)}])
        current = service()
        if current is not None:
            return mapped(lambda: jsonify(current.coverage(listed)))
        return jsonify(
            {
                "items": [
                    {
                        "plan_id": s["plan_id"],
                        "week_start": s["week_start"],
                        "week_end": s["week_end"],
                        "coverage": trips.NOT_COVERED,
                        "trip_id": None,
                        "changed_since_shopping": False,
                    }
                    for s in listed
                ]
            }
        )

    @app.post("/api/v1/shopping-trips")
    def create_trip():
        denied = guard()
        if denied:
            return denied

        def run():
            body = _parse_body({"plan_ids", "name"})
            plan_ids = body.get("plan_ids")
            if (
                not isinstance(plan_ids, list)
                or not 1 <= len(plan_ids) <= MAX_TRIP_WEEKS
                or not all(isinstance(p, str) and ID_PATTERN.fullmatch(p) for p in plan_ids)
            ):
                raise BodyError("plan_ids", f"plan_ids must list 1 to {MAX_TRIP_WEEKS} plan identifiers")
            if len(set(plan_ids)) != len(plan_ids):
                raise BodyError("plan_ids", "plan_ids must be unique")
            name = body.get("name")
            if name is not None and not isinstance(name, str):
                raise BodyError("name", "name must be a string")
            if isinstance(name, str) and len(" ".join(name.split())) > trips.MAX_NAME_LENGTH:
                raise BodyError("name", f"name must be at most {trips.MAX_NAME_LENGTH} characters")
            view = service().create(plan_ids, name)
            response = jsonify(view)
            response.status_code = 201
            response.headers["Location"] = f"/api/v1/shopping-trips/{view['trip_id']}"
            return response

        return mapped(run)

    @app.post("/api/v1/shopping-trips/<trip_id>/weeks")
    def add_week(trip_id: str):
        denied = guard() or check_id("trip_id", trip_id)
        if denied:
            return denied

        def run():
            body = _parse_body({"plan_id", "expected_version"})
            plan_id = body.get("plan_id")
            if not isinstance(plan_id, str) or not ID_PATTERN.fullmatch(plan_id):
                raise BodyError("plan_id", "plan_id must be a plan identifier")
            expected = _int_field(body, "expected_version", required=True, low=1, high=MAX_REVISION)
            return jsonify(service().add_week(trip_id, plan_id, expected))

        return mapped(run)

    @app.delete("/api/v1/shopping-trips/<trip_id>/weeks/<plan_id>")
    def remove_week(trip_id: str, plan_id: str):
        denied = guard() or check_id("trip_id", trip_id) or check_id("plan_id", plan_id)
        if denied:
            return denied
        return mapped(
            lambda: jsonify(service().remove_week(trip_id, plan_id, expected_version_query()))
        )

    def version_action(name: str):
        def run_action(trip_id: str):
            denied = guard() or check_id("trip_id", trip_id)
            if denied:
                return denied

            def run():
                body = _parse_body({"expected_version"})
                expected = _int_field(
                    body, "expected_version", required=True, low=1, high=MAX_REVISION
                )
                return jsonify(getattr(service(), name)(trip_id, expected))

            return mapped(run)

        return run_action

    app.add_url_rule(
        "/api/v1/shopping-trips/<trip_id>/done", "mark_trip_done", version_action("mark_done"),
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/v1/shopping-trips/<trip_id>/reopen", "reopen_trip", version_action("reopen"),
        methods=["POST"],
    )

    @app.delete("/api/v1/shopping-trips/<trip_id>")
    def delete_trip(trip_id: str):
        denied = guard() or check_id("trip_id", trip_id)
        if denied:
            return denied

        def run():
            service().delete(trip_id, expected_version_query())
            return ("", 204)

        return mapped(run)
