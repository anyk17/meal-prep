"""Publish from the home app: export selected months, push them to the public repository.

Orchestration only. ``PublicationService`` builds the checked bundle; the ``PublicationRepository``
port (a git working copy of the public repo) stores it and pushes; ``DeploymentStatusReader`` asks
GitHub whether the push became a live Pages deployment; ``PublishJobRepository`` keeps the private
job records. The remote is authoritative: every publish starts by syncing the working copy to it, an
identical bundle (same content, ignoring ``publication_id``/``exported_at``) is a no-op, and a
failure at any step leaves the previously pushed version untouched.

One job at a time: a process lock plus the database check in ``PublishJobRepository.start`` (a
``running`` row older than ``stale_after`` counts as a crashed job and is marked failed).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from ..domain.snapshot import JsonObject
from .ports import DeploymentStatusReader, PublicationRepository, PublishJobRepository
from .publication import MONTH, PublicationError, PublicationResult, PublicationService

LOG = logging.getLogger(__name__)

RUNNING, PUSHED, LIVE, FAILED, UNCHANGED = "running", "pushed", "live", "failed", "unchanged"
MAX_MONTHS = 12
DEFAULT_STALE_AFTER = timedelta(minutes=10)
PREVIEW_SYNC_TTL_SECONDS = 30.0
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_PATH_LIKE = re.compile(r"(?<![\w.])/[^\s'\":]+")
MAX_ERROR_LENGTH = 500

Clock = Callable[[], datetime]  # must return a timezone-aware datetime


# ---------------------------------------------------------------- errors


class PublishingError(Exception):
    """Base class for expected publishing failures."""


class InvalidMonths(PublishingError):
    pass


class PublishInProgress(PublishingError):
    pass


class PublishingDisabled(PublishingError):
    pass


class JobNotFound(PublishingError):
    pass


class PublishFailed(PublishingError):
    """The publication failed; ``message`` is safe to show (no paths, no secrets)."""

    def __init__(self, message: str, job: PublishJob | None = None) -> None:
        super().__init__(message)
        self.job = job


class PublishRepoError(Exception):
    """A repository (git) step failed. The message must already be safe to surface."""


class DeploymentStatusError(Exception):
    """The deployment status could not be determined right now (network, rate limit)."""


# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class PublishJob:
    job_id: str
    months: tuple[str, ...]
    status: str
    started_at: datetime
    publication_id: str | None = None
    commit_sha: str | None = None
    run_url: str | None = None
    error: str | None = None
    tokens: JsonObject = field(default_factory=dict)  # plans/trips exported, content_hash, app_ref
    finished_at: datetime | None = None
    deployed_at: datetime | None = None


@dataclass(frozen=True)
class PublishedContent:
    """What the public repository currently serves, read from the synced working copy."""

    publication_id: str
    index: JsonObject
    files: Mapping[str, JsonObject]
    app_ref: str | None


@dataclass(frozen=True)
class DeploymentStatus:
    state: str  # "pending" | "success" | "failure"
    run_url: str | None = None


@dataclass(frozen=True)
class PublishPreview:
    months: tuple[str, ...]
    weeks: tuple[JsonObject, ...]
    trips: tuple[JsonObject, ...]
    unchanged: bool


# ---------------------------------------------------------------- helpers


def validate_months(months: object) -> list[str]:
    """Sorted months; rejects empty, malformed, duplicate or more than MAX_MONTHS entries."""
    if not isinstance(months, (list, tuple)) or not months:
        raise InvalidMonths("Select at least one month (YYYY-MM).")
    if len(months) > MAX_MONTHS:
        raise InvalidMonths(f"Select at most {MAX_MONTHS} months.")
    for month in months:
        if not isinstance(month, str) or not MONTH.fullmatch(month):
            raise InvalidMonths(f"Not a YYYY-MM month: {str(month)[:20]!r}.")
    if len(set(months)) != len(months):
        raise InvalidMonths("Each month may be selected only once.")
    return sorted(months)


def content_hash(index: JsonObject, files: Mapping[str, JsonObject]) -> str:
    """Canonical hash of a publication's content; ``publication_id`` and ``exported_at`` do not count."""
    normalized = {path: content for path, content in files.items()}
    manifest = {k: v for k, v in files["manifest.json"].items() if k not in ("publication_id", "exported_at")}
    normalized["manifest.json"] = manifest
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def safe_message(text: str) -> str:
    """Strip absolute paths and cap the length of a message before it is stored or shown."""
    cleaned = _PATH_LIKE.sub("<path>", " ".join(str(text).split()))
    return cleaned[:MAX_ERROR_LENGTH]


def _tokens(result: PublicationResult, digest: str, app_ref: str | None) -> JsonObject:
    return {
        "plans": [
            {"plan_id": p["plan_id"], "revision": p["revision"], "snapshot_id": p["snapshot_id"]}
            for p in result.plans
        ],
        "trips": [{"trip_id": t["trip_id"], "status": t["status"]} for t in result.trips_included],
        "content_hash": digest,
        "app_ref": app_ref,
    }


# ---------------------------------------------------------------- service


class PublishingService:
    def __init__(
        self,
        publication: PublicationService,
        repository: PublicationRepository,
        jobs: PublishJobRepository,
        deployments: DeploymentStatusReader,
        clock: Clock,
        *,
        app_revision: str | None,
        site_url: str,
        id_factory: Callable[[], str] = lambda: secrets.token_hex(4),
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._publication = publication
        self._repo = repository
        self._jobs = jobs
        self._deployments = deployments
        self._clock = clock
        self._app_ref = app_revision if app_revision and FULL_SHA.fullmatch(app_revision) else None
        self.site_url = site_url
        self._new_id = id_factory
        self._stale_after = stale_after
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._published_cache: tuple[float, str, str | None] | None = None  # (when, hash, app_ref)

    # ------------------------------------------------------------ reads

    def overview(self) -> tuple[PublishJob | None, PublishJob | None]:
        """(latest job, last live job); a pushed latest job is refreshed from GitHub first."""
        latest = self._jobs.latest()
        if latest is not None:
            latest = self._refresh(latest)
        return latest, self._jobs.last_with_status((LIVE,))

    def get_job(self, job_id: str) -> PublishJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(job_id)
        return self._refresh(job)

    def preview(self, months: object) -> PublishPreview:
        selected = validate_months(months)
        try:
            bundle, result = self._publication.build(selected)
        except PublicationError as exc:
            raise PublishFailed(self._describe(exc)) from exc
        live = self._jobs.last_with_status((LIVE,))
        published = {p["plan_id"]: p["revision"] for p in (live.tokens.get("plans") or [])} if live else {}
        weeks = tuple(
            {
                "plan_id": p["plan_id"],
                "week_start": p["week_start"],
                "week_end": p["week_end"],
                "revision": p["revision"],
                "published_revision": published.get(p["plan_id"]),
            }
            for p in result.plans
        )
        digest = content_hash(bundle.index, bundle.files)
        return PublishPreview(tuple(selected), weeks, result.trips_included, self._is_unchanged(digest))

    # ------------------------------------------------------------ publish

    def publish(self, months: object) -> PublishJob:
        selected = validate_months(months)
        if self._app_ref is None:
            raise PublishingDisabled(
                "The application revision is unknown, so the public site cannot be built from matching code. "
                "Set MEALPLANNER_APP_REVISION to the meal-prep commit SHA."
            )
        if not self._lock.acquire(blocking=False):
            raise PublishInProgress("A publication is already running.")
        try:
            now = self._clock()
            job = PublishJob(
                job_id=f"job_{now.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}_{self._new_id()}",
                months=tuple(selected),
                status=RUNNING,
                started_at=now,
            )
            self._jobs.start(job, now - self._stale_after)  # raises PublishInProgress
            self._published_cache = None
            try:
                return self._run(job, selected)
            except Exception as exc:
                if isinstance(exc, (PublicationError, PublishRepoError)):
                    message = self._describe(exc)
                else:
                    LOG.exception("publication job %s failed unexpectedly", job.job_id)
                    message = "Unexpected error while publishing; see the server log."
                failed = replace(job, status=FAILED, error=message, finished_at=self._clock())
                self._jobs.save(failed)
                raise PublishFailed(message, failed) from exc
        finally:
            self._lock.release()

    def _run(self, job: PublishJob, months: list[str]) -> PublishJob:
        published = self._repo.sync()
        bundle, result = self._publication.build(months)
        digest = content_hash(bundle.index, bundle.files)
        tokens = _tokens(result, digest, self._app_ref)
        if (
            published is not None
            and published.app_ref == self._app_ref
            and content_hash(published.index, published.files) == digest
        ):
            done = replace(
                job,
                status=UNCHANGED,
                publication_id=published.publication_id,
                tokens=tokens,
                finished_at=self._clock(),
            )
            self._jobs.save(done)
            return done
        assert self._app_ref is not None
        self._repo.write(bundle, self._app_ref)
        sha = self._repo.commit_and_push(
            f"Publish {bundle.publication_id} ({', '.join(months)})"
        )
        pushed = replace(
            job,
            status=PUSHED,
            publication_id=bundle.publication_id,
            commit_sha=sha,
            tokens=tokens,
            finished_at=self._clock(),
        )
        self._jobs.save(pushed)
        return pushed

    # ------------------------------------------------------------ internals

    @staticmethod
    def _describe(exc: PublicationError | PublishRepoError) -> str:
        detail = "; ".join(getattr(exc, "errors", [])[:3])
        return safe_message(f"{exc}: {detail}" if detail else str(exc))

    def _refresh(self, job: PublishJob) -> PublishJob:
        """Turn a pushed job into live/failed once GitHub says so."""
        if job.status != PUSHED or not job.commit_sha:
            return job
        try:
            status = self._deployments.status(job.commit_sha)
        except DeploymentStatusError:
            return job  # unknown right now: stay pushed, never claim live
        if status.state == "success":
            updated = replace(job, status=LIVE, run_url=status.run_url or job.run_url, deployed_at=self._clock())
        elif status.state == "failure":
            updated = replace(
                job,
                status=FAILED,
                run_url=status.run_url or job.run_url,
                error="The GitHub Pages deployment failed. The previous public version stays live.",
            )
        else:
            updated = replace(job, run_url=status.run_url or job.run_url)
        if updated != job:
            self._jobs.save(updated)
        return updated

    def _is_unchanged(self, digest: str) -> bool:
        """Would publishing a bundle with this content be a no-op?

        Asks the remote (through a synced working copy, at most every PREVIEW_SYNC_TTL_SECONDS and
        never while a publication runs); if that is not possible, falls back to the last successful job.
        """
        if self._app_ref is None:
            return False
        state = self._published_state()
        if state is None:
            last = self._jobs.last_with_status((PUSHED, LIVE, UNCHANGED))
            if last is None:
                return False
            state = (str(last.tokens.get("content_hash")), last.tokens.get("app_ref"))
        return state == (digest, self._app_ref)

    def _published_state(self) -> tuple[str, str | None] | None:
        cached = self._published_cache
        if cached is not None and self._monotonic() - cached[0] < PREVIEW_SYNC_TTL_SECONDS:
            return cached[1], cached[2]
        if not self._lock.acquire(blocking=False):
            return None
        try:
            content = self._repo.sync()
        except PublishRepoError as exc:
            LOG.warning("preview could not sync the publication repository: %s", exc)
            return None
        finally:
            self._lock.release()
        if content is None:
            return None
        state = (content_hash(content.index, content.files), content.app_ref)
        self._published_cache = (self._monotonic(), *state)
        return state


__all__ = [
    "DeploymentStatus",
    "DeploymentStatusError",
    "InvalidMonths",
    "JobNotFound",
    "PublishFailed",
    "PublishInProgress",
    "PublishJob",
    "PublishPreview",
    "PublishRepoError",
    "PublishedContent",
    "PublishingDisabled",
    "PublishingService",
    "content_hash",
    "validate_months",
]
