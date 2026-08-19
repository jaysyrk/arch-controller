"""Token login, in-memory sessions and brute-force throttling."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field

COOKIE_NAME = "archctl_session"
# A custom header the browser can only send same-origin (it forces a preflight),
# so a cookie-authenticated session cannot be driven by a cross-site form post.
CSRF_HEADER = "x-archctl"

MAX_FAILURES = 5
LOCKOUT_BASE = 5.0  # seconds, doubled per failure past the threshold
LOCKOUT_MAX = 900.0


@dataclass
class _Attempts:
    count: int = 0
    blocked_until: float = 0.0


@dataclass
class Auth:
    token: str
    session_ttl: float = 720 * 3600
    _sessions: dict[str, float] = field(default_factory=dict)
    _failures: dict[str, _Attempts] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- token check -------------------------------------------------
    def check_token(self, candidate: str) -> bool:
        if not self.token or not candidate:
            return False
        return hmac.compare_digest(self.token, candidate)

    # -- throttling --------------------------------------------------
    def retry_after(self, client: str, now: float | None = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            state = self._failures.get(client)
            if state and state.blocked_until > now:
                return state.blocked_until - now
        return 0.0

    def record_failure(self, client: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            state = self._failures.setdefault(client, _Attempts())
            state.count += 1
            if state.count >= MAX_FAILURES:
                over = state.count - MAX_FAILURES
                delay = min(LOCKOUT_BASE * (2**over), LOCKOUT_MAX)
                state.blocked_until = now + delay

    def record_success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

    # -- sessions ----------------------------------------------------
    def create_session(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[sid] = now + self.session_ttl
            self._prune(now)
        return sid

    def valid_session(self, sid: str, now: float | None = None) -> bool:
        if not sid:
            return False
        now = time.time() if now is None else now
        with self._lock:
            expiry = self._sessions.get(sid)
            if expiry is None:
                return False
            if expiry <= now:
                del self._sessions[sid]
                return False
        return True

    def drop_session(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def _prune(self, now: float) -> None:
        for sid, expiry in list(self._sessions.items()):
            if expiry <= now:
                del self._sessions[sid]


@dataclass
class PasswordCache:
    """Holds a sudo password in memory only, for a bounded window.

    Nothing is written to disk and the value is dropped on logout, on expiry,
    and whenever the process restarts.
    """

    ttl: float = 300.0
    _value: str | None = None
    _expiry: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, now: float | None = None) -> str | None:
        if self.ttl <= 0:
            return None
        now = time.time() if now is None else now
        with self._lock:
            if self._value is not None and self._expiry > now:
                return self._value
            self._value = None
        return None

    def store(self, password: str, now: float | None = None) -> None:
        if self.ttl <= 0 or not password:
            return
        now = time.time() if now is None else now
        with self._lock:
            self._value = password
            self._expiry = now + self.ttl

    def clear(self) -> None:
        with self._lock:
            self._value = None
            self._expiry = 0.0
