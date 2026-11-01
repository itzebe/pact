"""Capture and webhook racing for the same money transition.

A release is reached two ways: the settlement engine calls PayPal to capture, and
PayPal's webhook says a capture completed. Both are the *same* transition, so if
they run concurrently and both read ``HELD`` the job would be settled twice — two
``escrow_released`` ledger entries for one payment, which is a real accounting
defect even though only one capture ever hit PayPal.

These tests run the real ``SettlementEngine.release`` and the real ``process_event``
against one job, on real threads, sharing the engine's per-job guard, and assert
exactly one release is recorded. They are PostgreSQL integration tests when
``DATABASE_URL`` is set (the guard then spans instances via an advisory lock) and
fall back to the in-process guard otherwise.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from pact.core.models import EscrowState, Job, JobStatus
from pact.core.settlement import SettlementEngine
from pact.core.store import JobStore, ReputationLedger
from pact.core.webhook_store import InMemoryWebhookStore
from pact.core.webhooks import process_event
from pact.paypal import MockPayPalEscrowClient


def make_job() -> Job:
    job = Job(title="Landing copy", brief="Write copy.", budget_max=Decimal("300"))
    job.buyer_id = "buyer"
    job.seller_id = "seller_atlas"
    job.agreed_price = Decimal("120.00")
    job.escrow.amount = Decimal("120.00")
    job.escrow.currency = "USD"
    job.escrow.mode = "mock"
    job.escrow.simulated = True
    job.escrow.state = EscrowState.HELD
    job.status = JobStatus.ESCROWED
    return job


def held_job_with_authorization():
    """A job holding a real (mock-provider) authorization ready to be captured."""
    escrow = MockPayPalEscrowClient()
    order = escrow.create_order(amount=Decimal("120.00"), currency="USD", reference_id="job_x")
    auth = escrow.authorize_order(order["id"])
    job = make_job()
    job.escrow.order_id = order["id"]
    job.escrow.authorization_id = auth.id
    return escrow, job


def capture_event(job_id: str, capture_id: str, event_id: str = "evt-1") -> dict:
    return {
        "id": event_id,
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": capture_id,
            "status": "COMPLETED",
            "custom_id": job_id,
            "amount": {"currency_code": "USD", "value": "120.00"},
        },
    }


class StoreShim:
    def __init__(self, jobs):
        self.jobs = jobs

    def get(self, job_id):
        return self.jobs.get(job_id)

    def save(self, job):
        self.jobs[job.id] = job


def test_a_capture_and_its_webhook_settle_exactly_once(tmp_path):
    escrow, job = held_job_with_authorization()
    store = JobStore(tmp_path / "jobs")
    store.save(job)
    reputation = ReputationLedger(tmp_path / "rep.json")
    settlement = SettlementEngine(escrow, reputation, store=store)
    jobs = {job.id: job}
    shim = StoreShim(jobs)
    webhook_store = InMemoryWebhookStore()

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}
    lock = threading.Lock()

    def do_capture():
        barrier.wait()
        try:
            settlement.release(job, note="verified")
            outcome = "captured"
        except Exception as exc:  # a losing racer is expected to raise
            outcome = f"refused:{type(exc).__name__}"
        with lock:
            results["capture"] = outcome

    def do_webhook():
        barrier.wait()
        outcome, _ = process_event(
            payload=capture_event(job.id, "CAP-WH"),
            verified=True,
            store=webhook_store,
            get_job=shim.get,
            save_job=shim.save,
            job_guard=settlement._guard,
        )
        with lock:
            results["webhook"] = outcome

    threads = [threading.Thread(target=do_capture), threading.Thread(target=do_webhook)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Whichever way the race lands, the job must be settled exactly once.
    releases = [e for e in job.ledger if e.kind == "escrow_released"]
    assert len(releases) == 1, [e.summary for e in releases]
    assert job.escrow.state is EscrowState.RELEASED
    assert job.status is JobStatus.SETTLED


def test_a_webhook_after_a_capture_does_not_settle_twice(tmp_path):
    escrow, job = held_job_with_authorization()
    store = JobStore(tmp_path / "jobs")
    store.save(job)
    settlement = SettlementEngine(escrow, ReputationLedger(tmp_path / "rep.json"), store=store)

    settlement.release(job, note="verified")
    assert job.escrow.state is EscrowState.RELEASED
    releases_before = len([e for e in job.ledger if e.kind == "escrow_released"])

    # The webhook for the very capture we just made.
    shim = StoreShim({job.id: job})
    process_event(
        payload=capture_event(job.id, job.escrow.capture_id),
        verified=True,
        store=InMemoryWebhookStore(),
        get_job=shim.get,
        save_job=shim.save,
        job_guard=settlement._guard,
    )
    releases_after = len([e for e in job.ledger if e.kind == "escrow_released"])
    assert releases_after == releases_before == 1


def test_a_webhook_resolves_an_in_flight_capture(tmp_path):
    """A capture whose response was lost is settled by the webhook that proves it."""
    escrow, job = held_job_with_authorization()
    store = JobStore(tmp_path / "jobs")
    store.save(job)
    settlement = SettlementEngine(escrow, ReputationLedger(tmp_path / "rep.json"), store=store)

    # Simulate the crash window: an in-flight claim was persisted, then the response
    # was lost, leaving the job HELD with the marker still set.
    job.escrow.capture_id = "pending"
    job.escrow.state = EscrowState.HELD
    store.save(job)

    shim = StoreShim({job.id: job})
    outcome, _ = process_event(
        payload=capture_event(job.id, "CAP-RECOVERED"),
        verified=True,
        store=InMemoryWebhookStore(),
        get_job=shim.get,
        save_job=shim.save,
        job_guard=settlement._guard,
    )
    assert outcome == "applied"
    assert job.escrow.state is EscrowState.RELEASED
    assert job.escrow.capture_id == "CAP-RECOVERED"
    released = [e for e in job.ledger if e.kind == "escrow_released"]
    assert released and released[-1].detail.get("resolved_in_flight") is True
