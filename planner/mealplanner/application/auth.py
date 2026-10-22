"""Household login: password hashing, sessions, login rate limiting, and user management.

Framework-free. Passwords use scrypt in a self-describing string
``scrypt$n$r$p$salt_b64$hash_b64``; session tokens are random and only their SHA-256 is stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import secrets
import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..domain.auth import (
    InvalidCredentials,
    SessionRecord,
    TooManyAttempts,
    User,
    UserNotFound,
    WeakPassword,
    normalize_username,
    validate_username,
)
from .ports import SessionRepository, UserRepository

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 1024
IDLE_TIMEOUT = timedelta(days=30)
ABSOLUTE_TIMEOUT = timedelta(days=90)
LAST_SEEN_INTERVAL = timedelta(hours=1)
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_MAX_FAILURES = 5
LIMITER_MAX_KEYS = 10_000

_MAX_N = 2**20  # bounds for parameters read back from storage


class PasswordHasher:
    """scrypt with per-hash salt; parameters travel inside the stored string."""

    def __init__(self, n: int = 2**15, r: int = 8, p: int = 1, salt_bytes: int = 16, key_bytes: int = 64) -> None:
        self.n, self.r, self.p, self.salt_bytes, self.key_bytes = n, r, p, salt_bytes, key_bytes

    @staticmethod
    def _maxmem(n: int, r: int, p: int) -> int:
        return 2 * 128 * r * (n + p + 2) + 2**20

    @classmethod
    def _derive(cls, password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=length, maxmem=cls._maxmem(n, r, p)
        )

    def hash(self, password: str) -> str:
        salt = secrets.token_bytes(self.salt_bytes)
        key = self._derive(password, salt, self.n, self.r, self.p, self.key_bytes)
        b64 = lambda raw: base64.b64encode(raw).decode("ascii")
        return f"scrypt${self.n}${self.r}${self.p}${b64(salt)}${b64(key)}"

    def verify(self, password: str, stored: str) -> bool:
        try:
            scheme, n_s, r_s, p_s, salt_s, key_s = stored.split("$")
            n, r, p = int(n_s), int(r_s), int(p_s)
            salt, expected = base64.b64decode(salt_s, validate=True), base64.b64decode(key_s, validate=True)
            if scheme != "scrypt" or not (2 <= n <= _MAX_N and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16):
                return False
            if not expected:
                return False
            actual = self._derive(password, salt, n, r, p, len(expected))
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(actual, expected)


class LoginRateLimiter:
    """Failed-login limiter keyed by username and by client address (in-process, thread-safe, bounded)."""

    def __init__(
        self,
        clock: Callable[[], datetime],
        *,
        max_failures: int = LOGIN_MAX_FAILURES,
        window: timedelta = LOGIN_WINDOW,
        max_keys: int = LIMITER_MAX_KEYS,
    ) -> None:
        self._clock = clock
        self._max_failures = max_failures
        self._window = window
        self._max_keys = max_keys
        self._failures: OrderedDict[tuple[str, str], deque[datetime]] = OrderedDict()
        self._lock = threading.Lock()

    def _live(self, key: tuple[str, str], now: datetime) -> deque[datetime] | None:
        entries = self._failures.get(key)
        if entries is None:
            return None
        while entries and now - entries[0] >= self._window:
            entries.popleft()
        if not entries:
            del self._failures[key]
            return None
        return entries

    def retry_after(self, username: str, address: str) -> int:
        """Seconds until a login attempt is allowed again; 0 when it is allowed now."""
        now = self._clock()
        wait = 0.0
        with self._lock:
            for key in (("user", username), ("ip", address)):
                entries = self._live(key, now)
                if entries is not None and len(entries) >= self._max_failures:
                    wait = max(wait, (entries[0] + self._window - now).total_seconds())
        return math.ceil(wait) if wait > 0 else 0

    def record_failure(self, username: str, address: str) -> None:
        now = self._clock()
        with self._lock:
            for key in (("user", username), ("ip", address)):
                entries = self._live(key, now)
                if entries is None:
                    entries = self._failures[key] = deque()
                self._failures.move_to_end(key)
                entries.append(now)
                if len(entries) > self._max_failures:
                    entries.popleft()
            while len(self._failures) > self._max_keys:
                self._failures.popitem(last=False)  # least recently failing key

    def clear_user(self, username: str) -> None:
        with self._lock:
            self._failures.pop(("user", username), None)


@dataclass(frozen=True)
class Authenticated:
    username: str
    expires_at: datetime


@dataclass(frozen=True)
class LoginResult:
    token: str
    username: str
    expires_at: datetime


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(
        self,
        users: UserRepository,
        sessions: SessionRepository,
        clock: Callable[[], datetime],
        *,
        hasher: PasswordHasher | None = None,
        limiter: LoginRateLimiter | None = None,
    ) -> None:
        self.users = users
        self.sessions = sessions
        self.clock = clock
        self.hasher = hasher or PasswordHasher()
        self.limiter = limiter or LoginRateLimiter(clock)
        self._dummy_hash: str | None = None

    # ---------------------------------------------------------------- login / sessions

    def login(self, username: str, password: str, client_address: str) -> LoginResult:
        """Raises TooManyAttempts or InvalidCredentials (the same error for every bad-credential cause)."""
        key = normalize_username(username)
        wait = self.limiter.retry_after(key, client_address)
        if wait:
            raise TooManyAttempts(wait)
        user = self.users.get_by_username(key)
        if user is None:
            # Spend the same time as a real check so timing does not reveal which usernames exist.
            if self._dummy_hash is None:
                self._dummy_hash = self.hasher.hash(secrets.token_urlsafe(16))
            self.hasher.verify(password, self._dummy_hash)
            valid = False
        else:
            valid = self.hasher.verify(password, user.password_hash) and not user.disabled
        if not valid or user is None:
            self.limiter.record_failure(key, client_address)
            raise InvalidCredentials("invalid username or password")
        self.limiter.clear_user(key)
        now = self.clock()
        self.sessions.delete_expired(now, now - IDLE_TIMEOUT)
        token = secrets.token_urlsafe(32)
        record = SessionRecord(_token_hash(token), user.user_id, now, now, now + ABSOLUTE_TIMEOUT)
        self.sessions.add(record)
        return LoginResult(token, user.username, record.expires_at)

    def authenticate(self, token: str | None) -> Authenticated | None:
        """The logged-in user for a session token, refreshing last_seen at most once per hour."""
        if not token:
            return None
        digest = _token_hash(token)
        record = self.sessions.get(digest)
        if record is None:
            return None
        now = self.clock()
        if now >= record.expires_at or now - record.last_seen_at > IDLE_TIMEOUT:
            self.sessions.delete(digest)
            return None
        user = self.users.get_by_id(record.user_id)
        if user is None or user.disabled:
            self.sessions.delete(digest)
            return None
        if now - record.last_seen_at >= LAST_SEEN_INTERVAL:
            self.sessions.touch(digest, now)
        return Authenticated(user.username, record.expires_at)

    def logout(self, token: str | None) -> None:
        if token:
            self.sessions.delete(_token_hash(token))

    # ---------------------------------------------------------------- user management (CLI)

    @staticmethod
    def _check_password(password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise WeakPassword(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        if len(password) > MAX_PASSWORD_LENGTH:
            raise WeakPassword(f"password must be at most {MAX_PASSWORD_LENGTH} characters")

    def _user(self, username: str) -> User:
        user = self.users.get_by_username(normalize_username(username))
        if user is None:
            raise UserNotFound(f"no such user: {normalize_username(username)}")
        return user

    def create_user(self, username: str, password: str) -> User:
        name = validate_username(normalize_username(username))
        self._check_password(password)
        user = User(f"usr_{secrets.token_hex(8)}", name, self.hasher.hash(password), self.clock(), False)
        self.users.add(user)  # UsernameTaken
        return user

    def set_password(self, username: str, password: str) -> None:
        user = self._user(username)
        self._check_password(password)
        self.users.set_password_hash(user.user_id, self.hasher.hash(password))
        self.sessions.delete_for_user(user.user_id)

    def disable_user(self, username: str) -> None:
        user = self._user(username)
        self.users.set_disabled(user.user_id, True)
        self.sessions.delete_for_user(user.user_id)

    def enable_user(self, username: str) -> None:
        self.users.set_disabled(self._user(username).user_id, False)

    def logout_all(self, username: str) -> None:
        self.sessions.delete_for_user(self._user(username).user_id)

    def remove_user(self, username: str) -> None:
        user = self._user(username)
        self.sessions.delete_for_user(user.user_id)
        self.users.delete(user.user_id)

    def list_users(self) -> list[User]:
        return self.users.list()
