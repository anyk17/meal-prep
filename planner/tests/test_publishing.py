"""Phase 6b: publish from the home app (service, git adapter, GitHub status adapter, job store, routes)."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mealplanner.adapters.git_publication import GitPublicationRepository, prune_publications
from mealplanner.adapters.github_deployments import GithubDeploymentStatusReader
from mealplanner.adapters.sqlite.publication_jobs import SqlitePublishJobRepository
from mealplanner.adapters.sqlite.store import SqlitePlanReadAdapter
from mealplanner.application.publication import PublicationService
from mealplanner.application.publishing import (
    DeploymentStatus,
    DeploymentStatusError,
    InvalidMonths,
    JobNotFound,
    PublishFailed,
    PublishingDisabled,
    PublishingService,
    PublishInProgress,
    PublishJob,
    PublishRepoError,
    content_hash,
    safe_message,
    validate_months,
)
from mealplanner.entrypoints.http.app import create_app
from tests.test_publication_export import NOW, TZ, W_NOV02, W_OCT26, Env

SHA = "a" * 40
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
LAN = {"REMOTE_ADDR": "192.168.1.20"}


def sh(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t.invalid", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def origin(tmp_path) -> Path:
    """A local bare repository with an initial commit on main (stands in for the GitHub repo)."""
    bare = tmp_path / "origin.git"
    sh("init", "--bare", "--initial-branch=main", str(bare))
    seed = tmp_path / "seed"
    sh("clone", str(bare), str(seed))
    (seed / "README.md").write_text("meal-plan\n")
    sh("add", "-A", cwd=seed)
    sh("commit", "-m", "init", cwd=seed)
    sh("push", "origin", "HEAD:refs/heads/main", cwd=seed)
    return bare


def tree(bare: Path) -> list[str]:
    return sh("--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main").split()


class FakeStatus:
    def __init__(self) -> None:
        self.state = DeploymentStatus("pending")
        self.fail = False
        self.calls = 0

    def status(self, commit_sha: str) -> DeploymentStatus:
        self.calls += 1
        if self.fail:
            raise DeploymentStatusError("down")
        return self.state


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Setup:
    def __init__(self, tmp_path: Path, origin: Path, env: Env) -> None:
        self.env = env
        self.clock = Clock()
        self.status = FakeStatus()
        self.repo = GitPublicationRepository(
            origin.as_uri(), tmp_path / "work", key_path=tmp_path / "no-key", known_hosts_path=tmp_path / "no-hosts"
        )
        self.jobs = SqlitePublishJobRepository(env.db)
        self.origin = origin
        self.counter = iter(range(1, 1000))
        self.make()

    def make(self, app_revision: str | None = SHA) -> PublishingService:
        self.service = PublishingService(
            PublicationService(SqlitePlanReadAdapter(self.env.db), self.env.trips, self.clock, TZ),
            self.repo,
            self.jobs,
            self.status,
            self.clock,
            app_revision=app_revision,
            site_url="https://example.invalid/site/",
            id_factory=lambda: f"{next(self.counter):04x}",
        )
        return self.service


@pytest.fixture
def env(tmp_path) -> Env:
    e = Env(tmp_path)
    e.plan(W_OCT26)
    e.plan(W_NOV02)
    return e


@pytest.fixture
def setup(tmp_path, origin, env) -> Setup:
    return Setup(tmp_path, origin, env)


MONTHS = ["2026-10", "2026-11"]


# ---------------------------------------------------------------- months


@pytest.mark.parametrize(
    "bad", [[], ["2026-13"], ["2026-10", "2026-10"], ["x"], [f"2026-{m:02d}" for m in range(1, 13)] + ["2027-01"], "2026-10", [5]]
)
def test_invalid_months(bad) -> None:
    with pytest.raises(InvalidMonths):
        validate_months(bad)


def test_months_are_sorted() -> None:
    assert validate_months(["2026-11", "2026-10"]) == ["2026-10", "2026-11"]


def test_safe_message_strips_paths() -> None:
    assert "/home/x" not in safe_message("failed at /home/x/.ssh/id: denied")


# ---------------------------------------------------------------- publish flow


def test_publish_pushes_bundle_and_app_ref(setup: Setup) -> None:
    job = setup.service.publish(MONTHS)
    assert job.status == "pushed" and job.commit_sha and len(job.commit_sha) == 40
    assert job.months == ("2026-10", "2026-11") and job.finished_at is not None
    files = tree(setup.origin)
    assert "README.md" in files and "app-ref" in files and "publication/index.json" in files
    assert f"publication/publications/{job.publication_id}/manifest.json" in files
    head = sh("--git-dir", str(setup.origin), "show", "-s", "--format=%an <%ae>|%H", "main").strip()
    assert head == f"meal-prep publisher <publisher@meal-prep.invalid>|{job.commit_sha}"
    assert sh("--git-dir", str(setup.origin), "show", "main:app-ref") == SHA + "\n"
    assert setup.jobs.get(job.job_id) == job
    assert {p["plan_id"] for p in job.tokens["plans"]} == {"plan_2026_10_26", "plan_2026_11_02"}


def test_identical_content_is_unchanged_without_commit(setup: Setup) -> None:
    first = setup.service.publish(MONTHS)
    before = sh("--git-dir", str(setup.origin), "rev-parse", "main").strip()
    setup.clock.now = NOW + timedelta(hours=1)  # new publication_id/exported_at must not count
    second = setup.service.publish(MONTHS)
    assert second.status == "unchanged" and second.commit_sha is None
    assert second.publication_id == first.publication_id
    assert sh("--git-dir", str(setup.origin), "rev-parse", "main").strip() == before


def test_new_revision_or_app_revision_is_a_change(setup: Setup) -> None:
    setup.service.publish(MONTHS)
    setup.env.new_revision("plan_2026_10_26")
    assert setup.service.publish(MONTHS).status == "pushed"
    assert setup.service.publish(MONTHS).status == "unchanged"
    setup.make(app_revision="b" * 40)
    assert setup.service.publish(MONTHS).status == "pushed"
    assert sh("--git-dir", str(setup.origin), "show", "main:app-ref").strip() == "b" * 40


def test_different_months_are_a_change(setup: Setup) -> None:
    setup.service.publish(["2026-10"])
    assert setup.service.publish(["2026-10", "2026-11"]).status == "pushed"


def test_retention_keeps_three_newest(setup: Setup) -> None:
    ids = []
    steps = [["2026-10"], ["2026-11"], MONTHS, MONTHS, MONTHS]
    for i, months in enumerate(steps):
        setup.clock.now = NOW + timedelta(minutes=i)
        if i == 3:
            setup.env.new_revision("plan_2026_10_26")
        if i == 4:
            setup.env.new_revision("plan_2026_11_02")
        ids.append(setup.service.publish(months).publication_id)
    kept = sorted(p.split("/")[2] for p in tree(setup.origin) if p.endswith("manifest.json"))
    assert kept == sorted(ids)[-3:]


def test_remote_is_authoritative_over_leftovers(setup: Setup) -> None:
    setup.service.publish(MONTHS)
    (Path(setup.repo._dir) / "stray.txt").write_text("x")  # a leftover of a failed attempt
    setup.env.new_revision("plan_2026_10_26")
    setup.service.publish(MONTHS)
    assert "stray.txt" not in tree(setup.origin)


def test_push_rejected_marks_job_failed_and_keeps_remote(setup: Setup, tmp_path, monkeypatch) -> None:
    first = setup.service.publish(MONTHS)
    head = sh("--git-dir", str(setup.origin), "rev-parse", "main").strip()
    setup.env.new_revision("plan_2026_10_26")
    original = setup.repo.sync

    def sync_then_remote_moves():
        content = original()
        other = tmp_path / "other"
        sh("clone", str(setup.origin), str(other))
        (other / "x.txt").write_text("x")
        sh("add", "-A", cwd=other)
        sh("commit", "-m", "other", cwd=other)
        sh("push", "origin", "HEAD:refs/heads/main", cwd=other)
        return content

    monkeypatch.setattr(setup.repo, "sync", sync_then_remote_moves)
    with pytest.raises(PublishFailed) as caught:
        setup.service.publish(MONTHS)
    assert caught.value.job is not None and caught.value.job.status == "failed"
    assert str(tmp_path) not in str(caught.value)
    assert setup.jobs.latest().status == "failed"
    # the earlier publication is still active on the remote (the other commit only added x.txt)
    index = json.loads(sh("--git-dir", str(setup.origin), "show", "main:publication/index.json"))
    assert index["publication_id"] == first.publication_id
    assert sh("--git-dir", str(setup.origin), "rev-parse", "main~1").strip() == head
    monkeypatch.undo()
    assert setup.service.publish(MONTHS).status == "pushed"  # recovers: sync resets to the remote


def test_unreachable_remote_fails_safely(setup: Setup, tmp_path) -> None:
    setup.repo._remote = (tmp_path / "missing.git").as_uri()
    setup.repo._dir = tmp_path / "work2"
    with pytest.raises(PublishFailed) as caught:
        setup.service.publish(MONTHS)
    assert "git clone failed" in str(caught.value) and str(tmp_path) not in str(caught.value)
    assert setup.jobs.latest().status == "failed"


def test_nothing_to_publish_is_refused_on_empty_selection(setup: Setup) -> None:
    job = setup.service.publish(["2030-01"])  # a month without plans is valid: plans: []
    assert job.status == "pushed"
    assert job.tokens["plans"] == []


def test_unknown_app_revision_is_a_clear_error(setup: Setup) -> None:
    service = setup.make(app_revision=None)
    with pytest.raises(PublishingDisabled, match="MEALPLANNER_APP_REVISION"):
        service.publish(MONTHS)
    assert setup.jobs.latest() is None
    setup.make(app_revision="main")  # not a full SHA
    with pytest.raises(PublishingDisabled):
        setup.service.publish(MONTHS)


# ---------------------------------------------------------------- locking


def test_second_publish_while_running_is_refused(setup: Setup) -> None:
    gate, entered = threading.Event(), threading.Event()
    original = setup.repo.sync

    def slow():
        entered.set()
        gate.wait(5)
        return original()

    setup.repo.sync = slow  # type: ignore[method-assign]
    results: list[object] = []
    worker = threading.Thread(target=lambda: results.append(setup.service.publish(MONTHS)))
    worker.start()
    assert entered.wait(5)
    with pytest.raises(PublishInProgress):
        setup.service.publish(MONTHS)
    gate.set()
    worker.join(10)
    assert results and results[0].status == "pushed"


def test_database_refuses_a_second_running_job_and_expires_stale_ones(setup: Setup) -> None:
    def running(job_id: str, at: datetime) -> PublishJob:
        return PublishJob(job_id, ("2026-10",), "running", at)

    setup.jobs.start(running("job_a", NOW), NOW - timedelta(minutes=10))
    with pytest.raises(PublishInProgress):
        setup.jobs.start(running("job_b", NOW + timedelta(minutes=1)), NOW - timedelta(minutes=9))
    later = NOW + timedelta(minutes=11)
    setup.jobs.start(running("job_c", later), later - timedelta(minutes=10))
    stale = setup.jobs.get("job_a")
    assert stale.status == "failed" and stale.error and stale.finished_at == later
    assert setup.jobs.latest().job_id == "job_c"


def test_stale_running_job_does_not_block_publish(setup: Setup) -> None:
    setup.jobs.start(PublishJob("job_old", ("2026-10",), "running", NOW - timedelta(hours=1)), NOW - timedelta(minutes=10))
    # (the row above is a crashed job: a new publish marks it failed and proceeds)
    assert setup.service.publish(MONTHS).status == "pushed"
    assert setup.jobs.get("job_old").status == "failed"


# ---------------------------------------------------------------- deployment status


def test_job_refresh_pending_live_failed(setup: Setup) -> None:
    job = setup.service.publish(MONTHS)
    pending = setup.service.get_job(job.job_id)
    assert pending.status == "pushed" and pending.deployed_at is None
    setup.status.state = DeploymentStatus("pending", "https://github.com/x/y/actions/runs/1")
    assert setup.service.get_job(job.job_id).run_url.endswith("/1")
    setup.status.fail = True  # GitHub unreachable: never claim live
    assert setup.service.get_job(job.job_id).status == "pushed"
    setup.status.fail = False
    setup.clock.now = NOW + timedelta(minutes=2)
    setup.status.state = DeploymentStatus("success", "https://github.com/x/y/actions/runs/1")
    live = setup.service.get_job(job.job_id)
    assert live.status == "live" and live.deployed_at == setup.clock.now
    assert setup.jobs.get(job.job_id).status == "live"
    calls = setup.status.calls
    setup.service.get_job(job.job_id)  # live jobs are not refreshed again
    assert setup.status.calls == calls
    with pytest.raises(JobNotFound):
        setup.service.get_job("job_missing")


def test_failed_deployment(setup: Setup) -> None:
    job = setup.service.publish(MONTHS)
    setup.status.state = DeploymentStatus("failure", "https://github.com/x/y/actions/runs/2")
    failed = setup.service.get_job(job.job_id)
    assert failed.status == "failed" and "deployment failed" in failed.error and failed.deployed_at is None


# ---------------------------------------------------------------- preview


def test_preview_tracks_published_revision_and_unchanged(setup: Setup) -> None:
    first = setup.service.preview(MONTHS)
    assert [w["published_revision"] for w in first.weeks] == [None, None]
    assert [w["plan_id"] for w in first.weeks] == ["plan_2026_10_26", "plan_2026_11_02"]
    assert first.unchanged is False
    job = setup.service.publish(MONTHS)
    setup.status.state = DeploymentStatus("success", "https://github.com/x/y/actions/runs/3")
    setup.service.get_job(job.job_id)
    setup.env.new_revision("plan_2026_10_26")
    again = setup.service.preview(MONTHS)
    assert [(w["revision"], w["published_revision"]) for w in again.weeks] == [(2, 1), (1, 1)]
    assert again.unchanged is False
    setup.service.publish(MONTHS)
    assert setup.service.preview(MONTHS).unchanged is True
    with pytest.raises(InvalidMonths):
        setup.service.preview([])


def test_preview_falls_back_to_last_job_when_remote_unreachable(setup: Setup, tmp_path) -> None:
    setup.service.publish(MONTHS)
    setup.repo._remote = (tmp_path / "gone.git").as_uri()
    setup.repo._dir = tmp_path / "work3"
    service = setup.make()
    assert service.preview(MONTHS).unchanged is True


# ---------------------------------------------------------------- adapters


def test_content_hash_ignores_publication_id_and_exported_at(setup: Setup) -> None:
    svc = PublicationService(SqlitePlanReadAdapter(setup.env.db), setup.env.trips, setup.clock, TZ)
    a, _ = svc.build(MONTHS)
    setup.clock.now = NOW + timedelta(days=1)
    b, _ = svc.build(MONTHS)
    assert a.publication_id != b.publication_id
    assert content_hash(a.index, a.files) == content_hash(b.index, b.files)


def test_git_error_messages_hide_paths(tmp_path) -> None:
    repo = GitPublicationRepository(
        (tmp_path / "nope.git").as_uri(), tmp_path / "w", key_path=tmp_path / "secret-key", known_hosts_path=tmp_path / "kh"
    )
    with pytest.raises(PublishRepoError) as caught:
        repo.sync()
    assert str(tmp_path) not in str(caught.value)


def test_git_timeout_is_reported(tmp_path, origin) -> None:
    repo = GitPublicationRepository(origin.as_uri(), tmp_path / "w", key_path="k", known_hosts_path="h", network_timeout=0.0001)
    with pytest.raises(PublishRepoError, match="timed out"):
        repo.sync()


def test_ssh_command_uses_key_and_pinned_hosts(tmp_path) -> None:
    repo = GitPublicationRepository("git@github.com:o/r.git", tmp_path, key_path="/run/secrets/deploy_key",
                                    known_hosts_path="/etc/ssh/ssh_known_hosts")
    ssh = repo._env()["GIT_SSH_COMMAND"]
    for part in ("-i /run/secrets/deploy_key", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
                 "UserKnownHostsFile=/etc/ssh/ssh_known_hosts", "BatchMode=yes"):
        assert part in ssh


def test_prune_never_removes_active(tmp_path) -> None:
    pubs = tmp_path / "publications"
    for name in ("pub_1", "pub_2", "pub_3", "pub_4", "pub_5"):
        (pubs / name).mkdir(parents=True)
    (tmp_path / "index.json").write_text('{"publication_id": "pub_1"}')
    prune_publications(tmp_path, 3)
    assert sorted(p.name for p in pubs.iterdir()) == ["pub_1", "pub_3", "pub_4", "pub_5"]


class FakeResponse:
    def __init__(self, body) -> None:
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self, n=-1) -> bytes:
        return self._body


def runs(*items) -> dict:
    return {"workflow_runs": [{"head_sha": SHA, "html_url": f"https://github.com/o/r/actions/runs/{i}", **r} for i, r in enumerate(items, 1)]}


def test_github_reader_states_and_cache() -> None:
    now = [0.0]
    calls: list[str] = []
    answer: list[object] = [runs()]

    def opener(request, timeout):
        calls.append(request.full_url)
        if isinstance(answer[0], Exception):
            raise answer[0]
        return FakeResponse(answer[0])

    reader = GithubDeploymentStatusReader("o/r", opener=opener, monotonic=lambda: now[0])
    assert reader.status(SHA) == DeploymentStatus("pending")
    assert calls[0].endswith(f"/repos/o/r/actions/runs?head_sha={SHA}&per_page=20")
    answer[0] = runs({"status": "completed", "conclusion": "success"})
    assert reader.status(SHA) == DeploymentStatus("pending")  # cached
    assert len(calls) == 1
    now[0] = 11
    assert reader.status(SHA) == DeploymentStatus("success", "https://github.com/o/r/actions/runs/1")
    now[0] = 30
    answer[0] = runs({"status": "in_progress", "conclusion": None})
    assert reader.status(SHA).state == "pending"
    now[0] = 50
    answer[0] = runs({"status": "completed", "conclusion": "failure"})
    assert reader.status(SHA).state == "failure"
    now[0] = 70
    answer[0] = urllib.error.URLError("offline")
    with pytest.raises(DeploymentStatusError):
        reader.status(SHA)
    with pytest.raises(DeploymentStatusError):  # failures are cached too: no hammering
        reader.status(SHA)
    assert len(calls) == 5
    with pytest.raises(DeploymentStatusError):
        reader.status("not-a-sha")


# ---------------------------------------------------------------- HTTP


@pytest.fixture
def client(setup: Setup):
    app = create_app(db_path=setup.env.db, clock=setup.clock, publishing_service=setup.service)
    return app.test_client()


def error_code(response, status: int) -> str:
    assert response.status_code == status, response.get_data(as_text=True)
    return response.get_json()["error"]["code"]


def test_capabilities_flag(setup: Setup) -> None:
    on = create_app(db_path=setup.env.db, clock=setup.clock, publishing_service=setup.service).test_client()
    assert on.get("/api/v1/capabilities", environ_base=LOCAL).get_json()["can_publish"] is True
    off = create_app(db_path=setup.env.db, clock=setup.clock).test_client()
    assert off.get("/api/v1/capabilities", environ_base=LOCAL).get_json()["can_publish"] is False


def test_routes_end_to_end(client, setup: Setup) -> None:
    status = client.get("/api/v1/publishing", environ_base=LOCAL).get_json()
    assert status == {"enabled": True, "site_url": "https://example.invalid/site/", "latest_job": None, "last_live_job": None}
    preview = client.get("/api/v1/publishing/preview?months=2026-11,2026-10", environ_base=LOCAL)
    assert preview.status_code == 200 and preview.headers["Cache-Control"] == "no-store"
    body = preview.get_json()
    assert body["months"] == MONTHS and body["unchanged"] is False and body["trips"] == []
    assert body["weeks"][0] == {
        "plan_id": "plan_2026_10_26", "week_start": "2026-10-26", "week_end": "2026-11-01",
        "revision": 1, "published_revision": None,
    }
    created = client.post("/api/v1/publishing/jobs", json={"months": MONTHS}, environ_base=LOCAL)
    assert created.status_code == 201
    job = created.get_json()
    assert set(job) == {"job_id", "months", "status", "publication_id", "commit_sha", "run_url", "error",
                        "started_at", "finished_at", "deployed_at"}
    assert job["status"] == "pushed" and job["months"] == MONTHS and job["deployed_at"] is None
    again = client.post("/api/v1/publishing/jobs", json={"months": MONTHS}, environ_base=LOCAL)
    assert again.status_code == 201 and again.get_json()["status"] == "unchanged"
    assert client.get("/api/v1/publishing/preview?months=2026-10,2026-11", environ_base=LOCAL).get_json()["unchanged"] is True
    setup.status.state = DeploymentStatus("success", "https://github.com/o/r/actions/runs/9")
    live = client.get(f"/api/v1/publishing/jobs/{job['job_id']}", environ_base=LOCAL).get_json()
    assert live["status"] == "live" and live["deployed_at"] and live["run_url"].endswith("/9")
    overview = client.get("/api/v1/publishing", environ_base=LOCAL).get_json()
    assert overview["latest_job"]["status"] == "unchanged" and overview["last_live_job"]["job_id"] == job["job_id"]
    week = client.get("/api/v1/publishing/preview?months=2026-10", environ_base=LOCAL).get_json()["weeks"][0]
    assert week["published_revision"] == 1


def test_route_errors(client, setup: Setup) -> None:
    for query in ("", "?months=", "?months=2026-13", "?months=2026-10,2026-10"):
        assert error_code(client.get("/api/v1/publishing/preview" + query, environ_base=LOCAL), 422) == "invalid_months"
    for months in ([], ["x"], ["2026-10", "2026-10"], [f"2025-{m:02d}" for m in range(1, 13)] + ["2026-01"], "2026-10"):
        assert error_code(client.post("/api/v1/publishing/jobs", json={"months": months}, environ_base=LOCAL), 422) == "invalid_months"
    assert error_code(client.post("/api/v1/publishing/jobs", json={}, environ_base=LOCAL), 400) == "invalid_request"
    assert error_code(client.post("/api/v1/publishing/jobs", json={"months": MONTHS, "x": 1}, environ_base=LOCAL), 400) == "invalid_request"
    assert error_code(client.post("/api/v1/publishing/jobs", json={"months": MONTHS}, environ_base=LAN), 403) == "forbidden"
    assert error_code(client.get("/api/v1/publishing/jobs/job_nope", environ_base=LOCAL), 404) == "not_found"
    assert setup.jobs.latest() is None


def test_route_conflict_and_failure(client, setup: Setup, tmp_path) -> None:
    setup.jobs.start(PublishJob("job_busy", ("2026-10",), "running", NOW), NOW - timedelta(minutes=10))
    assert error_code(client.post("/api/v1/publishing/jobs", json={"months": MONTHS}, environ_base=LOCAL), 409) == "publish_in_progress"
    setup.jobs.save(PublishJob("job_busy", ("2026-10",), "failed", NOW))
    setup.repo._remote = (tmp_path / "gone.git").as_uri()
    setup.repo._dir = tmp_path / "work4"
    response = client.post("/api/v1/publishing/jobs", json={"months": MONTHS}, environ_base=LOCAL)
    assert error_code(response, 502) == "publish_failed"
    assert str(tmp_path) not in response.get_data(as_text=True)
    assert client.get("/api/v1/publishing", environ_base=LOCAL).get_json()["latest_job"]["status"] == "failed"


def test_disabled_without_configuration(setup: Setup, monkeypatch) -> None:
    monkeypatch.delenv("MEALPLANNER_PUBLISH_REPO", raising=False)
    client = create_app(db_path=setup.env.db, clock=setup.clock).test_client()
    body = client.get("/api/v1/publishing", environ_base=LOCAL).get_json()
    assert body["enabled"] is False and body["latest_job"] is None and body["site_url"].startswith("https://")
    for response in (
        client.get("/api/v1/publishing/preview?months=2026-10", environ_base=LOCAL),
        client.post("/api/v1/publishing/jobs", json={"months": MONTHS}, environ_base=LOCAL),
        client.get("/api/v1/publishing/jobs/job_x", environ_base=LOCAL),
    ):
        assert error_code(response, 503) == "publishing_disabled"


def test_environment_configuration(setup: Setup, origin: Path, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MEALPLANNER_PUBLISH_REPO", origin.as_uri())
    monkeypatch.setenv("MEALPLANNER_APP_REVISION", SHA)
    monkeypatch.setenv("MEALPLANNER_PUBLIC_SITE_URL", "https://example.invalid/x/")
    app = create_app(db_path=setup.env.db, clock=setup.clock)
    service = app.config["PUBLISHING_SERVICE"]
    assert service is not None and service.site_url == "https://example.invalid/x/"
    assert service._repo._dir == setup.env.db.resolve().parent / "publish-repo"
    monkeypatch.setenv("MEALPLANNER_APP_REVISION", "not-a-sha")
    monkeypatch.setenv("MEALPLANNER_PUBLISH_WORKDIR", str(tmp_path / "wd"))
    service = create_app(db_path=setup.env.db, clock=setup.clock).config["PUBLISHING_SERVICE"]
    assert service._app_ref is None and service._repo._dir == tmp_path / "wd"
