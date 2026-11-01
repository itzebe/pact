"""Durable, exactly-once processing of PayPal webhook events.

PayPal delivers a webhook at least once, so the same event can arrive many times —
on a retry after a timeout, on a redelivery after a 5xx, or twice by accident. A
webhook is also the one place an *unauthenticated* party could try to change money
state, so two properties have to hold:

1. Only an event whose signature PayPal itself confirms may be applied. That check
   lives in the PayPal client (``verify_webhook_signature``); this module never
   decides authenticity.
2. An event may be applied *at most once*, across every instance. A duplicate must
   be a no-op, not a second settlement.

The second property is why this registry is not a process-local set: two instances
receiving the same redelivery would each see an empty local set and each apply it.
It lives in PostgreSQL whenever a shared database is configured, where the primary
key on ``event_id`` makes the claim a single atomic statement. Where there is no
shared database (local development, a single container) a process-local registry is
honest, exactly as the ticket registry is.

Failure is closed: a shared store that is configured but unreachable refuses every
event rather than falling back to a local registry no other instance can see. Event
payloads may contain payer identity, so only the event id, type and job id are
persisted — never the raw body.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from ..config import settings

log = logging.getLogger("pact.webhooks")

# Processed events are retained long enough that a delayed redelivery is still
# recognised as a duplicate, but not forever: the table must not grow without bound.
RETENTION_DAYS = 30
CLEANUP_BATCH = 500

WEBHOOK_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_webhooks (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    job_id       TEXT,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    outcome      TEXT NOT NULL DEFAULT 'received'
);

CREATE INDEX IF NOT EXISTS processed_webhooks_received_at_idx
    ON processed_webhooks (received_at);
"""

# Claim an event id exactly once. A brand-new id is inserted; a redelivery hits the
# primary-key conflict and DO NOTHING yields no row, so exactly one caller wins even
# when two instances process the same event at the same instant.
_CLAIM_SQL = """
INSERT INTO processed_webhooks (event_id, event_type, job_id)
VALUES (%s, %s, %s)
ON CONFLICT (event_id) DO NOTHING
RETURNING event_id
"""

_SETTLE_SQL = """
UPDATE processed_webhooks
SET processed_at = now(), outcome = %s
WHERE event_id = %s
"""

# Bounded, so one call can never delete an unbounded number of rows.
_PRUNE_SQL = """
DELETE FROM processed_webhooks
WHERE event_id IN (
    SELECT event_id FROM processed_webhooks
    WHERE received_at < now() - make_interval(days => %s)
    ORDER BY received_at
    LIMIT %s
)
"""


@dataclass
class WebhookRecord:
    """The minimal, non-sensitive record of a webhook event.

    Deliberately excludes the payload: it can carry payer identity and is not
    needed to decide idempotency or to audit *that* an event arrived.
    """

    event_id: str
    event_type: str
    job_id: str | None = None
    outcome: str = "received"


class InMemoryWebhookStore:
    """Process-local exactly-once registry. Correct for a single instance."""

    backend = "memory"
    shared = False

    def __init__(self) -> None:
        self._events: dict[str, float] = {}
        self._guard = threading.Lock()

    def claim(self, record: WebhookRecord) -> bool:
        """True if this is the first time the event is seen; False on a duplicate."""
        now = time.monotonic()
        with self._guard:
            self._drop_expired(now)
            if record.event_id in self._events:
                return False
            self._events[record.event_id] = now + RETENTION_DAYS * 86400
            return True

    def settle(self, event_id: str, outcome: str) -> None:
        # The in-memory store keeps no outcome; the claim is what matters for
        # idempotency, and the job ledger records the applied result.
        return None

    def prune(self, limit: int | None = None) -> int:
        with self._guard:
            return self._drop_expired(time.monotonic())

    def _drop_expired(self, now: float) -> int:
        expired = [k for k, exp in self._events.items() if exp <= now]
        for k in expired:
            del self._events[k]
        return len(expired)

    def size(self) -> int:
        with self._guard:
            return len(self._events)


class PostgresWebhookStore:
    """Instance-safe exactly-once registry, shared through PostgreSQL."""

    backend = "postgres"
    shared = True

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(WEBHOOK_SCHEMA)

    def _connect(self):
        from .dbstore import _driver

        return _driver().connect(self.dsn, autocommit=True)

    def claim(self, record: WebhookRecord) -> bool:
        """Claim the event id atomically. False means it was already claimed.

        A store failure is *not* swallowed here: unlike a ticket, an unclaimed event
        cannot be safely applied, so the caller must refuse it. Raising lets the
        route answer with a retryable status instead of silently processing twice.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_CLAIM_SQL, (record.event_id, record.event_type, record.job_id))
            won = cur.fetchone() is not None
        self._prune_quietly()
        return won

    def settle(self, event_id: str, outcome: str) -> None:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(_SETTLE_SQL, (outcome[:64], event_id))
        except Exception as exc:
            # Recording the outcome is bookkeeping; the event has already been
            # claimed and applied. A failure here must not fail the delivery.
            log.warning("Could not record webhook outcome for %s: %s", event_id, type(exc).__name__)

    def prune(self, limit: int | None = None) -> int:
        batch = CLEANUP_BATCH if limit is None else limit
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_PRUNE_SQL, (RETENTION_DAYS, batch))
            return cur.rowcount

    def _prune_quietly(self) -> None:
        try:
            self.prune()
        except Exception as exc:
            log.warning("Could not prune processed webhooks: %s", type(exc).__name__)

    def size(self) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM processed_webhooks")
            return int(cur.fetchone()[0])


class UnavailableWebhookStore:
    """Stands in when a shared store is configured but could not be opened.

    Every event is refused: a shared decision cannot be made, and applying an event
    against a local registry no other instance can see is exactly the double-apply
    this module exists to prevent.
    """

    backend = "unavailable"
    shared = True

    def __init__(self, error: Exception) -> None:
        self.error = error

    def claim(self, record: WebhookRecord) -> bool:
        log.error(
            "Shared webhook store is unavailable (%s); refusing the event",
            type(self.error).__name__,
        )
        return False

    def settle(self, event_id: str, outcome: str) -> None:
        return None

    def prune(self, limit: int | None = None) -> int:
        return 0

    def size(self) -> int:
        return 0


def _select_webhook_store() -> InMemoryWebhookStore | PostgresWebhookStore | UnavailableWebhookStore:
    dsn = settings.database_url
    if not dsn:
        return InMemoryWebhookStore()
    try:
        return PostgresWebhookStore(dsn)
    except Exception as exc:
        log.error(
            "DATABASE_URL is set but the shared webhook store could not be opened (%s). "
            "Webhook events will be refused until the database is reachable.",
            type(exc).__name__,
        )
        return UnavailableWebhookStore(exc)


webhook_store = _select_webhook_store()


def prune_webhook_records(store=None, limit: int | None = None) -> int:
    """Expire old processed-event records so the table stays bounded.

    Exposed for an operator or a scheduled job; the store also prunes
    opportunistically on each claim, so the table cannot grow without bound even
    when nothing calls this.
    """
    return (store or webhook_store).prune(limit)
