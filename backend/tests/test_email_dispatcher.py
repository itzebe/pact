"""Outbox dispatcher: the crash-safety and delivery-honesty contract.

The dispatcher had no coverage, and a typo in its render step (reading a
``_raw_token`` attribute that does not exist instead of ``token_raw``) silently
marked *every* verification and reset email permanently failed. These tests
exercise the real dispatcher against a real provider double so that class of bug
cannot come back: a message that can be rendered and accepted must reach the
provider and be recorded as ``sent``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from pact.core.auth_store import InMemoryAuthStore, OutboxMessage
from pact.core.email import (
    DELIVERY_ACCEPTED,
    NoopProvider,
    OutboxDispatcher,
    SendResult,
)

DATABASE_URL = os.getenv("DATABASE_URL", "")
pg_only = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set")


class RecordingProvider:
    """Accepts everything and records what it was asked to send."""

    name = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, *, to_email, subject, body, idempotency_key) -> SendResult:
        self.sent.append((to_email, subject, body))
        return SendResult(accepted=True, provider_id=f"prov-{len(self.sent)}")


class FailingProvider:
    """Rejects retryably, so the dispatcher must leave the message pending."""

    name = "failing"

    def __init__(self) -> None:
        self.calls = 0

    def send(self, *, to_email, subject, body, idempotency_key) -> SendResult:
        self.calls += 1
        return SendResult(accepted=False, retryable=True, error="temporary")


def _link_builder(kind: str, raw: str) -> str:
    return f"https://pact.test/{kind}?token={raw}"


def _verify_msg(msg_id: str = "m1") -> OutboxMessage:
    return OutboxMessage(
        id=msg_id, kind="verify_email", user_id="usr_1", to_email="user@example.com",
        token_hash="th-1", idempotency_key=f"verify:usr_1:{msg_id}", attempts=0,
        token_raw="RAW-VERIFY-TOKEN",
    )


# --------------------------------------------------------------------------- #
# The regression: a renderable message must actually be sent.
# --------------------------------------------------------------------------- #


def test_dispatcher_sends_a_renderable_verification_email():
    store = InMemoryAuthStore()
    store.enqueue_email(_verify_msg())
    provider = RecordingProvider()

    report = OutboxDispatcher(store, provider=provider, link_builder=_link_builder).dispatch()

    assert report == {"claimed": 1, "sent": 1, "retried": 0, "failed": 0}
    assert len(provider.sent) == 1
    to_email, subject, body = provider.sent[0]
    assert to_email == "user@example.com"
    assert "Verify" in subject
    # The link the provider received carries the real raw token.
    assert "RAW-VERIFY-TOKEN" in body

    msg = store._outbox["m1"]
    assert msg.status == "sent"
    assert msg.delivery_status == DELIVERY_ACCEPTED
    assert msg.last_error is None


def test_dispatcher_sends_a_renderable_password_reset_email():
    store = InMemoryAuthStore()
    store.enqueue_email(
        OutboxMessage(
            id="r1", kind="password_reset", user_id="usr_1", to_email="user@example.com",
            token_hash="th-r", idempotency_key="reset:usr_1:r1", attempts=0,
            token_raw="RAW-RESET-TOKEN",
        )
    )
    provider = RecordingProvider()

    report = OutboxDispatcher(store, provider=provider, link_builder=_link_builder).dispatch()

    assert report["sent"] == 1
    assert "RAW-RESET-TOKEN" in provider.sent[0][2]


def test_the_raw_token_is_dropped_once_a_message_is_sent():
    """A sent message must not retain the raw token it no longer needs."""
    store = InMemoryAuthStore()
    store.enqueue_email(_verify_msg())
    OutboxDispatcher(store, provider=RecordingProvider(), link_builder=_link_builder).dispatch()

    assert store._outbox["m1"].token_raw is None


# --------------------------------------------------------------------------- #
# Honest failure handling
# --------------------------------------------------------------------------- #


def test_no_link_is_retryable_not_terminal():
    """A missing PACT_PUBLIC_URL must not destroy a legitimate verification email."""
    store = InMemoryAuthStore()
    store.enqueue_email(_verify_msg())
    provider = RecordingProvider()

    # No link_builder -> no link can be built.
    report = OutboxDispatcher(store, provider=provider, link_builder=None).dispatch()

    assert provider.sent == []
    assert report["retried"] == 1
    assert report["failed"] == 0
    msg = store._outbox["m1"]
    assert msg.status == "pending", "a config problem must not permanently fail the message"
    assert "PACT_PUBLIC_URL" in (msg.last_error or "")


def test_a_retryable_provider_failure_leaves_the_message_pending():
    store = InMemoryAuthStore()
    store.enqueue_email(_verify_msg())
    provider = FailingProvider()

    report = OutboxDispatcher(store, provider=provider, link_builder=_link_builder).dispatch()

    assert report["retried"] == 1
    assert store._outbox["m1"].status == "pending"
    assert provider.calls == 1


def test_the_noop_provider_never_claims_delivery_and_keeps_the_message():
    """With no provider configured, nothing is sent and nothing is destroyed."""
    store = InMemoryAuthStore()
    store.enqueue_email(_verify_msg())

    report = OutboxDispatcher(store, provider=NoopProvider(), link_builder=_link_builder).dispatch()

    assert report["sent"] == 0
    msg = store._outbox["m1"]
    assert msg.status == "pending"
    assert msg.delivery_status == "queued"
    assert "no email provider is configured" in (msg.last_error or "")


def test_a_terminal_failure_drops_the_raw_token():
    store = InMemoryAuthStore()
    store.enqueue_email(_verify_msg())
    # Drive attempts to the cap so the failure becomes terminal.
    from pact.core.email import _max_attempts

    store._outbox["m1"].attempts = _max_attempts()
    OutboxDispatcher(store, provider=FailingProvider(), link_builder=_link_builder).dispatch()

    msg = store._outbox["m1"]
    assert msg.status == "dead"
    assert msg.token_raw is None


# --------------------------------------------------------------------------- #
# Crash recovery: a claimed-but-unsent message must not be stranded.
# --------------------------------------------------------------------------- #


def test_an_abandoned_sending_lease_is_recovered():
    """A dispatcher that crashed after claiming must not strand the message."""
    store = InMemoryAuthStore()
    store.enqueue_email(_verify_msg())

    # Simulate a crash: claim it, then let the lease expire without sending.
    claimed = store.claim_outbox(batch=1)
    assert claimed and claimed[0].status == "sending"
    store._outbox["m1"].lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    provider = RecordingProvider()
    report = OutboxDispatcher(store, provider=provider, link_builder=_link_builder).dispatch()

    assert report["sent"] == 1, "the expired lease must be reclaimed and delivered"
    assert store._outbox["m1"].status == "sent"


# --------------------------------------------------------------------------- #
# PostgreSQL: the same contract on the real shared store.
# --------------------------------------------------------------------------- #


@pg_only
def test_pg_dispatcher_sends_and_clears_the_token():
    from pact.core.auth_store import PostgresAuthStore

    store = PostgresAuthStore(DATABASE_URL)
    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE email_outbox")

    store.enqueue_email(_verify_msg("pg-disp-1"))
    provider = RecordingProvider()
    report = OutboxDispatcher(store, provider=provider, link_builder=_link_builder).dispatch()

    assert report["sent"] == 1
    assert "RAW-VERIFY-TOKEN" in provider.sent[0][2]

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, token_raw, delivery_status FROM email_outbox WHERE id = 'pg-disp-1'")
        status, token_raw, delivery_status = cur.fetchone()
    assert status == "sent"
    assert token_raw is None, "the raw token must be cleared once the message is sent"
    assert delivery_status == DELIVERY_ACCEPTED
