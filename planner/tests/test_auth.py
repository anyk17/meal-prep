"""Household login: hashing, sessions, rate limiting, cross-origin checks, session routes, user CLI."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from auth_helpers import FAST_HASHER, PASSWORD, USERNAME

from mealplanner.adapters.memory import InMemorySessionRepository, InMemoryUserRepository
from mealplanner.adapters.sqlite.auth import SqliteSessionRepository, SqliteUserRepository
from mealplanner.application.auth import (
    ABSOLUTE_TIMEOUT,
    AuthService,
    LoginRateLimiter,
    PasswordHasher,
)
from mealplanner.domain.auth import (
    InvalidCredentials,
    InvalidUsername,
    TooManyAttempts,
    UsernameTaken,
    UserNotFound,
    WeakPassword,
    normalize_username,
)
from mealplanner.entrypoints import db_cli
from mealplanner.entrypoints.db_cli import import_inputs
from mealplanner.entrypoints.http.app import create_app
from mealplanner.entrypoints.http.auth_routes import cross_origin_rejected

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(clock) -> AuthService:
    return AuthService(InMemoryUserRepository(), InMemorySessionRepository(), clock, hasher=FAST_HASHER)


# ---- hashing


def test_hash_format_and_verify() -> None:
    hasher = PasswordHasher()  # production parameters
    stored = hasher.hash("a long enough password")
    scheme, n, r, p, salt, key = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", "32768", "8", "1")
    assert len(__import__("base64").b64decode(salt)) == 16 and len(__import__("base64").b64decode(key)) == 64
    assert hasher.verify("a long enough password", stored)
    assert not hasher.verify("a long enough passworD", stored)
    assert hasher.hash("a long enough password") != stored  # fresh salt


def test_verify_uses_parameters_stored_in_the_hash_and_rejects_garbage() -> None:
    cheap = PasswordHasher(n=2**4).hash("pw")
    assert PasswordHasher().verify("pw", cheap)
    for bad in ("", "x", "scrypt$1$8$1$AA==$AA==", "scrypt$16$8$1$!!$AA==", "bcrypt$16$8$1$AA==$AA==", "scrypt$16$8$1$AA==$"):
        assert PasswordHasher().verify("pw", bad) is False


# ---- users


def test_username_normalization_and_rules(service) -> None:
    assert normalize_username("  ＪＵＡＮ ") == "juan"
    user = service.create_user("  Juan ", PASSWORD)
    assert user.username == "juan"
    with pytest.raises(UsernameTaken):
        service.create_user("JUAN", PASSWORD)
    for bad in ("", "a b", "x" * 65, "a\u0000b"):
        with pytest.raises(InvalidUsername):
            service.create_user(bad, PASSWORD)
    with pytest.raises(WeakPassword):
        service.create_user("ana", "too short")


# ---- login and sessions


def login(service, password=PASSWORD, username=USERNAME, ip="10.0.0.1"):
    return service.login(username, password, ip)


def test_login_and_authenticate(service, clock) -> None:
    service.create_user("Tester", PASSWORD)
    result = login(service, username="TESTER")
    assert result.username == "tester" and result.expires_at == NOW + ABSOLUTE_TIMEOUT
    assert service.authenticate(result.token).username == "tester"
    assert service.authenticate("nonsense") is None and service.authenticate(None) is None


def test_only_the_token_hash_is_stored(service) -> None:
    service.create_user(USERNAME, PASSWORD)
    token = login(service).token
    stored = list(service.sessions._sessions)
    assert len(stored) == 1 and token not in stored[0] and len(stored[0]) == 64


def test_bad_credentials_are_indistinguishable(service) -> None:
    service.create_user(USERNAME, PASSWORD)
    service.create_user("off", PASSWORD)
    service.disable_user("off")
    messages = set()
    for name, pw in ((USERNAME, "wrong password!"), ("nobody", PASSWORD), ("off", PASSWORD)):
        with pytest.raises(InvalidCredentials) as exc:
            login(service, pw, name)
        messages.add(str(exc.value))
    assert len(messages) == 1


def test_idle_and_absolute_expiry(service, clock) -> None:
    service.create_user(USERNAME, PASSWORD)
    idle = login(service).token
    clock.advance(days=29)
    assert service.authenticate(idle)  # refreshes last_seen
    clock.advance(days=29)
    assert service.authenticate(idle)
    clock.advance(days=31)
    assert service.authenticate(idle) is None  # idle past 30 days; row removed
    assert service.sessions.count() == 0

    absolute = login(service).token
    for _ in range(8):  # keep it active every 10 days
        clock.advance(days=10)
        assert service.authenticate(absolute)
    clock.advance(days=11)  # day 91 since login
    assert service.authenticate(absolute) is None


def test_last_seen_is_touched_at_most_hourly(service, clock) -> None:
    service.create_user(USERNAME, PASSWORD)
    token = login(service).token
    digest = next(iter(service.sessions._sessions))
    clock.advance(minutes=59)
    service.authenticate(token)
    assert service.sessions.get(digest).last_seen_at == NOW
    clock.advance(minutes=2)
    service.authenticate(token)
    assert service.sessions.get(digest).last_seen_at == clock.now


def test_logout_deletes_session(service) -> None:
    service.create_user(USERNAME, PASSWORD)
    token = login(service).token
    service.logout(token)
    service.logout(token)  # idempotent
    assert service.authenticate(token) is None


@pytest.mark.parametrize(
    "action",
    [
        lambda svc: svc.set_password(USERNAME, "another good password"),
        lambda svc: svc.disable_user(USERNAME),
        lambda svc: svc.logout_all(USERNAME),
        lambda svc: svc.remove_user(USERNAME),
    ],
)
def test_password_change_disable_logout_all_and_remove_end_sessions(service, action) -> None:
    service.create_user(USERNAME, PASSWORD)
    first, second = login(service).token, login(service).token
    action(service)
    assert service.authenticate(first) is None and service.authenticate(second) is None


def test_unknown_user_management_errors(service) -> None:
    with pytest.raises(UserNotFound):
        service.set_password("ghost", PASSWORD)


def test_disabled_user_session_is_rejected_and_enable_restores_login(service) -> None:
    service.create_user(USERNAME, PASSWORD)
    token = login(service).token
    service.users.set_disabled(service.users.get_by_username(USERNAME).user_id, True)  # bypass session cleanup
    assert service.authenticate(token) is None
    service.enable_user(USERNAME)
    assert login(service).token


def test_remove_user(service) -> None:
    service.create_user(USERNAME, PASSWORD)
    token = login(service).token
    service.remove_user(USERNAME)
    assert service.authenticate(token) is None and service.list_users() == []


def test_new_login_prunes_expired_sessions(service, clock) -> None:
    service.create_user(USERNAME, PASSWORD)
    login(service)
    clock.advance(days=100)
    login(service)
    assert service.sessions.count() == 1


# ---- rate limiting


def test_five_failures_lock_username_then_retry_after(service, clock) -> None:
    service.create_user(USERNAME, PASSWORD)
    for i in range(5):
        with pytest.raises(InvalidCredentials):
            login(service, "wrong password!", ip=f"10.0.0.{i}")  # different addresses: username counter trips
    with pytest.raises(TooManyAttempts) as exc:
        login(service, PASSWORD, ip="10.0.9.9")  # even the right password
    assert 1 <= exc.value.retry_after <= 15 * 60
    clock.advance(minutes=15, seconds=1)
    assert login(service, PASSWORD, ip="10.0.9.9").token


def test_five_failures_lock_the_address_across_usernames(service) -> None:
    for i in range(5):
        with pytest.raises(InvalidCredentials):
            login(service, "x" * 12, username=f"ghost{i}", ip="10.1.1.1")
    with pytest.raises(TooManyAttempts):
        login(service, "x" * 12, username="another", ip="10.1.1.1")
    service.create_user(USERNAME, PASSWORD)
    assert login(service, ip="10.2.2.2").token  # other address unaffected


def test_success_clears_username_counter(service) -> None:
    service.create_user(USERNAME, PASSWORD)
    for i in range(4):
        with pytest.raises(InvalidCredentials):
            login(service, "wrong password!", ip=f"10.0.1.{i}")
    login(service, ip="10.0.2.1")
    for i in range(4):
        with pytest.raises(InvalidCredentials):
            login(service, "wrong password!", ip=f"10.0.3.{i}")  # the 4 earlier failures no longer count for the username


def test_limiter_memory_is_bounded(clock) -> None:
    limiter = LoginRateLimiter(clock, max_keys=50)
    for i in range(500):
        limiter.record_failure(f"user{i}", f"ip{i}")
    assert len(limiter._failures) <= 50


# ---- cross-origin


@pytest.mark.parametrize(
    "headers,rejected",
    [
        ({}, False),
        ({"Sec-Fetch-Site": "same-origin"}, False),
        ({"Sec-Fetch-Site": "none"}, False),
        ({"Sec-Fetch-Site": "cross-site"}, True),
        ({"Sec-Fetch-Site": "same-site"}, True),
        ({"Origin": "http://localhost:8080"}, False),
        ({"Origin": "http://evil.example"}, True),
        ({"Origin": "null"}, True),
        ({"Sec-Fetch-Site": "same-origin", "Origin": "http://evil.example"}, False),  # Sec-Fetch-Site decides
        ({"Sec-Fetch-Site": "cross-site", "Origin": "http://localhost:8080"}, True),
    ],
)
def test_cross_origin_rule(headers, rejected) -> None:
    assert cross_origin_rejected(headers, "http://localhost:8080") is rejected


# ---- HTTP session routes


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "plans.db"
    import_inputs(path, ROOT / "data")
    return path


@pytest.fixture
def app(db_path, clock):
    app = create_app(db_path=db_path, clock=clock)
    app.config["AUTH_SERVICE"].hasher = FAST_HASHER
    app.config["AUTH_SERVICE"].create_user(USERNAME, PASSWORD)
    return app


def creds(**kw):
    return {"username": USERNAME, "password": PASSWORD, **kw}


def code(response):
    return response.get_json()["error"]["code"]


def test_session_lifecycle(app) -> None:
    client = app.test_client()
    assert client.get("/api/v1/session").status_code == 401
    assert code(client.get("/api/v1/session")) == "unauthenticated"
    login = client.post("/api/v1/session", json=creds(username="  TESTER "))
    assert login.status_code == 200 and login.get_json() == {"username": "tester"}
    assert client.get("/api/v1/session").get_json() == {"username": "tester"}
    assert client.get("/api/v1/session").headers["Cache-Control"] == "no-store"
    out = client.delete("/api/v1/session")
    assert out.status_code == 204 and out.get_data() == b""
    assert client.get("/api/v1/session").status_code == 401
    assert client.delete("/api/v1/session").status_code == 204  # idempotent, no session


def test_cookie_attributes(app) -> None:
    response = app.test_client().post("/api/v1/session", json=creds())
    cookie = response.headers["Set-Cookie"]
    assert cookie.startswith("mp_session=")
    for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
        assert attr in cookie
    assert f"Max-Age={int(ABSOLUTE_TIMEOUT.total_seconds())}" in cookie
    cleared = app.test_client().delete("/api/v1/session").headers["Set-Cookie"]
    assert "mp_session=;" in cleared and "Max-Age=0" in cleared and "Path=/" in cleared


def test_cookie_secure_switch(db_path, monkeypatch) -> None:
    monkeypatch.setenv("MEALPLANNER_COOKIE_SECURE", "0")
    app = create_app(db_path=db_path)
    app.config["AUTH_SERVICE"].hasher = FAST_HASHER
    app.config["AUTH_SERVICE"].create_user(USERNAME, PASSWORD)
    assert "Secure" not in app.test_client().post("/api/v1/session", json=creds()).headers["Set-Cookie"]


def test_bad_credentials_and_bodies(app) -> None:
    client = app.test_client()
    for body in (creds(password="wrong password!"), creds(username="nobody")):
        response = client.post("/api/v1/session", json=body)
        assert response.status_code == 401 and code(response) == "invalid_credentials"
        assert "Set-Cookie" not in response.headers
    for body in ({}, {"username": "a"}, creds(extra=1), creds(password=""), creds(username=5), [1]):
        assert client.post("/api/v1/session", json=body).status_code == 422
    assert client.post("/api/v1/session", data="x", content_type="text/plain").status_code == 422
    assert client.post("/api/v1/session", data="{", content_type="application/json").status_code == 422


def test_login_rate_limit_http(app, clock) -> None:
    client = app.test_client()
    for _ in range(5):
        assert client.post("/api/v1/session", json=creds(password="wrong password!")).status_code == 401
    blocked = client.post("/api/v1/session", json=creds())
    assert blocked.status_code == 429 and code(blocked) == "too_many_attempts"
    assert int(blocked.headers["Retry-After"]) >= 1
    clock.advance(minutes=16)
    assert client.post("/api/v1/session", json=creds()).status_code == 200


def test_reads_open_changes_and_publishing_need_session(app) -> None:
    anonymous = app.test_client()
    assert anonymous.get("/api/v1/plans?from=2026-10-01&to=2026-10-31").status_code == 200
    assert anonymous.get("/api/v1/shopping-trips").status_code == 200
    assert anonymous.get("/api/v1/nope").status_code == 404
    for method, url in (
        ("post", "/api/v1/plans"), ("put", "/api/v1/plans"), ("patch", "/api/v1/plans"), ("delete", "/api/v1/plans/x"),
        ("get", "/api/v1/publishing"), ("get", "/api/v1/publishing/preview"), ("get", "/api/v1/publishing/jobs/j"),
        ("post", "/api/v1/nope"),
    ):
        response = getattr(anonymous, method)(url, json={})
        assert response.status_code == 401 and code(response) == "unauthenticated", (method, url)


def test_expired_session_cookie_is_refused(app, clock) -> None:
    client = app.test_client()
    client.post("/api/v1/session", json=creds())
    clock.advance(days=91)
    assert client.get("/api/v1/session").status_code == 401
    assert client.post("/api/v1/plans", json={}).status_code == 401


def test_password_change_logs_the_browser_out(app) -> None:
    client = app.test_client()
    client.post("/api/v1/session", json=creds())
    app.config["AUTH_SERVICE"].set_password(USERNAME, "a brand new password")
    assert client.get("/api/v1/session").status_code == 401


def test_cross_origin_rejected_on_changes_and_login(app) -> None:
    client = app.test_client()
    client.post("/api/v1/session", json=creds())
    for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://evil.example"}):
        for method, url in (("post", "/api/v1/session"), ("post", "/api/v1/plans"), ("delete", "/api/v1/session")):
            response = getattr(client, method)(url, json=creds(), headers=headers)
            assert response.status_code == 403 and code(response) == "cross_origin_rejected"
    # same-origin browser requests pass; reads are never judged
    ok = client.post("/api/v1/session", json=creds(), headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://localhost"})
    assert ok.status_code == 200
    assert client.get("/api/v1/plans?from=2026-10-01&to=2026-10-31", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
    assert client.get("/api/v1/session", headers={"Origin": "http://evil.example"}).status_code == 200


def test_trusted_proxies_use_forwarded_headers(db_path) -> None:
    app = create_app(db_path=db_path, trusted_proxies=1)
    client = app.test_client()
    forwarded = {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "meals.example", "Origin": "https://meals.example"}
    assert client.post("/api/v1/session", json=creds(), headers=forwarded).status_code == 401  # origin accepted, no user
    plain = create_app(db_path=db_path).test_client()  # not trusted: the forwarded host is ignored
    assert plain.post("/api/v1/session", json=creds(), headers=forwarded).status_code == 403


def test_capabilities_reflect_session(app) -> None:
    client = app.test_client()
    assert client.get("/api/v1/capabilities").get_json()["authenticated"] is False
    client.post("/api/v1/session", json=creds())
    body = client.get("/api/v1/capabilities").get_json()
    assert body["authenticated"] is True and body["username"] == "tester" and body["can_edit_plans"] is True


def test_no_loopback_or_trust_switch_remains() -> None:
    import mealplanner.entrypoints.http.app as app_module

    assert not hasattr(app_module, "_is_loopback_request")
    assert "trust_network_peers" not in create_app.__code__.co_varnames


# ---- SQLite adapters and migration


def test_sqlite_repositories_roundtrip_and_cascade(tmp_path, clock) -> None:
    db = tmp_path / "a.db"
    service = AuthService(SqliteUserRepository(db), SqliteSessionRepository(db), clock, hasher=FAST_HASHER)
    service.create_user("Ana", PASSWORD)
    with pytest.raises(UsernameTaken):
        service.create_user("ana", PASSWORD)
    token = service.login("ana", PASSWORD, "1.1.1.1").token
    assert service.authenticate(token).username == "ana"
    clock.advance(hours=2)
    service.authenticate(token)
    assert service.sessions.get(next(iter(_hashes(db)))).last_seen_at == clock.now
    service.remove_user("ana")
    assert service.authenticate(token) is None and list(_hashes(db)) == []


def _hashes(db):
    import sqlite3

    with sqlite3.connect(db) as conn:
        return [r[0] for r in conn.execute("SELECT session_hash FROM sessions")]


def test_migration_5_applies_to_a_version_4_database(tmp_path) -> None:
    import sqlite3

    from mealplanner.adapters.sqlite import migrations

    path = tmp_path / "old.db"
    conn = migrations.connect(path)
    for version, name, statements in migrations.MIGRATIONS[:4]:
        with migrations.transaction(conn):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            for statement in statements:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version VALUES (?, ?, 'x')", (version, name))
    assert migrations.migrate(conn) == [5, 6, 7]
    conn.close()
    with sqlite3.connect(path) as check:
        assert {"users", "sessions"} <= {r[0] for r in check.execute("SELECT name FROM sqlite_master")}


# ---- CLI


@pytest.fixture
def cli_db(tmp_path) -> Path:
    path = tmp_path / "cli.db"
    import_inputs(path, ROOT / "data")
    return path


def run(cli_db, *argv, stdin: str | None = None, monkeypatch=None) -> int:
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return db_cli.main([argv[0], argv[1], *argv[2:], "--db", str(cli_db)] if argv[0] == "user" else list(argv))


def test_cli_user_management(cli_db, monkeypatch, capsys) -> None:
    password = "first long password"
    assert run(cli_db, "user", "add", "Juan", "--password-stdin", stdin=password + "\n", monkeypatch=monkeypatch) == 0
    assert run(cli_db, "user", "add", "juan", "--password-stdin", stdin=password + "\n", monkeypatch=monkeypatch) == 1
    assert run(cli_db, "user", "add", "ana", "--password-stdin", stdin="short\n", monkeypatch=monkeypatch) == 1
    assert run(cli_db, "user", "list") == 0
    assert "juan\tactive" in capsys.readouterr().out
    service = db_cli.auth_service(cli_db)
    token = service.login("juan", password, "1.1.1.1").token
    assert run(cli_db, "user", "passwd", "juan", "--password-stdin", stdin="second long password\n", monkeypatch=monkeypatch) == 0
    assert service.authenticate(token) is None
    token = service.login("juan", "second long password", "1.1.1.1").token
    assert run(cli_db, "user", "logout-all", "juan") == 0 and service.authenticate(token) is None
    assert run(cli_db, "user", "disable", "juan") == 0
    with pytest.raises(InvalidCredentials):
        service.login("juan", "second long password", "1.1.1.1")
    assert run(cli_db, "user", "enable", "juan") == 0
    assert service.login("juan", "second long password", "1.1.1.1").token
    assert run(cli_db, "user", "remove", "juan") == 0
    assert run(cli_db, "user", "remove", "juan") == 1  # no such user
    assert service.list_users() == []


def test_cli_never_takes_a_password_argument(cli_db) -> None:
    with pytest.raises(SystemExit):
        db_cli.main(["user", "add", "juan", "--password", "secret-value-123", "--db", str(cli_db)])
    with pytest.raises(SystemExit):
        db_cli.main(["user", "add", "juan", "secret-value-123", "--db", str(cli_db)])


def test_cli_prompts_twice_with_getpass(cli_db, monkeypatch) -> None:
    answers = iter(["interactive password", "interactive password"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert run(cli_db, "user", "add", "ana") == 0
    answers = iter(["one password long", "different password"])
    assert run(cli_db, "user", "passwd", "ana") == 1


def test_cli_needs_an_existing_database(tmp_path) -> None:
    assert db_cli.main(["user", "list", "--db", str(tmp_path / "missing.db")]) == 1
