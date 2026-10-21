"""Plan write/history routes. Parsing, error mapping, and serialization only.

All lifecycle rules live in PlanWorkflowService; this module never decides
business outcomes. Registered by app.create_app.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import date
from typing import Any

from ...application.errors import InvalidRequest, PlanNotFound
from ...application.plan_workflows import PlanWorkflowService
from ...domain.month import MONTH_PATTERN, MonthFillResult
from ...domain.weekly_plan import (
    DuplicateWeek,
    GenerationFailed,
    InputBasisChanged,
    InvalidWeek,
    LinkedPairSplit,
    MutationResult,
    NothingToUndo,
    PlanRevision,
    Proposal,
    ProposalExpired,
    ProposalNotFound,
    RetryIdReused,
    RuleViolationError,
    SlotChange,
    StaleRevision,
    WeekNotEditable,
    WorkflowError,
)

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IDEMPOTENCY_PATTERN = re.compile(r"^[\x21-\x7e]{1,128}$")
MAX_SEED = 2**31 - 1
MAX_REVISION = 2**31 - 1

# domain error -> (HTTP status, stable code)
ERROR_MAP: list[tuple[type[Exception], int, str]] = [
    (StaleRevision, 409, "stale_revision"),
    (ProposalNotFound, 404, "proposal_not_found"),
    (ProposalExpired, 410, "proposal_expired"),
    (InputBasisChanged, 409, "input_basis_changed"),
    (RetryIdReused, 422, "retry_id_reused"),
    (NothingToUndo, 409, "nothing_to_undo"),
    (LinkedPairSplit, 409, "linked_pair_split"),
    (WeekNotEditable, 409, "week_not_editable"),
    (DuplicateWeek, 409, "duplicate_week"),
    (InvalidWeek, 400, "invalid_week"),
    (GenerationFailed, 422, "generation_failed"),
    (RuleViolationError, 422, "rule_violation"),
    (PlanNotFound, 404, "plan_not_found"),
]


class GenerationGate:
    """Non-blocking bound on concurrent solver runs (busy -> generation_busy)."""

    def __init__(self, limit: int) -> None:
        self._sem = threading.BoundedSemaphore(max(1, limit))

    def try_acquire(self) -> bool:
        return self._sem.acquire(blocking=False)

    def release(self) -> None:
        self._sem.release()


class BodyError(Exception):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def _int_field(body: dict[str, Any], name: str, *, required: bool, low: int, high: int) -> int | None:
    if name not in body or body[name] is None:
        if required:
            raise BodyError(name, f"{name} is required")
        return None
    value = body[name]
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise BodyError(name, f"{name} must be an integer between {low} and {high}")
    return value


def _parse_body(allowed: set[str]) -> dict[str, Any]:
    from flask import request

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise BodyError("body", "request body must be a JSON object (Content-Type: application/json)")
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise BodyError(unknown[0], f"unknown field {unknown[0]!r}")
    return body


def serialize_revision_summary(rev: PlanRevision) -> dict[str, Any]:
    return {
        "revision": rev.revision,
        "snapshot_id": rev.snapshot.plan["snapshot_id"],
        "source": rev.source,
        "created_at": rev.created_at.isoformat(),
    }


def serialize_change(change: SlotChange) -> dict[str, Any]:
    return {
        "assignment_id": change.assignment_id,
        "date": change.date,
        "meal": change.meal,
        "kind": change.kind,
        "before": change.before,
        "after": change.after,
    }


def serialize_proposal(p: Proposal) -> dict[str, Any]:
    return {
        "proposal_id": p.proposal_id,
        "plan_id": p.plan_id,
        "base_revision": p.base_revision,
        "base_snapshot_id": p.base_snapshot_id,
        "seed": p.seed,
        "created_at": p.created_at.isoformat(),
        "expires_at": p.expires_at.isoformat(),
        "identical": p.is_identical,
        "changes": [serialize_change(c) for c in p.changes],
        "plan": p.snapshot.plan,
        "shopping_list": p.snapshot.shopping_list,
    }


def serialize_result(r: MutationResult) -> dict[str, Any]:
    return {
        "plan_id": r.plan_id,
        "revision": r.revision,
        "snapshot_id": r.snapshot_id,
        "changed": r.changed,
        "replayed": r.replayed,
    }


def serialize_month_fill(r: MonthFillResult) -> dict[str, Any]:
    weeks = []
    for w in r.weeks:
        item: dict[str, Any] = {
            "week_start": w.week_start.isoformat(),
            "week_end": w.week_end.isoformat(),
            "status": w.status,
        }
        if w.plan_id is not None:
            item["plan_id"] = w.plan_id
            item["revision"] = w.revision
        if w.error_code is not None:
            item["error"] = {"code": w.error_code, "message": w.error_message}
        weeks.append(item)
    return {"month": r.month, "weeks": weeks}


def register_write_routes(
    app: Any,
    error: Callable[..., Any],
    is_loopback: Callable[[], bool],
    gate: GenerationGate,
) -> None:
    from flask import jsonify, request

    def service() -> PlanWorkflowService | None:
        return app.config.get("PLAN_WORKFLOW_SERVICE")

    def can_edit() -> bool:
        return service() is not None and is_loopback()

    def guard():
        """Server-side authorization boundary for every mutation."""
        if not is_loopback():
            return error(403, "forbidden", "Plan changes are allowed from this machine only.")
        if service() is None:
            if app.config.get("PLAN_WRITE_FAILURE"):
                return error(503, "source_unavailable", "The plan database is unavailable.")
            return error(
                403, "read_only", "Plan changes need the SQLite-backed API (MEALPLANNER_DB)."
            )
        return None

    def check_plan_id(plan_id: str):
        if not ID_PATTERN.fullmatch(plan_id):
            return error(
                400, "invalid_request", "The plan identifier is invalid.",
                [{"field": "plan_id", "message": "Must be 1 to 128 ASCII letters, digits, underscores, or hyphens."}],
            )
        return None

    def check_proposal_id(proposal_id: str):
        if not ID_PATTERN.fullmatch(proposal_id):
            return error(
                400, "invalid_request", "The proposal identifier is invalid.",
                [{"field": "proposal_id", "message": "Must be 1 to 128 ASCII letters, digits, underscores, or hyphens."}],
            )
        return None

    def idempotency_key():
        values = request.headers.getlist("Idempotency-Key")
        if len(values) != 1 or not IDEMPOTENCY_PATTERN.fullmatch(values[0]):
            raise BodyError(
                "Idempotency-Key",
                "exactly one Idempotency-Key header of 1 to 128 printable ASCII characters is required",
            )
        return values[0]

    def mapped(fn: Callable[[], Any]):
        """Run fn, translating parsing and domain errors into the error envelope."""
        try:
            return fn()
        except BodyError as exc:
            return error(
                400, "invalid_request", "The request is invalid.",
                [{"field": exc.field, "message": str(exc)}],
            )
        except InvalidRequest as exc:
            return error(400, "invalid_request", "The request is invalid.", [{"field": "body", "message": str(exc)}])
        except WorkflowError as exc:
            return _domain_error(exc)
        except PlanNotFound:
            return error(404, "plan_not_found", "No plan exists for the requested plan_id.")
        except (sqlite3.Error, OSError):
            return error(503, "source_unavailable", "The plan data source is temporarily unavailable.")

    def _domain_error(exc: Exception):
        for cls, status, code in ERROR_MAP:
            if isinstance(exc, cls):
                details: list[dict[str, str]] = []
                if isinstance(exc, StaleRevision):
                    details = [
                        {"field": "expected_revision", "message": str(exc.expected)},
                        {"field": "actual_revision", "message": str(exc.actual)},
                    ]
                if isinstance(exc, RuleViolationError):
                    details = [{"field": v.rule, "message": v.message} for v in exc.violations]
                return error(status, code, _message(code, exc), details)
        raise exc

    def _message(code: str, exc: Exception) -> str:
        fixed = {
            "stale_revision": "The plan changed since you loaded it. Refetch and try again.",
            "proposal_not_found": "No such proposal; it may have been discarded or accepted.",
            "proposal_expired": "The proposal expired. Generate a new one.",
            "input_basis_changed": "Recipes, rules, or pantry changed since the preview. Generate a new proposal.",
            "retry_id_reused": "This Idempotency-Key was already used for a different request.",
            "nothing_to_undo": "The first revision has no earlier revision to restore.",
            "linked_pair_split": "A linked lunch and dinner must change together.",
            "rule_violation": "The plan breaks weekly rules.",
            "plan_not_found": "No plan exists for the requested plan_id.",
        }
        return fixed.get(code, str(exc) or code)

    def generating(fn: Callable[[], Any]):
        """Run a solver-backed action under the concurrency bound."""
        if not gate.try_acquire():
            response = error(503, "generation_busy", "A plan is already being generated. Try again shortly.")
            response.headers["Retry-After"] = "5"
            return response
        try:
            return fn()
        finally:
            gate.release()

    @app.get("/api/v1/capabilities")
    def capabilities():
        return jsonify({"can_edit_plans": can_edit()})

    @app.post("/api/v1/plans")
    def create_plan():
        denied = guard()
        if denied:
            return denied

        def run():
            body = _parse_body({"week_start", "seed"})
            week_text = body.get("week_start")
            if not isinstance(week_text, str) or not DATE_PATTERN.fullmatch(week_text):
                raise BodyError("week_start", "week_start must be an ISO date (YYYY-MM-DD)")
            try:
                week = date.fromisoformat(week_text)
            except ValueError as exc:
                raise BodyError("week_start", "week_start must be a real calendar date") from exc
            seed = _int_field(body, "seed", required=False, low=0, high=MAX_SEED)
            revision = generating(
                lambda: service().create_plan(week, 42 if seed is None else seed)
            )
            if not isinstance(revision, PlanRevision):
                return revision  # generation_busy response
            response = jsonify(revision.snapshot.plan)
            response.status_code = 201
            response.headers["Location"] = f"/api/v1/plans/{revision.plan_id}"
            return response

        return mapped(run)

    @app.post("/api/v1/months/<month>/fill")
    def fill_month(month: str):
        denied = guard()
        if denied:
            return denied

        def run():
            if not MONTH_PATTERN.fullmatch(month):
                raise BodyError("month", "month must be YYYY-MM")
            _parse_body(set())  # empty object only
            result = generating(lambda: service().fill_month(month))
            if not isinstance(result, MonthFillResult):
                return result
            return jsonify(serialize_month_fill(result))

        return mapped(run)

    @app.post("/api/v1/plans/<plan_id>/proposals")
    def propose(plan_id: str):
        denied = guard() or check_plan_id(plan_id)
        if denied:
            return denied

        def run():
            body = _parse_body({"expected_revision", "seed"})
            expected = _int_field(body, "expected_revision", required=True, low=1, high=MAX_REVISION)
            seed = _int_field(body, "seed", required=False, low=0, high=MAX_SEED)
            proposal = generating(lambda: service().propose_regeneration(plan_id, expected, seed))
            if not isinstance(proposal, Proposal):
                return proposal
            response = jsonify(serialize_proposal(proposal))
            response.status_code = 201
            response.headers["Location"] = (
                f"/api/v1/plans/{plan_id}/proposals/{proposal.proposal_id}"
            )
            return response

        return mapped(run)

    @app.get("/api/v1/plans/<plan_id>/proposals/<proposal_id>")
    def get_proposal(plan_id: str, proposal_id: str):
        denied = guard() or check_plan_id(plan_id) or check_proposal_id(proposal_id)
        if denied:
            return denied
        return mapped(lambda: jsonify(serialize_proposal(service().get_proposal(plan_id, proposal_id))))

    @app.delete("/api/v1/plans/<plan_id>/proposals/<proposal_id>")
    def discard_proposal(plan_id: str, proposal_id: str):
        denied = guard() or check_plan_id(plan_id) or check_proposal_id(proposal_id)
        if denied:
            return denied

        def run():
            service().discard_proposal(proposal_id, plan_id)
            return ("", 204)

        return mapped(run)

    @app.post("/api/v1/plans/<plan_id>/proposals/<proposal_id>/accept")
    def accept(plan_id: str, proposal_id: str):
        denied = guard() or check_plan_id(plan_id) or check_proposal_id(proposal_id)
        if denied:
            return denied

        def run():
            key = idempotency_key()
            body = _parse_body({"expected_revision"})
            expected = _int_field(body, "expected_revision", required=True, low=1, high=MAX_REVISION)
            result = service().accept_proposal(plan_id, proposal_id, expected, key)
            return jsonify(serialize_result(result))

        return mapped(run)

    @app.post("/api/v1/plans/<plan_id>/undo")
    def undo(plan_id: str):
        denied = guard() or check_plan_id(plan_id)
        if denied:
            return denied

        def run():
            key = idempotency_key()
            body = _parse_body({"expected_revision"})
            expected = _int_field(body, "expected_revision", required=True, low=1, high=MAX_REVISION)
            return jsonify(serialize_result(service().undo(plan_id, expected, key)))

        return mapped(run)

    @app.get("/api/v1/plans/<plan_id>/revisions")
    def revisions(plan_id: str):
        denied = guard() or check_plan_id(plan_id)
        if denied:
            return denied
        return mapped(
            lambda: jsonify(
                {
                    "plan_id": plan_id,
                    "items": [serialize_revision_summary(r) for r in service().list_revisions(plan_id)],
                }
            )
        )
