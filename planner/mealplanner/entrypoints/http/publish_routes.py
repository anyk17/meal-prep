"""Publish routes: parsing, error mapping and serialization only (rules live in application/publishing).

Publishing is a write to the public internet: every route needs a local request and a configured
PublishingService (SQLite-backed API with MEALPLANNER_PUBLISH_REPO), else 503 publishing_disabled.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from ...application.publishing import (
    InvalidMonths,
    JobNotFound,
    PublishFailed,
    PublishingDisabled,
    PublishingService,
    PublishInProgress,
    PublishJob,
)
from .write_routes import BodyError, _parse_body

DISABLED_MESSAGE = "Publishing is not configured (set MEALPLANNER_PUBLISH_REPO)."


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize_job(job: PublishJob | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        "job_id": job.job_id,
        "months": list(job.months),
        "status": job.status,
        "publication_id": job.publication_id,
        "commit_sha": job.commit_sha,
        "run_url": job.run_url,
        "error": job.error,
        "started_at": _time(job.started_at),
        "finished_at": _time(job.finished_at),
        "deployed_at": _time(job.deployed_at),
    }


def register_publish_routes(app: Any, error: Callable[..., Any]) -> None:
    from flask import jsonify, request

    def service() -> PublishingService | None:
        return app.config.get("PUBLISHING_SERVICE")

    def disabled():
        return error(503, "publishing_disabled", DISABLED_MESSAGE)

    def invalid_months(exc: InvalidMonths):
        return error(422, "invalid_months", str(exc), [{"field": "months", "message": str(exc)}])

    def failed(exc: PublishFailed):
        return error(502, "publish_failed", str(exc))

    @app.get("/api/v1/publishing")
    def publishing_status():
        svc = service()
        if svc is None:
            return jsonify({"enabled": False, "site_url": _site_url(app), "latest_job": None, "last_live_job": None})
        latest, live = svc.overview()
        return jsonify(
            {
                "enabled": True,
                "site_url": svc.site_url,
                "latest_job": serialize_job(latest),
                "last_live_job": serialize_job(live),
            }
        )

    @app.get("/api/v1/publishing/preview")
    def publishing_preview():
        svc = service()
        if svc is None:
            return disabled()
        raw = request.args.get("months", "")
        try:
            preview = svc.preview([part.strip() for part in raw.split(",")] if raw.strip() else [])
        except InvalidMonths as exc:
            return invalid_months(exc)
        except PublishFailed as exc:
            return failed(exc)
        return jsonify(
            {
                "months": list(preview.months),
                "weeks": list(preview.weeks),
                "trips": list(preview.trips),
                "unchanged": preview.unchanged,
            }
        )

    @app.post("/api/v1/publishing/jobs")
    def publishing_start():
        svc = service()
        if svc is None:
            return disabled()
        try:
            body = _parse_body({"months"})
            if "months" not in body:
                raise BodyError("months", "months is required")
        except BodyError as exc:
            return error(400, "invalid_request", str(exc), [{"field": exc.field, "message": str(exc)}])
        try:
            job = svc.publish(body["months"])
        except InvalidMonths as exc:
            return invalid_months(exc)
        except PublishInProgress:
            return error(409, "publish_in_progress", "A publication is already running. Try again in a minute.")
        except PublishingDisabled as exc:
            return error(503, "publishing_disabled", str(exc))
        except PublishFailed as exc:
            return failed(exc)
        response = jsonify(serialize_job(job))
        response.status_code = 201
        return response

    @app.get("/api/v1/publishing/jobs/<job_id>")
    def publishing_job(job_id: str):
        svc = service()
        if svc is None:
            return disabled()
        try:
            job = svc.get_job(job_id)
        except JobNotFound:
            return error(404, "not_found", "No publication job exists for the requested job_id.")
        return jsonify(serialize_job(job))


def _site_url(app: Any) -> str:
    return app.config.get("PUBLIC_SITE_URL") or ""
