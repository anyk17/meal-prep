"""Test helpers: a logged-in test client without paying the production scrypt cost per test."""

from __future__ import annotations

from mealplanner.application.auth import PasswordHasher
from mealplanner.domain.auth import UsernameTaken

USERNAME = "tester"
PASSWORD = "correct horse battery"
FAST_HASHER = PasswordHasher(n=2**4)  # same format, trivially cheap; production uses n=2**15


def make_user(app, username: str = USERNAME, password: str = PASSWORD) -> None:
    auth = app.config["AUTH_SERVICE"]
    auth.hasher = FAST_HASHER
    try:
        auth.create_user(username, password)
    except UsernameTaken:
        pass


def login(client, app, username: str = USERNAME, password: str = PASSWORD):
    make_user(app, username, password)
    response = client.post("/api/v1/session", json={"username": username, "password": password})
    assert response.status_code == 200, response.get_data(as_text=True)
    return client


def logged_in_client(app):
    return login(app.test_client(), app)
