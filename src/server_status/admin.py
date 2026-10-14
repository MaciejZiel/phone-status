"""Administrator access: one shared secret, in-memory sessions and a per-visitor login rate limit.

The secret comes from SERVER_STATUS_ADMIN_TOKEN. Without it (or with a value that is too short to be
a real secret) the administrator view does not exist: every admin path answers 404.
The token is only ever compared, never logged, stored in a response or put in a session.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable

log = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 24
SESSION_TTL = 12 * 3600.0
MAX_SESSIONS = 64
LOGIN_ATTEMPTS = 5
LOGIN_WINDOW = 300.0
MAX_TRACKED_CLIENTS = 4096


class RateLimiter:
    """At most `limit` attempts per `window` seconds for each key, with a bounded key table."""

    def __init__(
        self,
        limit: int,
        window: float,
        clock: Callable[[], float],
        max_keys: int = MAX_TRACKED_CLIENTS,
    ) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> float:
        """Record an attempt. Returns 0 when allowed, else the seconds until the next one is."""
        now = self._clock()
        queue = self._hits.get(key)
        if queue is None:
            if len(self._hits) >= self._max_keys:
                self._prune(now)
            queue = self._hits[key] = deque()
        while queue and now - queue[0] >= self._window:
            queue.popleft()
        if len(queue) >= self._limit:
            return max(1.0, self._window - (now - queue[0]))
        queue.append(now)
        return 0.0

    def _prune(self, now: float) -> None:
        for key in [k for k, q in self._hits.items() if not q or now - q[-1] >= self._window]:
            del self._hits[key]
        while len(self._hits) >= self._max_keys:  # still full: drop the oldest entry
            del self._hits[next(iter(self._hits))]


class AdminAuth:
    def __init__(
        self,
        token: str,
        clock: Callable[[], float] = time.monotonic,
        session_ttl: float = SESSION_TTL,
        max_sessions: int = MAX_SESSIONS,
        attempts: int = LOGIN_ATTEMPTS,
        window: float = LOGIN_WINDOW,
    ) -> None:
        self._token = token.encode()
        self._clock = clock
        self._ttl = session_ttl
        self._max_sessions = max_sessions
        self._lock = threading.Lock()
        self._sessions: dict[str, float] = {}  # session id -> expiry
        self._limiter = RateLimiter(attempts, window, clock)

    @classmethod
    def from_token(cls, token: str | None) -> AdminAuth | None:
        """None (admin disabled) when the token is unset, blank or too short to be a secret."""
        token = (token or "").strip()
        if not token:
            return None
        if len(token) < MIN_TOKEN_LENGTH:
            log.error(
                "administrator token is shorter than %d characters; administrator view disabled",
                MIN_TOKEN_LENGTH,
            )
            return None
        return cls(token)

    @property
    def session_ttl(self) -> int:
        return int(self._ttl)

    def login(self, client: str, supplied: str) -> tuple[str, str | float]:
        """('ok', session id), ('denied', 0) or ('limited', seconds to wait)."""
        with self._lock:
            wait = self._limiter.hit(client)
            if wait:
                log.warning("administrator login rate limit hit by %s", client)
                return "limited", wait
        if not hmac.compare_digest(supplied.encode(), self._token):
            log.warning("administrator login failed from %s", client)
            return "denied", 0
        sid = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._sessions = {s: e for s, e in self._sessions.items() if e > now}
            while len(self._sessions) >= self._max_sessions:
                del self._sessions[next(iter(self._sessions))]  # oldest first (insertion order)
            self._sessions[sid] = now + self._ttl
        log.info("administrator logged in from %s", client)
        return "ok", sid

    def valid(self, sid: str | None) -> bool:
        if not sid:
            return False
        with self._lock:
            expiry = self._sessions.get(sid)
            if expiry is None:
                return False
            if expiry <= self._clock():
                del self._sessions[sid]
                return False
            return True

    def logout(self, sid: str | None) -> None:
        if sid:
            with self._lock:
                self._sessions.pop(sid, None)


def client_key(forwarded: str | None, peer: str) -> str:
    """Rate-limit key: CF-Connecting-IP when it is a valid address, else the socket peer."""
    if forwarded:
        try:
            return str(ipaddress.ip_address(forwarded.strip()))
        except ValueError:
            pass
    return peer
