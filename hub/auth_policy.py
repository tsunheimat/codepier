"""Explicit lifetimes for persistent, individually revocable connections.

Zero disables a server-side idle/absolute expiry, not revocation or live policy.
The finite sentinel keeps old SQL readers fail-closed and avoids Infinity in JSON.
Browsers cap cookie retention independently; routine responses never re-set credentials.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from shared.config import ConfigurationError, env_int

UNTIL_REVOKED = 253402300799.0
BROWSER_COOKIE_SECONDS = 400 * 86400
REFRESH_TOMBSTONE_SECONDS = 30 * 86400


@dataclass(frozen=True)
class AuthPolicy:
    session_idle_seconds: int = 0
    session_max_seconds: int = 0
    access_seconds: int = 3600
    refresh_seconds: int = 0
    refresh_retry_seconds: int = 30

    @classmethod
    def from_env(cls) -> AuthPolicy:
        values = {
            "session_idle_seconds": env_int("CODEPIER_SESSION_IDLE_SECONDS", 0, 0, 3650 * 86400),
            "session_max_seconds": env_int("CODEPIER_SESSION_MAX_SECONDS", 0, 0, 3650 * 86400),
            "access_seconds": env_int("CODEPIER_OAUTH_ACCESS_SECONDS", 3600, 300, 86400),
            "refresh_seconds": env_int("CODEPIER_OAUTH_REFRESH_SECONDS", 0, 0, 3650 * 86400),
            "refresh_retry_seconds": env_int("CODEPIER_OAUTH_REFRESH_RETRY_SECONDS", 30, 0, 120),
        }
        for key in ("session_idle_seconds", "session_max_seconds", "refresh_seconds"):
            if 0 < values[key] < 60:
                raise ConfigurationError("Authentication lifetimes must be 0 or at least 60 seconds: " + key)
        return cls(**values)

    def session_deadline(self, now: float, authenticated_at: float) -> float:
        idle = now + self.session_idle_seconds if self.session_idle_seconds else UNTIL_REVOKED
        absolute = authenticated_at + self.session_max_seconds if self.session_max_seconds else UNTIL_REVOKED
        return min(idle, absolute)

    def refresh_deadline(self, now: float) -> float:
        return now + self.refresh_seconds if self.refresh_seconds else UNTIL_REVOKED

    @staticmethod
    def cookie_age(expires: float, now: float) -> int:
        return max(0, min(BROWSER_COOKIE_SECONDS, math.ceil(expires - now)))
