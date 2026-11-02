"""The refund path: returning money PayPal has already captured.

A refund is the one operation that moves money *away* from a seller after a
settlement, so the guarantees are the mirror image of the capture guarantees:

* it is only reachable from a settled (captured) escrow, never a bare hold;
* the sum of every confirmed refund can never exceed the captured amount;
* a lost response leaves the job unresolved rather than reporting a refund that
  may not have landed;
* a repeated request cannot refund twice.

These use a scripted escrow double built on the real mock, so the state machine
under test is the production one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pact.core.engine import PactEngine
from pact.core.llm import MockLLM
from pact.core.models import EscrowState, JobStatus
from pact.core.store import JobStore, ReputationLedger
from pact.paypal import AmbiguousOutcome, MockPayPalEscrowClient, PayPalError


class ScriptedEscrow(MockPayPalEscrowClient):
    """The mock, with refund outcomes scriptable per call."""

    def __init__(self) -> None:
        super().__init__()
        self.raise_on_refund: Exception | None = None
        self.force_refund_status: str | None = None
        self.refund_calls: list[str | None] = []

    def refund_capture(self, capture_id, *, amount=None, currency=None, idempotency_key=None):
        self.refund_calls.append(idempotency_key)
        if self.raise_on_refund is not None:
            raise self.raise_on_refund
        body = super().refund_capture(
            capture_id, amount=amount, currency=currency, idempotency_key=idempotency_key
        )
        if self.force_refund_status:
            body["status"] = self.force_refund_status
        return body


@pytest.fixture()
def engine(tmp_path) -> PactEngine:
    return PactEngine(
        llm=MockLLM(), escrow=ScriptedEscrow(),
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "reputation.json"),
    )


class IdempotentProvider(MockPayPalEscrowClient):
    """The mock, with PayPal's real idempotency-key semantics.

    The production mock assigns a fresh refund id on every call and ignores the
    idempotency key, so it cannot model a *retry*: PayPal returns the original refund
    for a repeated key and does not move money a second time. Tests that exercise a
    lost response, a crash between the provider applying a refund and the write, or a
    client retry need this behaviour or they silently model a second real refund.
    """

    def __init__(self, capture_id: str = "CAP-IDEM", amount: str = "100.00") -> None:
        super().__init__()
        self.refund_calls: list[str | None] = []
        order = self.create_order(amount=Decimal(amount), currency="USD", reference_id="seed")
        auth = self.authorize_order(order["id"])
        cap = self.capture_authorization(auth.id, amount=Decimal(amount), currency="USD")
        self._seed_capture_id = capture_id
        # Re-key the capture so the caller can refer to it by a stable id.
        self._captures[capture_id] = self._captures.pop(cap.id)
        self._captures[capture_id]["id"] = capture_id
        self._by_key: dict[str, dict] = {}

    def refund_capture(self, capture_id, *, amount=None, currency=None, idempotency_key=None):
        self.refund_calls.append(idempotency_key)
        if idempotency_key in self._by_key:
            return dict(self._by_key[idempotency_key])
        body = super().refund_capture(capture_id, amount=amount, currency=currency,
                                      idempotency_key=idempotency_key)
        self._by_key[idempotency_key] = dict(body)
        return body

    def capture_refunded_value(self) -> Decimal:
        return Decimal(self._captures[self._seed_capture_id].get("refunded_value", "0.00"))


def settled_job(engine: PactEngine, amount: str = "120.00"):
    """A job driven to a completed capture, ready to be refunded."""
    job = engine.create_job(
        title="Copy", brief="Write copy.", criteria=["A headline"], budget_max=Decimal("300")
    )
    job.seller_id = "seller_nova"
    job.agreed_price = Decimal(amount)
    engine.settlement.hold(job)
    engine.settlement.release(job)
    assert job.escrow.state is EscrowState.RELEASED, job.escrow.state
    return job


# ------------------------------------------------------------------ the happy path


def test_a_full_refund_reverses_the_capture(engine):
    job = settled_job(engine)
    engine.refund_job(job)
    assert job.escrow.state is EscrowState.REFUNDED
    assert job.status is JobStatus.REFUNDED
    assert job.escrow.refunded_amount == Decimal("120.00")
    assert job.escrow.refund_id
    assert any(e.kind == "escrow_refunded" for e in job.ledger)


def test_a_partial_refund_leaves_the_job_released(engine):
    job = settled_job(engine)
    engine.refund_job(job, amount=Decimal("50.00"))
    # Not fully refunded, so the escrow is still a released capture with a partial refund.
    assert job.escrow.state is EscrowState.RELEASED
    assert job.escrow.refunded_amount == Decimal("50.00")
    entry = [e for e in job.ledger if e.kind == "escrow_refunded"][-1]
    assert entry.detail["partial"] is True


def test_two_partial_refunds_sum_to_a_full_refund(engine):
    job = settled_job(engine)
    engine.refund_job(job, amount=Decimal("50.00"))
    engine.refund_job(job, amount=Decimal("70.00"))
    assert job.escrow.refunded_amount == Decimal("120.00")
    assert job.escrow.state is EscrowState.REFUNDED


# ------------------------------------------------------------------ the cap


def test_a_refund_beyond_the_capture_is_refused_before_the_provider(engine):
    job = settled_job(engine)
    with pytest.raises(ValueError, match="exceeds"):
        engine.refund_job(job, amount=Decimal("200.00"))
    assert engine.escrow.refund_calls == [], "no provider call may be made for an over-large refund"


def test_the_sum_of_refunds_cannot_exceed_the_capture(engine):
    job = settled_job(engine)
    engine.refund_job(job, amount=Decimal("100.00"))
    with pytest.raises(ValueError, match="exceeds"):
        engine.refund_job(job, amount=Decimal("50.00"))
    assert job.escrow.refunded_amount == Decimal("100.00")


def test_a_refund_of_exactly_the_remaining_amount_is_allowed(engine):
    job = settled_job(engine)
    engine.refund_job(job, amount=Decimal("100.00"))
    engine.refund_job(job, amount=Decimal("20.00"))
    assert job.escrow.state is EscrowState.REFUNDED


def test_a_refund_of_zero_is_refused(engine):
    job = settled_job(engine)
    with pytest.raises(ValueError):
        engine.refund_job(job, amount=Decimal("0.00"))


# ------------------------------------------------------------------ idempotency


def test_a_repeated_full_refund_does_not_move_money_twice(engine):
    job = settled_job(engine)
    engine.refund_job(job)
    calls = len(engine.escrow.refund_calls)
    engine.refund_job(job)  # a retry after success
    assert len(engine.escrow.refund_calls) == calls, "a settled refund must be a no-op"
    assert job.escrow.refunded_amount == Decimal("120.00")


def test_each_distinct_refund_uses_a_new_idempotency_key(engine):
    job = settled_job(engine)
    engine.refund_job(job, amount=Decimal("40.00"))
    engine.refund_job(job, amount=Decimal("40.00"))
    keys = engine.escrow.refund_calls
    assert len(keys) == 2
    assert keys[0] != keys[1], "two distinct refunds must not share a provider key"


# ------------------------------------------------------------------ state gating


def test_a_held_authorization_cannot_be_refunded(engine):
    job = engine.create_job(
        title="Copy", brief="Write copy.", criteria=["A headline"], budget_max=Decimal("300")
    )
    job.seller_id = "seller_nova"
    job.agreed_price = Decimal("120.00")
    engine.settlement.hold(job)
    assert job.escrow.state is EscrowState.HELD
    with pytest.raises(ValueError, match="Cannot refund"):
        engine.refund_job(job)


def test_a_job_with_no_capture_cannot_be_refunded(engine):
    job = engine.create_job(
        title="Copy", brief="Write copy.", criteria=["A headline"], budget_max=Decimal("300")
    )
    job.escrow.state = EscrowState.RELEASED  # forced, but no capture id
    with pytest.raises(ValueError, match="No captured payment"):
        engine.refund_job(job)


# ------------------------------------------------------------------ ambiguity


def test_a_provider_failure_is_not_reported_as_a_refund(engine):
    job = settled_job(engine)
    engine.escrow.raise_on_refund = PayPalError("nope", code="REFUND_FAILED")
    with pytest.raises(PayPalError):
        engine.refund_job(job)
    assert job.escrow.state is EscrowState.RELEASED
    assert job.escrow.refunded_amount == Decimal("0.00")
    assert any(e.kind == "refund_failed" for e in job.ledger)


def test_an_ambiguous_refund_leaves_the_job_unresolved(engine):
    job = settled_job(engine)
    engine.escrow.raise_on_refund = AmbiguousOutcome("timeout", code="TIMEOUT")
    with pytest.raises(AmbiguousOutcome):
        engine.refund_job(job)
    assert job.escrow.state is EscrowState.UNRESOLVED
    assert job.escrow.refunded_amount == Decimal("0.00"), "no refund may be recorded on ambiguity"


def test_a_non_completed_refund_is_not_recorded(engine):
    job = settled_job(engine)
    engine.escrow.force_refund_status = "PENDING"
    engine.refund_job(job)
    assert job.escrow.refunded_amount == Decimal("0.00")
    assert job.escrow.state is EscrowState.UNRESOLVED
    assert any(e.kind == "refund_not_completed" for e in job.ledger)


# ------------------------------------------------------------------ ledger


def test_the_refund_ledger_entry_chains(engine):
    job = settled_job(engine)
    engine.refund_job(job, amount=Decimal("30.00"))
    assert job.verify_chain() is True


def test_a_refund_never_records_the_provider_credentials(engine):
    job = settled_job(engine)
    engine.refund_job(job)
    blob = " ".join(e.summary for e in job.ledger)
    assert "secret" not in blob.lower()
