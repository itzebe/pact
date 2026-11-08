"""Distributed rate limiting for the money-bearing routes.

Why this exists
---------------
Every route that can authorize, capture, void or refund a payment is protected by
authentication and object-level authorization, but nothing bounded how *often* a
single caller (or a rotating set of guest sessions from one address) could hit
them. PayPal has its own rate limits, but a caller who exhausts them degrades the
service for everyone, and a runaway client can drive unbounded provider calls.
This module adds the missing ceiling.

Identity is the unit of the limit
---------------------------------
A limit is per (rule, identity). The identity is chosen so that a caller cannot
trivially rotate around it:

* an authenticated principal is keyed on its subject (a signed token subject, so
  it cannot be forged);
* a guest is keyed on its client address, because a guest is anonymous and can
  mint fresh sessions at will — the address is the only identifier it cannot
  rotate (this matches the existing per-address bound on guest-session creation);
* an unauthenticated caller is keyed on the client address only.

The client address is the *rightmost* ``X-Forwarded-For`` entry — the one the
nearest trusted proxy wrote — never the leftmost, which a caller controls and
could forge on every request to evade the limit. The address is hashed, never
stored raw.

Shared, or honestly local
-------------------------
The counter lives in PostgreSQL whenever a database is configured, so two
instances share one decision and a caller cannot spend the budget once per
instance. With no database the deployment is single-instance by definition and a
process-local counter is exactly as strong; the response says which one is in use
(``X-RateLimit-Scope``) rather than implying a distributed guarantee it does not
have.

Failure is closed
-----------------
If a shared store is configured but cannot be reached, the request is refused
rather than allowed: a limit that silently stops applying under load is worse
than a brief refusal, because the failure mode of a money route must be "do not
call the provider". The only exception is *recording* usage after a request has
already been served — a post-hoc record failure is logged and dropped, never
turned into an error for a response the caller already has.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import settings

log = logging.getLogger("pact.ratelimit")


@dataclass(frozen=True)
class RateLimitRule:
    """A fixed-window limit: ``limit`` requests per ``window_seconds`` per identity."""

    limit: int
    window_seconds: int

    def retry_after(self, oldest_in_window: float, now: float) -> int:
        """Whole seconds until the oldest counted request leaves the window.

        At least 1, so a client is never told to retry in 0 seconds and hot-loop.
        """
        remaining = self.window_seconds - (now - oldest_in_window)
        return max(1, int(remaining) + (1 if remaining % 1 else 0))


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after: int
    limit: int
    window_seconds: int
    scope: str  # "postgres" | "memory" | "unavailable"

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Scope": self.scope,
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after)
        return headers


# --------------------------------------------------------------------- stores

#: The shared counter. ``(scope, key, at)`` rows; ``at`` is indexed so a count is a
#: range scan and pruning is cheap. ``ok`` is unused for rate limiting but kept so
#: the table shape matches the per-identifier attempt log the login throttle uses.
_RATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_limit_events (
    id         BIGSERIAL PRIMARY KEY,
    scope      TEXT NOT NULL,
    key        TEXT NOT NULL,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS rate_limit_events_lookup_idx
    ON rate_limit_events (scope, key, at);
"""

_COUNT_SQL = """
SELECT count(*), min(at) FROM rate_limit_events
WHERE scope = %s AND key = %s AND at > now() - make_interval(secs => %s)
"""

_RECORD_SQL = "INSERT INTO rate_limit_events (scope, key) VALUES (%s, %s)"

#: Bounded opportunistic prune of rows older than the largest window. A bare
#: ``DELETE ... LIMIT`` is invalid PostgreSQL; the CTE + ``USING`` form is.
_PRUNE_SQL = """
WITH expired AS (
    SELECT id FROM rate_limit_events
    WHERE at <= now() - make_interval(secs => %s)
    ORDER BY at
    LIMIT %s
    FOR UPDATE SKIP LOCKED
)
DELETE FROM rate_limit_events r
USING expired e
WHERE r.id = e.id
"""

CLEANUP_BATCH = 500
#: Rows older than this are always prunable, whatever the configured windows are.
MAX_RETENTION_SECONDS = 3600


class InMemoryRateLimiter:
    """Process-local fixed-window counter. Honest for a single-instance deployment."""

    backend = "memory"
    shared = False

    def __init__(self) -> None:
        # scope -> key -> list of monotonic timestamps within the window
        self._events: dict[str, dict[str, list[float]]] = {}
        self._guard = threading.Lock()

    def check(self, scope: str, key: str, rule: RateLimitRule) -> RateLimitDecision:
        now = time.monotonic()
        cutoff = now - rule.window_seconds
        with self._guard:
            bucket = self._events.setdefault(scope, {})
            times = [t for t in bucket.get(key, []) if t > cutoff]
            if len(times) >= rule.limit:
                bucket[key] = times
                return RateLimitDecision(
                    allowed=False,
                    remaining=0,
                    retry_after=rule.retry_after(times[0], now),
                    limit=rule.limit,
                    window_seconds=rule.window_seconds,
                    scope=self.backend,
                )
            times.append(now)
            bucket[key] = times
            return RateLimitDecision(
                allowed=True,
                remaining=rule.limit - len(times),
                retry_after=0,
                limit=rule.limit,
                window_seconds=rule.window_seconds,
                scope=self.backend,
            )

    def record(self, scope: str, key: str) -> None:
        """No-op: the in-memory counter records usage inside ``check``."""

    def reset(self) -> None:
        with self._guard:
            self._events.clear()


class PostgresRateLimiter:
    """Instance-safe fixed-window counter, shared through PostgreSQL."""

    backend = "postgres"
    shared = True

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        from .pool import get_pool

        self._pool = get_pool(dsn)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_RATE_SCHEMA)

    def check(self, scope: str, key: str, rule: RateLimitRule) -> RateLimitDecision:
        """Count usage in the window and record this request if it fits.

        Returns a refusing decision when the store cannot be reached: a shared
        limit that cannot be evaluated must refuse, not allow.
        """
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute(_COUNT_SQL, (scope, key, rule.window_seconds))
                count, oldest = cur.fetchone()
                count = int(count)
                if count >= rule.limit:
                    retry = rule.window_seconds
                    if oldest is not None:
                        retry = _retry_after(oldest, rule.window_seconds)
                    return RateLimitDecision(
                        allowed=False,
                        remaining=0,
                        retry_after=retry,
                        limit=rule.limit,
                        window_seconds=rule.window_seconds,
                        scope=self.backend,
                    )
                cur.execute(_RECORD_SQL, (scope, key))
                return RateLimitDecision(
                    allowed=True,
                    remaining=rule.limit - count - 1,
                    retry_after=0,
                    limit=rule.limit,
                    window_seconds=rule.window_seconds,
                    scope=self.backend,
                )
        except Exception as exc:
            log.error(
                "Shared rate-limit store is unavailable (%s); refusing the request",
                type(exc).__name__,
            )
            return RateLimitDecision(
                allowed=False,
                remaining=0,
                retry_after=1,
                limit=rule.limit,
                window_seconds=rule.window_seconds,
                scope="unavailable",
            )
        finally:
            self._prune_quietly()

    def record(self, scope: str, key: str) -> None:
        """Record usage for a request already served (check/record split).

        Used where the limit must not gate a route that authenticates or performs
        its own gating: the request is served, and the *use* is recorded here so a
        later burst is still bounded. A failure here is logged and dropped.
        """
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute(_RECORD_SQL, (scope, key))
        except Exception as exc:
            log.warning("Could not record rate-limit usage: %s", type(exc).__name__)

    def prune(self, limit: int | None = None) -> int:
        batch = CLEANUP_BATCH if limit is None else limit
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_PRUNE_SQL, (MAX_RETENTION_SECONDS, batch))
            return cur.rowcount

    def reset(self) -> None:
        """Clear every counted event. For test isolation; not used in production."""
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute("TRUNCATE rate_limit_events")
        except Exception as exc:
            log.warning("Could not reset rate-limit events: %s", type(exc).__name__)

    def _prune_quietly(self) -> None:
        try:
            self.prune()
        except Exception as exc:
            log.warning("Could not prune rate-limit events: %s", type(exc).__name__)


class UnavailableRateLimiter:
    """Stands in when a shared store is configured but could not be opened.

    A deployment that asked for a shared limiter and did not get one must not
    quietly fall back to a process-local counter that no other instance sees, and
    must not allow the request. Every money-route request is refused until the
    database is reachable.
    """

    backend = "unavailable"
    shared = True

    def __init__(self, error: Exception) -> None:
        self.error = error

    def check(self, scope: str, key: str, rule: RateLimitRule) -> RateLimitDecision:
        log.error(
            "Shared rate-limit store is unavailable (%s); refusing the request until "
            "the database is reachable",
            type(self.error).__name__,
        )
        return RateLimitDecision(
            allowed=False,
            remaining=0,
            retry_after=1,
            limit=rule.limit,
            window_seconds=rule.window_seconds,
            scope=self.backend,
        )

    def record(self, scope: str, key: str) -> None:
        return None

    def prune(self, limit: int | None = None) -> int:
        return 0


def _retry_after(oldest: datetime, window_seconds: int) -> int:
    """Whole seconds until ``oldest`` leaves the window, at least 1."""
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - oldest).total_seconds()
    remaining = window_seconds - elapsed
    return max(1, int(remaining) + (1 if remaining % 1 else 0))


def _select_rate_limiter() -> InMemoryRateLimiter | PostgresRateLimiter | UnavailableRateLimiter:
    dsn = settings.database_url
    if not dsn:
        return InMemoryRateLimiter()
    try:
        return PostgresRateLimiter(dsn)
    except Exception as exc:
        log.error(
            "DATABASE_URL is set but the shared rate-limit store could not be opened (%s). "
            "Money-route requests will be refused until the database is reachable.",
            type(exc).__name__,
        )
        return UnavailableRateLimiter(exc)


rate_limiter = _select_rate_limiter()


# ------------------------------------------------------------------ identities

#: Max length of a stored key, so a pathological subject cannot bloat the table.
MAX_KEY_LEN = 160


def identity_for(principal_subject: str | None, is_guest: bool, ip_hash: str) -> str:
    """Build the identity a limit is counted against.

    * authenticated (non-guest): the signed subject, prefixed so it can never
      collide with an address-derived key. A registered account is bounded per
      account.
    * guest: the client address alone, **not** the guest subject. A guest is
      anonymous and can mint a fresh session at will, so keying on the subject
      would let it reset its own budget; the address is the only identifier it
      cannot rotate. This matches the existing per-address bound on guest-session
      *creation* (``PACT_GUEST_NEW_PER_IP``), so both guest controls share one
      notion of "one source".
    * anonymous: the hashed client address.
    """
    if principal_subject and not is_guest:
        return f"u:{principal_subject}"[:MAX_KEY_LEN]
    if principal_subject and is_guest:
        return f"g:{ip_hash}"[:MAX_KEY_LEN]
    return f"ip:{ip_hash}"[:MAX_KEY_LEN]


#: Per-endpoint rules, keyed by the logical operation name. The values come from
#: ``settings.rate_limit`` so an operator can tune them by environment variable
#: without a code change; ``rate_limit_problems`` validates them at startup.
def money_rule(name: str) -> RateLimitRule:
    rl = settings.rate_limit
    table = {
        "run": RateLimitRule(rl.run_limit, rl.run_window_seconds),
        "approve": RateLimitRule(rl.approve_limit, rl.approve_window_seconds),
        "cancel": RateLimitRule(rl.cancel_limit, rl.cancel_window_seconds),
        "reconcile": RateLimitRule(rl.reconcile_limit, rl.reconcile_window_seconds),
        "refund": RateLimitRule(rl.refund_limit, rl.refund_window_seconds),
        "job_create": RateLimitRule(rl.job_create_limit, rl.job_create_window_seconds),
        "read": RateLimitRule(rl.read_limit, rl.read_window_seconds),
    }
    return table[name]


def enforce(scope: str, identity: str, rule: RateLimitRule) -> RateLimitDecision:
    """Check and (if allowed) record one request for ``scope``/``identity``."""
    return rate_limiter.check(scope, identity, rule)


def record_usage(scope: str, identity: str) -> None:
    """Record usage without gating. For routes that gate themselves elsewhere."""
    rate_limiter.record(scope, identity)
