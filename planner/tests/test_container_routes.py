"""Health check, same-origin frontend serving and the container's network-trust switch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mealplanner.entrypoints.http.app import create_app
from tests.test_read_api import source_repository

REMOTE = {"REMOTE_ADDR": "172.18.0.1"}


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>shell</html>")
    (tmp_path / "assets" / "app-abc123.js").write_text("console.log(1)")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    return tmp_path


def make_client(**kwargs):
    return create_app(source_repository(), **kwargs).test_client()


def test_healthz_needs_no_database_and_no_static_dir() -> None:
    response = make_client().get("/healthz", environ_base=REMOTE)
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    assert response.headers["Cache-Control"] == "no-store"


def test_no_static_serving_by_default() -> None:
    client = make_client()
    assert client.get("/meal-prep/").status_code == 404
    assert client.get("/").status_code == 404


def test_static_dir_from_environment(dist: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEALPLANNER_STATIC_DIR", str(dist))
    assert make_client().get("/meal-prep/").status_code == 200


def test_spa_shell_assets_and_fallback(dist: Path) -> None:
    client = make_client(static_dir=dist)
    root = client.get("/")
    assert root.status_code == 302 and root.headers["Location"].endswith("/meal-prep/")
    shell = client.get("/meal-prep/")
    assert b"shell" in shell.data and shell.headers["Cache-Control"] == "no-cache"
    asset = client.get("/meal-prep/assets/app-abc123.js")
    assert asset.status_code == 200
    assert asset.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert client.get("/meal-prep/favicon.svg").headers["Cache-Control"] == "no-cache"
    # Client-side route: shell; missing file with an extension: 404.
    assert b"shell" in client.get("/meal-prep/week/2026-10-19").data
    assert client.get("/meal-prep/assets/missing.js").status_code == 404


def test_static_serving_cannot_escape_the_directory(dist: Path) -> None:
    (dist.parent / "secret.txt").write_text("secret")
    client = make_client(static_dir=dist)
    for path in ("/meal-prep/../secret.txt", "/meal-prep/%2e%2e/secret.txt"):
        response = client.get(path)
        assert b"secret" not in response.data


def test_api_still_served_next_to_static(dist: Path) -> None:
    client = make_client(static_dir=dist, trust_network_peers=True)
    assert client.get("/api/v1/plans/plan_2026_10_19", environ_base=REMOTE).status_code == 200
    assert client.get("/api/v1/nope", environ_base=REMOTE).status_code == 404


def test_static_dir_without_index_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="index.html"):
        make_client(static_dir=tmp_path)


def test_remote_peers_rejected_unless_network_is_trusted() -> None:
    url = "/api/v1/plans/plan_2026_10_19"
    assert make_client().get(url, environ_base=REMOTE).status_code == 403
    trusted = make_client(trust_network_peers=True)
    assert trusted.get(url, environ_base=REMOTE).status_code == 200


def test_trust_switch_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEALPLANNER_TRUST_NETWORK_PEERS", "1")
    response = make_client().get("/api/v1/plans/plan_2026_10_19", environ_base=REMOTE)
    assert response.status_code == 200
