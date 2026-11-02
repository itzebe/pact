"""Webhook authenticity, idempotency, and payment-state mapping.

These use the in-memory webhook store and a scripted escrow client, so they are
*mocked-API* tests: they prove PACT's own logic (verify-then-apply, exactly-once,
state mapping) without a live PayPal call. The cross-instance and real-PostgreSQL
properties live in ``test_webhook_postgres.py``; the signature call itself against
the real client's HTTP layer lives in ``test_paypal_client.py``.
"""

from __future__ import annotations

from decimal import Decimal

from pact.core.models import EscrowState, Job, JobStatus
from pact.core.webhook_store import InMemoryWebhookStore, WebhookRecord
from pact.core.webhooks import (
    CAPTURE_COMPLETED,
    CAPTURE_DENIED,
    CAPTURE_REFUNDED,
    HANDLED_EVENT_TYPES,
    WebhookOutcome,
    extract_event,
    process_event,
)


def make_job(state: EscrowState = EscrowState.HELD, **escrow) -> Job:
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
    job.escrow.state = state
    if state is EscrowState.HELD:
        job.status = JobStatus.ESCROWED
    elif state is EscrowState.UNRESOLVED:
        job.status = JobStatus.UNRESOLVED
    elif state is EscrowState.RELEASED:
        job.status = JobStatus.SETTLED
    for key, value in escrow.items():
        setattr(job.escrow, key, value)
    return job


class FakeStore:
    """A jobs dict with the two methods the webhook processor needs."""

    def __init__(self, jobs: dict[str, Job]) -> None:
        self.jobs = jobs
        self.saves: list[str] = []

    def get(self, job_id: str):
        return self.jobs.get(job_id)

    def save(self, job: Job) -> None:
        self.jobs[job.id] = job
        self.saves.append(job.id)


def capture_event(event_id: str, job_id: str, *, value="120.00", currency="USD",
                  status="COMPLETED", capture_id="CAP-1", event_type=CAPTURE_COMPLETED) -> dict:
    return {
        "id": event_id,
        "event_type": event_type,
        "resource": {
            "id": capture_id,
            "status": status,
            "custom_id": job_id,
            "amount": {"currency_code": currency, "value": value},
        },
    }


def run(payload, jobs, store=None, verified=True):
    fake = FakeStore(jobs)
    outcome, job = process_event(
        payload=payload, verified=verified, store=store or InMemoryWebhookStore(),
        get_job=fake.get, save_job=fake.save,
    )
    return outcome, job, fake


# --------------------------------------------------------------- extraction


def test_a_malformed_payload_is_rejected():
    assert extract_event("not-a-dict") is None
    assert extract_event({}) is None
    assert extract_event({"id": "e1"}) is None  # no event_type
    assert extract_event({"event_type": "x"}) is None  # no id


def test_the_job_id_is_read_from_the_capture_custom_id():
    record = extract_event(capture_event("e1", "job_abc"))
    assert record is not None
    assert record.job_id == "job_abc"


def test_the_job_id_is_read_from_an_order_purchase_unit():
    payload = {
        "id": "e2",
        "event_type": "CHECKOUT.ORDER.APPROVED",
        "resource": {"purchase_units": [{"custom_id": "job_xyz"}]},
    }
    assert extract_event(payload).job_id == "job_xyz"


# --------------------------------------------------------------- authenticity


def test_an_unverified_event_is_refused_and_never_claimed():
    job = make_job()
    store = InMemoryWebhookStore()
    outcome, touched, fake = run(capture_event("e1", job.id), {job.id: job}, store=store,
                                 verified=False)
    assert outcome == WebhookOutcome.UNVERIFIED
    assert touched is None
    assert fake.saves == []
    assert job.escrow.state is EscrowState.HELD  # nothing changed
    # Not claimed, so a later legitimate delivery of the same id still applies.
    assert store.claim(WebhookRecord("e1", CAPTURE_COMPLETED)) is True


def test_an_unverified_event_cannot_settle_a_job():
    job = make_job()
    run(capture_event("e1", job.id), {job.id: job}, verified=False)
    assert job.status is JobStatus.ESCROWED


# --------------------------------------------------------------- idempotency


def test_a_first_capture_event_settles_the_job():
    job = make_job()
    outcome, touched, fake = run(capture_event("e1", job.id), {job.id: job})
    assert outcome == WebhookOutcome.APPLIED
    assert touched is job
    assert job.escrow.state is EscrowState.RELEASED
    assert job.status is JobStatus.SETTLED
    assert job.escrow.capture_id == "CAP-1"
    assert fake.saves == [job.id]


def test_a_duplicate_capture_event_does_not_settle_twice():
    job = make_job()
    store = InMemoryWebhookStore()
    jobs = {job.id: job}
    first, _, _ = run(capture_event("e1", job.id), jobs, store=store)
    entries_after_first = len(job.ledger)
    second, _, fake = run(capture_event("e1", job.id), jobs, store=store)
    assert first == WebhookOutcome.APPLIED
    assert second == WebhookOutcome.DUPLICATE
    assert len(job.ledger) == entries_after_first, "a duplicate must not append a second release"
    assert fake.saves == []


def test_two_different_events_for_one_capture_apply_once():
    """A redelivery under a new event id must still not double-settle."""
    job = make_job()
    store = InMemoryWebhookStore()
    jobs = {job.id: job}
    run(capture_event("e1", job.id), jobs, store=store)
    settled_ledger = len(job.ledger)
    # A second, distinct event id describing the same capture.
    outcome, _, _ = run(capture_event("e2", job.id), jobs, store=store)
    # The job is already RELEASED with the same capture id, so it is a no-op.
    assert outcome == WebhookOutcome.IGNORED
    assert len(job.ledger) == settled_ledger


# --------------------------------------------------------------- relevance


def test_an_event_for_an_unknown_job_is_acknowledged_not_applied():
    outcome, touched, fake = run(capture_event("e1", "job_unknown"), {})
    assert outcome == WebhookOutcome.UNKNOWN_JOB
    assert touched is None
    assert fake.saves == []


def test_an_unknown_event_type_is_acknowledged_and_ignored():
    job = make_job()
    payload = {"id": "e1", "event_type": "SOME.FUTURE.EVENT", "resource": {"custom_id": job.id}}
    outcome, touched, fake = run(payload, {job.id: job})
    assert outcome == WebhookOutcome.IGNORED
    assert job.escrow.state is EscrowState.HELD
    assert fake.saves == []


def test_every_handled_type_is_a_string_constant():
    assert CAPTURE_COMPLETED in HANDLED_EVENT_TYPES
    assert all(isinstance(t, str) for t in HANDLED_EVENT_TYPES)


# --------------------------------------------------------------- amount/currency


def test_a_capture_for_the_wrong_amount_is_not_settled():
    job = make_job()
    outcome, _, _ = run(capture_event("e1", job.id, value="999.00"), {job.id: job})
    assert outcome == WebhookOutcome.APPLIED  # recorded, but...
    assert job.escrow.state is EscrowState.HELD  # ...not settled
    assert job.status is not JobStatus.SETTLED
    assert any(e.kind == "webhook_amount_mismatch" for e in job.ledger)


def test_a_capture_in_the_wrong_currency_is_not_settled():
    job = make_job()
    run(capture_event("e1", job.id, currency="EUR"), {job.id: job})
    assert job.escrow.state is EscrowState.HELD
    assert any(e.kind == "webhook_currency_mismatch" for e in job.ledger)


def test_a_capture_event_that_is_not_completed_does_not_settle():
    job = make_job()
    run(capture_event("e1", job.id, status="PENDING"), {job.id: job})
    assert job.escrow.state is EscrowState.HELD
    assert job.status is not JobStatus.SETTLED


# --------------------------------------------------------------- unexpected states


def test_a_capture_event_while_awaiting_approval_is_not_a_settlement():
    job = make_job(EscrowState.AWAITING_APPROVAL)
    outcome, _, _ = run(capture_event("e1", job.id), {job.id: job})
    assert outcome == WebhookOutcome.APPLIED
    assert job.escrow.state is EscrowState.AWAITING_APPROVAL
    assert any(e.kind == "webhook_capture_unexpected" for e in job.ledger)


# --------------------------------------------------------------- refunds / voids


def test_a_refund_event_moves_a_released_job_to_refunded():
    job = make_job(EscrowState.RELEASED, capture_id="CAP-1")
    run(capture_event("e1", job.id, event_type=CAPTURE_REFUNDED), {job.id: job})
    assert job.escrow.state is EscrowState.REFUNDED
    assert job.status is JobStatus.REFUNDED


def test_a_denied_capture_on_a_held_job_clears_the_claim():
    job = make_job()
    run(capture_event("e1", job.id, event_type=CAPTURE_DENIED), {job.id: job})
    assert job.escrow.capture_status == "DENIED"
    assert job.escrow.capture_id == ""
    assert job.escrow.state is EscrowState.HELD


def test_a_denial_after_release_does_not_flip_a_settlement():
    job = make_job(EscrowState.RELEASED, capture_id="CAP-1")
    run(capture_event("e1", job.id, event_type=CAPTURE_DENIED), {job.id: job})
    assert job.escrow.state is EscrowState.RELEASED
    assert any(e.kind == "webhook_capture_denied_after_release" for e in job.ledger)


def test_an_authorization_voided_event_corrects_a_held_job():
    job = make_job()
    payload = {
        "id": "e1",
        "event_type": "PAYMENT.AUTHORIZATION.VOIDED",
        "resource": {"id": "AUTH-1", "custom_id": job.id},
    }
    outcome, _, _ = run(payload, {job.id: job})
    assert outcome == WebhookOutcome.APPLIED
    assert job.escrow.state is EscrowState.VOIDED
    assert job.status is JobStatus.VOIDED


def test_an_authorization_expired_event_corrects_a_held_job():
    job = make_job()
    payload = {
        "id": "e1",
        "event_type": "PAYMENT.AUTHORIZATION.EXPIRED",
        "resource": {"id": "AUTH-1", "custom_id": job.id},
    }
    run(payload, {job.id: job})
    assert job.escrow.state is EscrowState.VOIDED


def test_an_order_approved_event_does_not_authorize_by_itself():
    """Approval is not capture, and a webhook must not hold funds on its own."""
    job = make_job(EscrowState.AWAITING_APPROVAL)
    payload = {
        "id": "e1",
        "event_type": "CHECKOUT.ORDER.APPROVED",
        "resource": {"purchase_units": [{"custom_id": job.id}]},
    }
    outcome, _, fake = run(payload, {job.id: job})
    assert outcome == WebhookOutcome.IGNORED
    assert job.escrow.state is EscrowState.AWAITING_APPROVAL
    assert fake.saves == []


# --------------------------------------------------------------- store semantics


def test_the_in_memory_store_claims_once_and_reports_duplicates():
    store = InMemoryWebhookStore()
    record = WebhookRecord("e1", CAPTURE_COMPLETED, "job_1")
    assert store.claim(record) is True
    assert store.claim(record) is False
    assert store.claim(WebhookRecord("e2", CAPTURE_COMPLETED)) is True


def test_a_ledger_entry_never_carries_the_raw_payload():
    job = make_job()
    run(capture_event("e1", job.id), {job.id: job})
    released = [e for e in job.ledger if e.kind == "escrow_released"]
    assert released
    detail = released[-1].detail
    assert "resource" not in detail
    assert "payer" not in detail


# ------------------------------------------------------- partial refund events


def test_a_partial_refund_event_does_not_flip_a_settlement():
    """A partial refund is real money, but it does not reverse the whole capture."""
    job = make_job(EscrowState.RELEASED, capture_id="CAP-1", amount=Decimal("120.00"))
    payload = capture_event("e1", job.id, value="40.00", event_type=CAPTURE_REFUNDED)
    run(payload, {job.id: job})
    assert job.escrow.state is EscrowState.RELEASED
    assert job.escrow.refunded_amount == Decimal("40.00")
    entry = [e for e in job.ledger if e.kind == "escrow_refunded"][-1]
    assert entry.detail["partial"] is True


def test_partial_refund_events_accumulate_to_a_full_refund():
    job = make_job(EscrowState.RELEASED, capture_id="CAP-1", amount=Decimal("120.00"))
    # Two *distinct* refunds: PayPal assigns a new refund id per refund, so reusing
    # one id here would model the same refund reported twice, which must not count.
    run(capture_event("e1", job.id, value="40.00", event_type=CAPTURE_REFUNDED,
                      capture_id="REF-A"), {job.id: job})
    run(capture_event("e2", job.id, value="80.00", event_type=CAPTURE_REFUNDED,
                      capture_id="REF-B"), {job.id: job})
    assert job.escrow.refunded_amount == Decimal("120.00")
    assert job.escrow.state is EscrowState.REFUNDED
    assert job.status is JobStatus.REFUNDED


def test_the_same_refund_event_twice_is_counted_once():
    """A redelivered refund event must not accumulate twice (belt and braces: the
    event store also dedupes on event id, but the refund id is the money identity)."""
    job = make_job(EscrowState.RELEASED, capture_id="CAP-1", amount=Decimal("120.00"))
    payload = capture_event("e1", job.id, value="40.00", event_type=CAPTURE_REFUNDED,
                            capture_id="REF-A")
    run(payload, {job.id: job})
    # A second event with a *different* event id but the same refund id.
    run(capture_event("e2", job.id, value="40.00", event_type=CAPTURE_REFUNDED,
                      capture_id="REF-A"), {job.id: job})
    assert job.escrow.refunded_amount == Decimal("40.00"), "one refund was counted twice"


def test_a_webhook_refund_already_recorded_by_pact_is_not_counted_twice():
    """PACT's own refund and its matching webhook are one refund, not two.

    The Refund API records the refund id and the amount; PayPal then reports the same
    refund as ``PAYMENT.CAPTURE.REFUNDED`` with that id as ``resource.id``. Counting
    both would turn a 40-of-120 partial refund into a full reversal.
    """
    job = make_job(EscrowState.RELEASED, capture_id="CAP-1", amount=Decimal("120.00"))
    job.escrow.refunded_amount = Decimal("40.00")
    job.escrow.refund_id = "REF-API-1"
    job.escrow.refund_ids = ["REF-API-1"]

    run(capture_event("e1", job.id, value="40.00", event_type=CAPTURE_REFUNDED,
                      capture_id="REF-API-1"), {job.id: job})

    assert job.escrow.refunded_amount == Decimal("40.00"), "the refund was counted twice"
    assert job.escrow.state is EscrowState.RELEASED
    entry = [e for e in job.ledger if e.kind == "webhook_refund_already_recorded"]
    assert entry, "the duplicate should be recorded, not silently dropped"


def test_a_distinct_webhook_refund_is_still_counted():
    """Dedupe must not swallow a refund PACT did not initiate itself."""
    job = make_job(EscrowState.RELEASED, capture_id="CAP-1", amount=Decimal("120.00"))
    job.escrow.refunded_amount = Decimal("40.00")
    job.escrow.refund_id = "REF-API-1"
    job.escrow.refund_ids = ["REF-API-1"]

    run(capture_event("e1", job.id, value="80.00", event_type=CAPTURE_REFUNDED,
                      capture_id="REF-OTHER-2"), {job.id: job})

    assert job.escrow.refunded_amount == Decimal("120.00")
    assert job.escrow.state is EscrowState.REFUNDED


def test_a_reversed_capture_is_a_full_reversal_regardless_of_amount():
    job = make_job(EscrowState.RELEASED, capture_id="CAP-1", amount=Decimal("120.00"))
    from pact.core.webhooks import CAPTURE_REVERSED

    run(capture_event("e1", job.id, value="120.00", event_type=CAPTURE_REVERSED), {job.id: job})
    assert job.escrow.state is EscrowState.REFUNDED
