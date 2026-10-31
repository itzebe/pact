"""Shared, atomic consumption of single-use SSE stream tickets.

A stream ticket travels in a URL, so a captured one must not be replayable. The
first version of this guard kept consumed ticket ids in a process-local dict,
which is correct for one process and *wrong* for a multi-instance deployment: a
replay that lands on another instance finds an empty registry and is accepted.

This module moves the registry into PostgreSQL whenever a shared database is
configured, so the decision is one statement against one row and two instances
cannot both accept the same ticket. Where no shared database exists (local
development, a single container with a mounted volume) it falls back to a
process-local registry — exactly as safe as the deployment is single-instance,
which is why the deployment docs require ``DATABASE_URL`` for anything that runs
more than one instance.

Failure is closed. If a shared store is configured but unreachable, every ticket
is refused rather than silently accepted by a process-local fallback that no
other instance can see. Ticket values are never logged: an id is a credential
for the lifetime of its ticket.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from ..config import settings

log = logging.getLogger("pact.tickets")

# The table is keyed on the ticket's ``jti`` and carries the moment the row may
# be discarded. ``expires_at`` is indexed so pruning is a range scan rather than
# a table scan, which is what keeps cleanup cheap as the table fills.
TICKET_SCHEMA = """
CREATE TABLE IF NOT EXISTS consumed_tickets (
    jti        TEXT PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS consumed_tickets_expires_at_idx
    ON consumed_tickets (expires_at);
"""

# Consume exactly once, atomically, in a single statement.
#
# A brand-new id is inserted. A second attempt for the *same, still-live* id
# finds the conflict, the ``WHERE`` makes the update a no-op, and ``RETURNING``
# yields no row — so exactly one caller wins. Two callers racing on the same new
# id serialize on the primary key; the loser re-evaluates the conflict after the
# winner commits, sees a future ``expires_at``, and is refused. An id whose row
# has already passed its expiry may be re-consumed, which is harmless because the
# ticket that carried it has itself expired and is rejected by signature/expiry
# verification before it reaches here.
_CONSUME_SQL = """
INSERT INTO consumed_tickets (jti, expires_at)
VALUES (%s, %s)
ON CONFLICT (jti) DO UPDATE
    SET expires_at = EXCLUDED.expires_at
    WHERE consumed_tickets.expires_at <= now()
RETURNING jti
"""

# Bounded, so one call can never delete an unbounded number of rows. Repeated
# calls drain the table; each row lives only as long as its ticket did.
_PRUNE_SQL = """
DELETE FROM consumed_tickets
WHERE jti IN (
    SELECT jti FROM consumed_tickets
    WHERE expires_at <= now()
    ORDER BY expires_at
    LIMIT %s
)
"""

# How many expired rows a single opportunistic prune may remove.
CLEANUP_BATCH = 200


class InMemoryTicketStore:
    """Process-local single-use registry. Correct for a single-instance deployment."""

    backend = "memory"
    shared = False

    def __init__(self) -> None:
        self._consumed: dict[str, float] = {}
        self._guard = threading.Lock()

    def consume(self, ticket_id: str, ttl_seconds: float) -> bool:
        """Record ``ticket_id`` as used for ``ttl_seconds``. False if already used.

        Atomic under a lock, so two concurrent connections cannot both consume the
        same id. The expiry is stored on the *monotonic* clock (the same clock it
        is pruned against), so the map stays bounded; mixing in a wall-clock value
        would mean the prune never fires and the map would grow without limit.
        """
        now = time.monotonic()
        with self._guard:
            self._drop_expired(now)
            if ticket_id in self._consumed:
                return False
            self._consumed[ticket_id] = now + max(0.0, ttl_seconds)
            return True

    def prune(self, limit: int | None = None) -> int:
        with self._guard:
            return self._drop_expired(time.monotonic())

    def _drop_expired(self, now: float) -> int:
        expired = [key for key, exp in self._consumed.items() if exp <= now]
        for key in expired:
            del self._consumed[key]
        return len(expired)

    def size(self) -> int:
        with self._guard:
            return len(self._consumed)


class PostgresTicketStore:
    """Instance-safe single-use registry, shared through PostgreSQL."""

    backend = "postgres"
    shared = True

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(TICKET_SCHEMA)

    def _connect(self):
        from .dbstore import _driver

        return _driver().connect(self.dsn, autocommit=True)

    def consume(self, ticket_id: str, ttl_seconds: float) -> bool:
        """Consume ``ticket_id`` exactly once across every instance.

        Returns False when the id was already consumed *or* when the store could
        not be reached. Both must refuse the ticket: the second because a shared
        decision cannot be made, and refusing is the only safe answer.
        """
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(0.0, ttl_seconds))
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(_CONSUME_SQL, (ticket_id, expires_at))
                won = cur.fetchone() is not None
        except Exception as exc:
            # The class name is enough to diagnose; the ticket value is a
            # credential and must not reach the log.
            log.error(
                "Shared ticket store is unavailable (%s); refusing the ticket",
                type(exc).__name__,
            )
            return False
        self._prune_quietly()
        return won

    def prune(self, limit: int | None = None) -> int:
        batch = CLEANUP_BATCH if limit is None else limit
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_PRUNE_SQL, (batch,))
            return cur.rowcount

    def _prune_quietly(self) -> None:
        """Expire old ids best-effort. A prune failure must not refuse a valid ticket."""
        try:
            self.prune()
        except Exception as exc:
            log.warning("Could not prune consumed tickets: %s", type(exc).__name__)

    def size(self) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM consumed_tickets")
            return int(cur.fetchone()[0])


class UnavailableTicketStore:
    """Stands in when a shared store is configured but could not be opened.

    A deployment that asked for a shared registry and did not get one must not
    quietly fall back to a process-local one: a replay landing on another instance
    would then be accepted, which is the exact hole this module exists to close.
    Every ticket is refused until the database is reachable.
    """

    backend = "unavailable"
    shared = True

    def __init__(self, error: Exception) -> None:
        self.error = error

    def consume(self, ticket_id: str, ttl_seconds: float) -> bool:
        log.error(
            "Shared ticket store is unavailable (%s); refusing every stream ticket "
            "until the database is reachable",
            type(self.error).__name__,
        )
        return False

    def prune(self, limit: int | None = None) -> int:
        return 0

    def size(self) -> int:
        return 0


def _select_ticket_store() -> InMemoryTicketStore | PostgresTicketStore | UnavailableTicketStore:
    """Choose the registry from configuration, mirroring the job store's choice.

    ``DATABASE_URL`` is the single switch: set it and ticket consumption is shared
    and instance-safe; leave it unset and the deployment is single-instance by
    definition, so a process-local registry is honest. A configured-but-unreachable
    database is neither, and fails closed.
    """
    dsn = settings.database_url
    if not dsn:
        return InMemoryTicketStore()
    try:
        return PostgresTicketStore(dsn)
    except Exception as exc:
        log.error(
            "DATABASE_URL is set but the shared ticket store could not be opened (%s). "
            "Stream tickets will be refused until the database is reachable.",
            type(exc).__name__,
        )
        return UnavailableTicketStore(exc)


ticket_store = _select_ticket_store()
