"""Household login: the session resource, the request guard, and cross-origin protection.

Wiring only; every rule lives in application/auth.py. Viewing (GET) is open; every state-changing
request and the whole publishing area need a session.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...domain.auth import InvalidCredentials, TooManyAttempts

SESSION_COOKIE = "mp_session"
SESSION_PATH = "/api/v1/session"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_FIELD_LENGTH = 1024


def cross_origin_rejected(headers: Any, request_origin: str) -> bool:
    """True for a browser request that did not come from this site (state-changing requests only).

    Sec-Fetch-Site decides when present; otherwise an Origin header must equal the request's own
    scheme+host. Requests with neither header (curl, tests) are allowed.
    """
    site = headers.get("Sec-Fetch-Site")
    if site is not None:
        return site.strip().lower() not in ("same-origin", "none")
    origin = headers.get("Origin")
    if origin is not None:
        return origin.strip().rstrip("/").lower() != request_origin.lower()
    return False


def register_auth(app: Any, error: Callable[..., Any], *, cookie_secure: bool) -> None:
    from flask import g, jsonify, make_response, request

    def service():
        return app.config["AUTH_SERVICE"]

    def set_cookie(response, token: str, max_age: int) -> None:
        response.set_cookie(
            SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="Lax", secure=cookie_secure, path="/"
        )

    @app.before_request
    def api_guard():
        if not request.path.startswith("/api/"):
            return None
        unsafe = request.method not in SAFE_METHODS
        if unsafe and cross_origin_rejected(request.headers, f"{request.scheme}://{request.host}"):
            return error(403, "cross_origin_rejected", "This request did not come from this site.")
        g.auth = service().authenticate(request.cookies.get(SESSION_COOKIE))
        if request.path == SESSION_PATH:
            return None  # the session routes answer for themselves
        publishing = request.path == "/api/v1/publishing" or request.path.startswith("/api/v1/publishing/")
        if (unsafe or publishing) and g.auth is None:
            return error(401, "unauthenticated", "Log in to continue.")
        return None

    @app.get(SESSION_PATH)
    def get_session():
        if g.auth is None:
            return error(401, "unauthenticated", "Log in to continue.")
        return jsonify({"username": g.auth.username})

    @app.post(SESSION_PATH)
    def login():
        body = request.get_json(silent=True)
        details = []
        if not isinstance(body, dict):
            details.append({"field": "body", "message": "request body must be a JSON object (Content-Type: application/json)"})
        else:
            for name in ("username", "password"):
                value = body.get(name)
                if not isinstance(value, str) or not value or len(value) > MAX_FIELD_LENGTH:
                    details.append({"field": name, "message": f"{name} must be a non-empty string"})
            for name in sorted(set(body) - {"username", "password"}):
                details.append({"field": name, "message": f"unknown field {name!r}"})
        if details:
            return error(422, "invalid_request", "The login request is invalid.", details)
        try:
            result = service().login(body["username"], body["password"], request.remote_addr or "unknown")
        except TooManyAttempts as exc:
            response = error(429, "too_many_attempts", "Too many failed attempts. Try again later.")
            response.headers["Retry-After"] = str(exc.retry_after)
            return response
        except InvalidCredentials:
            return error(401, "invalid_credentials", "Incorrect username or password.")
        service().logout(request.cookies.get(SESSION_COOKIE))  # drop a session this browser held before
        response = jsonify({"username": result.username})
        remaining = int((result.expires_at - service().clock()).total_seconds())
        set_cookie(response, result.token, max(remaining, 1))
        return response

    @app.delete(SESSION_PATH)
    def logout():
        service().logout(request.cookies.get(SESSION_COOKIE))
        response = make_response("", 204)
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="Lax", secure=cookie_secure)
        return response
