"""Household login: users, sessions, and the rules for usernames (pure; no I/O)."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime

MAX_USERNAME_LENGTH = 64


class AuthError(Exception):
    """Base class for login and user-management failures."""


class InvalidCredentials(AuthError):
    """Unknown user, wrong password, or disabled account (deliberately indistinguishable)."""


class TooManyAttempts(AuthError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("too many failed login attempts")
        self.retry_after = retry_after


class UsernameTaken(AuthError):
    pass


class UserNotFound(AuthError):
    pass


class InvalidUsername(AuthError):
    pass


class WeakPassword(AuthError):
    pass


@dataclass(frozen=True)
class User:
    user_id: str
    username: str  # normalized
    password_hash: str
    created_at: datetime
    disabled: bool = False


@dataclass(frozen=True)
class SessionRecord:
    session_hash: str  # SHA-256 of the cookie token; the token itself is never stored
    user_id: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime  # absolute lifetime


def normalize_username(value: str) -> str:
    """NFKC, case-folded to lower, surrounding whitespace removed. Used for storage and rate limiting."""
    return unicodedata.normalize("NFKC", value).strip().lower()


def validate_username(normalized: str) -> str:
    if not 1 <= len(normalized) <= MAX_USERNAME_LENGTH:
        raise InvalidUsername(f"username must be 1 to {MAX_USERNAME_LENGTH} characters")
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in normalized):
        raise InvalidUsername("username must not contain spaces or control characters")
    return normalized
