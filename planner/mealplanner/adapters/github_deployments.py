"""DeploymentStatusReader over the unauthenticated GitHub REST API (the public repo needs no token).

Finds the workflow runs for a commit SHA: any run still queued/in progress means pending; all
completed and successful means success; any other conclusion means failure. Short timeout, no retry
loop, and results (including failures) are cached per SHA for ``cache_seconds`` to stay far below the
60 requests/hour unauthenticated limit.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ..application.publishing import DeploymentStatus, DeploymentStatusError

API = "https://api.github.com"
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class GithubDeploymentStatusReader:
    def __init__(
        self,
        repo: str = "jflopezo91/meal-plan",
        *,
        timeout: float = 5.0,
        cache_seconds: float = 10.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not REPO_PATTERN.fullmatch(repo):
            raise ValueError("GitHub repository must look like owner/name")
        self._repo = repo
        self._timeout = timeout
        self._ttl = cache_seconds
        self._open = opener
        self._now = monotonic
        self._cache: dict[str, tuple[float, DeploymentStatus | None]] = {}

    def status(self, commit_sha: str) -> DeploymentStatus:
        if not SHA_PATTERN.fullmatch(commit_sha):
            raise DeploymentStatusError("not a commit SHA")
        cached = self._cache.get(commit_sha)
        if cached is not None and self._now() - cached[0] < self._ttl:
            if cached[1] is None:
                raise DeploymentStatusError("GitHub status unavailable (cached)")
            return cached[1]
        try:
            result = self._fetch(commit_sha)
        except DeploymentStatusError:
            self._cache[commit_sha] = (self._now(), None)
            raise
        self._cache[commit_sha] = (self._now(), result)
        return result

    def _fetch(self, commit_sha: str) -> DeploymentStatus:
        request = urllib.request.Request(
            f"{API}/repos/{self._repo}/actions/runs?head_sha={commit_sha}&per_page=20",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "meal-prep-publisher",
            },
        )
        try:
            with self._open(request, timeout=self._timeout) as response:
                payload = json.loads(response.read(1_000_000))
            runs = [r for r in payload["workflow_runs"] if r.get("head_sha") == commit_sha]
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
            raise DeploymentStatusError("GitHub did not answer") from exc
        if not runs:
            return DeploymentStatus("pending")  # the workflow has not started yet
        pending = [r for r in runs if r.get("status") != "completed"]
        failed = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") != "success"]
        if failed:
            return DeploymentStatus("failure", failed[0].get("html_url"))
        if pending:
            return DeploymentStatus("pending", pending[0].get("html_url"))
        return DeploymentStatus("success", runs[0].get("html_url"))
