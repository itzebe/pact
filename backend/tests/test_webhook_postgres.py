"""Webhook exactly-once processing on real PostgreSQL, across instances.

The in-memory registry in ``test_webhooks.py`` is correct for one process. The whole
point of a durable webhook store is the case it cannot model: PayPal redelivers the
same event, and the two deliveries land on *different* instances. Each would see an
empty local set and each would apply the event, so a duplicate would settle twice.

These tests use two independent ``PostgresWebhookStore`` objects (two connections,
standing in for two processes) against a real PostgreSQL 16 and prove exactly one
claim wins — including under true concurrency. They are skipped unless
``DATABASE_URL`` points at a reachable Postgres, because an in-memory double cannot
model the property under test.

Run with:
    DATABASE_URL=postgresql://... pytest tests/test_webhook_postgres.py -v
"""

from __future__ import annotations

import os
import threading
from decimal import Decimal

import pytest

from pact.core.models import EscrowState, Job, JobStatus
from pact.core.webhook_store import (
    PostgresWebhookStore,
    WebhookRecord,
)
from pact.core.webhooks import CAPTURE_COMPLETED, WebhookOutcome, process_event

DATABASE_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set; Postgres tests are opt-in"
)


def _truncate() -> None:
    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS processed_webhooks ("
            "event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, job_id TEXT, "
            "received_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "processed_at TIMESTAMPTZ, outcome TEXT NOT NULL DEFAULT 'received')"
        )
        cur.execute("TRUNCATE processed_webhooks")


@pytest.fixture()
def store_a() -> PostgresWebhookStore:
    _truncate()
    return PostgresWebhookStore(DATABASE_URL)


@pytest.fixture()
def store_b() -> PostgresWebhookStore:
    return PostgresWebhookStore(DATABASE_URL)


def make_job() -> Job:
    job = Job(title="Landing copy", brief="Write copy.", budget_max=Decimal("300"))
    job.buyer_id = "buyer"
    job.seller_id = "seller_atlas"
    job.agreed_price = Decimal("120.00")
    job.escrow.amount = Decimal("120.00")
    job.escrow.currency = "USD"
    job.escrow.mode = "sandbox"
    job.escrow.simulated = False
    job.escrow.order_id = "ORDER-1"
    job.escrow.authorization_id = "AUTH-1"
    job.escrow.state = EscrowState.HELD
    job.status = JobStatus.ESCROWED
    return job


class FakeStore:
    def __init__(self, jobs):
        self.jobs = jobs
        self.saves = []

    def get(self, job_id):
        return self.jobs.get(job_id)

    def save(self, job):
        self.jobs[job.id] = job
        self.saves.append(job.id)


def capture_event(event_id: str, job_id: str) -> dict:
    return {
        "id": event_id,
        "event_type": CAPTURE_COMPLETED,
        "resource": {
            "id": "CAP-1",
            "status": "COMPLETED",
            "custom_id": job_id,
            "amount": {"currency_code": "USD", "value": "120.00"},
        },
    }


# --------------------------------------------------------------- single store


def test_the_first_claim_wins_and_the_second_is_a_duplicate(store_a):
    record = WebhookRecord("evt-1", CAPTURE_COMPLETED, "job_1")
    assert store_a.claim(record) is True
    assert store_a.claim(record) is False


def test_a_second_store_instance_refuses_an_already_claimed_event(store_a, store_b):
    record = WebhookRecord("evt-1", CAPTURE_COMPLETED, "job_1")
    assert store_a.claim(record) is True
    # A *different* instance, on its own connection, sees the claim.
    assert store_b.claim(record) is False


def test_distinct_events_do_not_collide(store_a, store_b):
    assert store_a.claim(WebhookRecord("evt-1", CAPTURE_COMPLETED)) is True
    assert store_b.claim(WebhookRecord("evt-2", CAPTURE_COMPLETED)) is True


# --------------------------------------------------------- true concurrency


def test_two_independent_instances_allow_exactly_one_claim(store_a, store_b):
    """The same event, claimed at the same instant by two instances, admits one."""
    record = WebhookRecord("evt-race", CAPTURE_COMPLETED, "job_1")
    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def worker(store):
        barrier.wait()
        won = store.claim(record)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=worker, args=(store_a,)),
               threading.Thread(target=worker, args=(store_b,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [False, True], results


def test_many_instances_racing_one_event_admit_exactly_one():
    record = WebhookRecord("evt-many", CAPTURE_COMPLETED, "job_1")
    barrier = threading.Barrier(8)
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        store = PostgresWebhookStore(DATABASE_URL)
        barrier.wait()
        won = store.claim(record)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1, results
    assert results.count(False) == 7, results


def test_a_duplicate_event_never_settles_twice_across_instances(store_a, store_b):
    """The end-to-end property: one event delivered to two instances, one settlement."""
    job = make_job()
    jobs = {job.id: job}
    fake = FakeStore(jobs)
    payload = capture_event("evt-dup", job.id)

    first, _ = process_event(payload=payload, verified=True, store=store_a,
                             get_job=fake.get, save_job=fake.save)
    entries_after_first = len(job.ledger)
    second, _ = process_event(payload=payload, verified=True, store=store_b,
                              get_job=fake.get, save_job=fake.save)

    assert first == WebhookOutcome.APPLIED
    assert second == WebhookOutcome.DUPLICATE
    assert job.escrow.state is EscrowState.RELEASED
    assert len(job.ledger) == entries_after_first
    assert fake.saves == [job.id]


def test_concurrent_capture_and_duplicate_across_instances(store_a, store_b):
    """A capture and its redelivery arriving at once still produce one settlement."""
    job = make_job()
    jobs = {job.id: job}
    fake = FakeStore(jobs)
    payload = capture_event("evt-concurrent", job.id)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(store):
        barrier.wait()
        outcome, _ = process_event(payload=payload, verified=True, store=store,
                                   get_job=fake.get, save_job=fake.save)
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker, args=(store_a,)),
               threading.Thread(target=worker, args=(store_b,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == [WebhookOutcome.APPLIED, WebhookOutcome.DUPLICATE], outcomes
    assert job.escrow.state is EscrowState.RELEASED
    assert sum(1 for e in job.ledger if e.kind == "escrow_released") == 1


# --------------------------------------------------------------- retention


def test_an_expired_record_is_pruned_so_the_table_stays_bounded(store_a):
    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO processed_webhooks (event_id, event_type, received_at) "
            "VALUES (%s, %s, now() - make_interval(days => 400))",
            ("evt-old", CAPTURE_COMPLETED),
        )
        cur.execute(
            "INSERT INTO processed_webhooks (event_id, event_type) VALUES (%s, %s)",
            ("evt-new", CAPTURE_COMPLETED),
        )

    removed = store_a.prune()
    assert removed >= 1

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT event_id FROM processed_webhooks")
        remaining = {row[0] for row in cur.fetchall()}
    assert "evt-old" not in remaining
    assert "evt-new" in remaining


def test_pruning_removes_at_most_one_batch(store_a):
    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        for i in range(5):
            cur.execute(
                "INSERT INTO processed_webhooks (event_id, event_type, received_at) "
                "VALUES (%s, %s, now() - make_interval(days => 400))",
                (f"evt-old-{i}", CAPTURE_COMPLETED),
            )

    removed = store_a.prune(limit=2)
    assert removed == 2


def test_a_fresh_record_is_never_pruned(store_a):
    store_a.claim(WebhookRecord("evt-fresh", CAPTURE_COMPLETED, "job_1"))
    store_a.prune()
    assert store_a.claim(WebhookRecord("evt-fresh", CAPTURE_COMPLETED, "job_1")) is False


def test_the_table_is_created_on_a_brand_new_database():
    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS processed_webhooks")
    # Constructing the store must recreate the schema.
    store = PostgresWebhookStore(DATABASE_URL)
    assert store.claim(WebhookRecord("evt-new-db", CAPTURE_COMPLETED)) is True


def test_an_unreachable_store_fails_closed():
    from pact.core.webhook_store import UnavailableWebhookStore

    store = UnavailableWebhookStore(RuntimeError("db down"))
    assert store.claim(WebhookRecord("evt-1", CAPTURE_COMPLETED)) is False


def test_an_event_id_is_never_written_to_the_log(store_a, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        store_a.claim(WebhookRecord("evt-secret-id-xyz", CAPTURE_COMPLETED, "job_1"))
    assert "evt-secret-id-xyz" not in caplog.text
