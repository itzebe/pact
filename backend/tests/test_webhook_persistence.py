"""Webhook application must *persist* to the shared store, not just report success.

Regression for a defect where ``process_event`` applied an event to the object the
settlement guard handed back but then saved a *different* (stale) object — so on the
Postgres path a verified ``PAYMENT.CAPTURE.COMPLETED`` was acknowledged as ``applied``
while the database still held the pre-event state. The in-memory doubles in
``test_webhook_postgres.py`` could not catch it: their ``get`` returns the same object
that ``save`` stored, so mutating one object mutated the stored one too. A real
``PostgresJobStore`` returns a fresh object per ``get``, which is what exposed it.

These tests use a real PostgreSQL 16 through ``PostgresJobStore`` and assert the new
state is readable from a *fresh* store instance (i.e. it reached the database), both
with and without the settlement guard.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from pact.core.models import IN_FLIGHT_CAPTURE, EscrowState, Job, JobStatus

DATABASE_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set; Postgres tests are opt-in"
)


@pytest.fixture()
def pg_store():
    import psycopg

    from pact.core.dbstore import PostgresJobStore

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS jobs")
        cur.execute("DROP TABLE IF EXISTS processed_webhooks")
    return PostgresJobStore(DATABASE_URL)


def _seed(store, state: EscrowState, capture_id: str, status: JobStatus) -> Job:
    job = Job(id="job_persist", title="t", brief="b", criteria=["c"], budget_max=Decimal("100"))
    job.escrow.amount = Decimal("50.00")
    job.escrow.currency = "USD"
    job.escrow.state = state
    job.escrow.capture_id = capture_id
    job.status = status
    store.save(job)
    return job


def _capture_completed() -> dict:
    return {
        "id": "WH-PERSIST-1",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAP-PERSIST",
            "status": "COMPLETED",
            "custom_id": "job_persist",
            "amount": {"value": "50.00", "currency_code": "USD"},
        },
    }


def _guard(store):
    """The production settlement guard for a Postgres-backed engine."""
    import tempfile
    from pathlib import Path

    from pact.core.settlement import SettlementEngine
    from pact.core.store import ReputationLedger
    from pact.paypal import MockPayPalEscrowClient

    engine = SettlementEngine(
        MockPayPalEscrowClient(),
        ReputationLedger(Path(tempfile.mkdtemp()) / "rep.json"),
        store=store,
    )
    return engine._guard


@pytest.mark.parametrize("use_guard", [False, True], ids=["no-guard", "production-guard"])
def test_an_applied_capture_is_persisted_to_postgres(pg_store, use_guard):
    """The whole point: an 'applied' webhook must be durable in the shared store."""
    from pact.core.webhook_store import PostgresWebhookStore
    from pact.core.webhooks import process_event

    _seed(pg_store, EscrowState.UNRESOLVED, IN_FLIGHT_CAPTURE, JobStatus.UNRESOLVED)
    webhooks = PostgresWebhookStore(DATABASE_URL)

    outcome, touched = process_event(
        payload=_capture_completed(),
        verified=True,
        store=webhooks,
        get_job=pg_store.get,
        save_job=pg_store.save,
        job_guard=_guard(pg_store) if use_guard else None,
    )

    assert outcome == "applied"
    assert touched.escrow.state is EscrowState.RELEASED

    # A *fresh* store reads the row back from the database.
    from pact.core.dbstore import PostgresJobStore

    reloaded = PostgresJobStore(DATABASE_URL).get("job_persist")
    assert reloaded.escrow.state is EscrowState.RELEASED, "capture was not persisted"
    assert reloaded.escrow.capture_id == "CAP-PERSIST"
    assert reloaded.status is JobStatus.SETTLED


def test_a_refund_webhook_is_persisted_to_postgres(pg_store):
    from pact.core.webhook_store import PostgresWebhookStore
    from pact.core.webhooks import process_event

    _seed(pg_store, EscrowState.RELEASED, "CAP-PERSIST", JobStatus.SETTLED)
    webhooks = PostgresWebhookStore(DATABASE_URL)

    outcome, _ = process_event(
        payload={
            "id": "WH-PERSIST-REFUND",
            "event_type": "PAYMENT.CAPTURE.REFUNDED",
            "resource": {
                "id": "REF-PERSIST",
                "status": "REFUNDED",
                "custom_id": "job_persist",
                "amount": {"value": "50.00", "currency_code": "USD"},
            },
        },
        verified=True,
        store=webhooks,
        get_job=pg_store.get,
        save_job=pg_store.save,
        job_guard=_guard(pg_store),
    )

    assert outcome == "applied"
    from pact.core.dbstore import PostgresJobStore

    reloaded = PostgresJobStore(DATABASE_URL).get("job_persist")
    assert reloaded.escrow.state is EscrowState.REFUNDED, "refund was not persisted"
    assert reloaded.status is JobStatus.REFUNDED


def test_a_refund_webhook_does_not_double_count_a_refund_pact_recorded(pg_store):
    """A refund PACT's API recorded and its matching webhook are one refund, not two.

    PACT's Refund API records a refund and the amount it moved; PayPal then delivers a
    ``PAYMENT.CAPTURE.REFUNDED`` webhook whose ``resource.id`` is that same refund id.
    Counting both would make a *partial* refund read as a full reversal. The webhook
    must recognise the id it already counted and not add it again.
    """
    from pact.core.dbstore import PostgresJobStore
    from pact.core.webhooks import process_event

    from pact.core.webhook_store import PostgresWebhookStore

    # PACT's API already recorded refund REF-PERSIST for 50 of a 100 capture.
    job = Job(id="job_persist", title="t", brief="b", criteria=["c"], budget_max=Decimal("200"))
    job.escrow.amount = Decimal("100.00")
    job.escrow.currency = "USD"
    job.escrow.state = EscrowState.RELEASED
    job.status = JobStatus.SETTLED
    job.escrow.capture_id = "CAP-PERSIST"
    job.escrow.refunded_amount = Decimal("50.00")
    job.escrow.refund_id = "REF-PERSIST"
    job.escrow.refund_ids = ["REF-PERSIST"]
    pg_store.save(job)

    webhooks = PostgresWebhookStore(DATABASE_URL)
    process_event(
        payload={
            "id": "WH-PERSIST-DOUBLECOUNT",
            "event_type": "PAYMENT.CAPTURE.REFUNDED",
            "resource": {
                "id": "REF-PERSIST",
                "status": "REFUNDED",
                "custom_id": "job_persist",
                "amount": {"value": "50.00", "currency_code": "USD"},
            },
        },
        verified=True,
        store=webhooks,
        get_job=pg_store.get,
        save_job=pg_store.save,
        job_guard=_guard(pg_store),
    )

    reloaded = PostgresJobStore(DATABASE_URL).get("job_persist")
    assert reloaded.escrow.refunded_amount == Decimal("50.00"), (
        "the matching webhook double-counted a refund PACT already recorded"
    )
    assert reloaded.escrow.state is EscrowState.RELEASED, "a partial refund must not flip the job"


def test_a_refund_counted_by_its_webhook_is_not_counted_again_by_the_api(pg_store):
    """The crash window: the provider applied a refund, but PACT persisted nothing.

    PACT calls the Refund API; PayPal applies the refund and assigns a refund id, but
    the API instance crashes (or the write fails) before the row is updated — so
    ``refunded_amount`` and ``refund_ids`` are still empty. PayPal then delivers the
    ``PAYMENT.CAPTURE.REFUNDED`` webhook for that refund, which counts it. When the
    refund is retried, PayPal returns the *same* refund for the same idempotency key
    (it does not move money again). If the API adds the amount a second time, PACT
    reports a 50-of-100 refund as a full 100 reversal while only 50 left the account.

    Requires the webhook to record the refund id it counted, and the API to treat a
    refund id already recorded as already counted.
    """
    from pact.core.dbstore import PostgresJobStore
    from pact.core.webhook_store import PostgresWebhookStore
    from pact.core.webhooks import process_event

    from tests.test_refund import IdempotentProvider

    provider = IdempotentProvider(capture_id="CAP-CRASH", amount="100.00")
    job = _seed(pg_store, EscrowState.RELEASED, "CAP-CRASH", JobStatus.SETTLED)
    job.escrow.amount = Decimal("100.00")
    pg_store.save(job)

    # The provider applies the refund (as the API call would), then PACT "crashes":
    # the row is left untouched, so refund_sequence is still 0 and the retry reuses
    # the same idempotency key.
    first = provider.refund_capture("CAP-CRASH", amount=Decimal("50.00"), currency="USD",
                                    idempotency_key="pact-refund-job_persist-1")
    assert provider.capture_refunded_value() == Decimal("50.00")

    webhooks = PostgresWebhookStore(DATABASE_URL)
    process_event(
        payload={
            "id": "WH-CRASH-1",
            "event_type": "PAYMENT.CAPTURE.REFUNDED",
            "resource": {"id": first["id"], "status": "COMPLETED",
                         "custom_id": "job_persist",
                         "amount": {"value": "50.00", "currency_code": "USD"}},
        },
        verified=True, store=webhooks, get_job=pg_store.get, save_job=pg_store.save,
        job_guard=_guard(pg_store),
    )
    after_webhook = PostgresJobStore(DATABASE_URL).get("job_persist")
    assert after_webhook.escrow.refunded_amount == Decimal("50.00")
    assert first["id"] in after_webhook.escrow.refund_ids

    # The retry: same key -> same provider refund -> must not count again.
    from pact.core.settlement import SettlementEngine
    from pact.core.store import ReputationLedger
    import tempfile
    from pathlib import Path

    engine = SettlementEngine(
        provider, ReputationLedger(Path(tempfile.mkdtemp()) / "rep.json"), store=pg_store
    )
    retried = engine.refund_capture(pg_store.get("job_persist"), amount=Decimal("50.00"))

    assert retried.escrow.refunded_amount == Decimal("50.00"), (
        "the refund was counted twice across the crash window"
    )
    final = PostgresJobStore(DATABASE_URL).get("job_persist")
    assert final.escrow.refunded_amount == Decimal("50.00")
    assert final.escrow.state is EscrowState.RELEASED, "a 50-of-100 refund must not read as full"
    assert provider.capture_refunded_value() == Decimal("50.00"), "provider moved only 50"
