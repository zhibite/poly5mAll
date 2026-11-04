"""In-memory login sessions for the Web backend.

A single-process bot does not need a database for sessions.  The token is
an opaque ``secrets.token_urlsafe`` value in an HttpOnly cookie; the server
keeps ``token -> username/expiry`` in memory.  A process restart invalidates
all sessions and forces a fresh login — acceptable for an operator UI.

If ``WEB_PASSWORD`` is empty, authentication is disabled and pages are open
(the previous behaviour).  Set a password in Coolify / .env for any public
deployment.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

SESSION_COOKIE = "poly5m_session"


@dataclass(frozen=True)
class Session:
    username: str
    created_at: float
    expires_at: float

    @property
    def expires_in(self) -> int:
        return max(0, int(self.expires_at - time.time()))


class SessionManager:
    def __init__(self, username: str, password: str, ttl_s: int) -> None:
        self.username = str(username or "admin")
        self.password = str(password or "")
        self.ttl_s = max(60, int(ttl_s or 604800))
        self._sessions: dict[str, Session] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def login(self, username: str, password: str) -> str | None:
        """Return a new session token, or None on bad credentials."""
        if not self.enabled:
            return ""
        user_ok = secrets.compare_digest(
            str(username or "").encode("utf-8"),
            self.username.encode("utf-8"),
        )
        pass_ok = secrets.compare_digest(
            str(password or "").encode("utf-8"),
            self.password.encode("utf-8"),
        )
        if not (user_ok and pass_ok):
            return None
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._sessions[token] = Session(
            username=self.username,
            created_at=now,
            expires_at=now + self.ttl_s,
        )
        self._cleanup(now)
        return token

    def validate(self, token: str | None) -> Session | None:
        if not self.enabled:
            return Session(self.username, 0.0, 0.0)
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= time.time():
            self._sessions.pop(token, None)
            return None
        return session

    def logout(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def _cleanup(self, now: float) -> None:
        expired = [
            token
            for token, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for token in expired:
            self._sessions.pop(token, None)
