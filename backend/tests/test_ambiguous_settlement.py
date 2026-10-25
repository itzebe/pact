"""Ambiguous settlement outcomes and the reconciliation that resolves them.

An ambiguous outcome is the one case where PACT cannot tell, from its own records,
whether money moved: PayPal processed the call and the response was lost. The
required behaviour is explicit — the job must enter an unresolved state, must not
be retried blindly, and must be resolvable only by asking PayPal what it did.

These tests exercise that sequence end to end against a real provider double whose
state machine is the same one the live client talks to.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pact.core.engine import PactEngine
from pact.core.llm import MockLLM
from pact.core.models import EscrowState, JobStatus
from pact.core.store import JobStore, ReputationLedger
from pact.paypal import AmbiguousOutcome, MockPayPalEscrowClient, PayPalError
from pact.paypal.escrow import Capture

CRITERIA = ["The deliverable states the headline result"]


class ScriptedEscrow(MockPayPalEscrowClient):
    """A double whose capture can be made to fail after being applied remotely.

    ``applied_then_ambiguous`` is the failure this whole module exists for: PayPal
    applied the capture, then the response was lost. Modelling it honestly means
    the provider state must actually change while the caller sees a timeout.
    """

    def __init__(self) -> None:
        super().__init__()
        self.raise_on: dict[str, Exception] = {}
        self.calls: list[str] = []
        self.apply_then_ambiguous = False

    def capture_authorization(self, authorization_id, **kwargs):
        self.calls.append("capture_authorization")
        if self.apply_then_ambiguous:
            # The remote operation really happens, then the response is lost.
            capture = super().capture_authorization(authorization_id, **kwargs)
            raise AmbiguousOutcome(
                f"capture {capture.id} applied but the response was lost", code="TIMEOUT"
            )
        if "capture_authorization" in self.raise_on:
            raise self.raise_on["capture_authorization"]
        return super().capture_authorization(authorization_id, **kwargs)

    def get_authorization(self, authorization_id: str):
        self.calls.append("get_authorization")
        if "get_authorization" in self.raise_on:
            raise self.raise_on["get_authorization"]
        return super().get_authorization(authorization_id)

    def get_order(self, order_id: str):
        self.calls.append("get_order")
        if "get_order" in self.raise_on:
            raise self.raise_on["get_order"]
        return super().get_order(order_id)


def _engine(tmp_path, escrow):
    return PactEngine(
        llm=MockLLM(),
        escrow=escrow,
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
    )


def _held_job(engine: PactEngine):
    """A job in HELD with a live authorization, ready to settle."""
    job = engine.create_job(title="Ambiguity", brief="b", criteria=CRITERIA,
                            budget_max=Decimal("200"))
    job.seller_id = "seller_nova"
    job.agreed_price = Decimal("120.00")
    engine.settlement.hold(job)
    assert job.escrow.state is EscrowState.HELD
    engine.store.save(job)
    return job


# ------------------------------------------------- C. ambiguous capture outcome


def test_ambiguous_capture_is_unresolved_and_keeps_the_inflight_marker(tmp_path):
    """Process dies after the pending marker is committed (failure sequence 1-4).

    The marker must survive, because it is the only local evidence that a capture
    may be in flight. Discarding it would leave a later attempt believing the
    authorization is untouched and free to capture.
    """
    escrow = ScriptedEscrow()
    escrow.apply_then_ambiguous = True
    engine = _engine(tmp_path, escrow)
    job = _held_job(engine)

    with pytest.raises(AmbiguousOutcome):
        engine.settlement.release(job)

    stored = engine.store.get(job.id)
    assert stored.escrow.state is EscrowState.UNRESOLVED
    assert stored.status is JobStatus.UNRESOLVED
    assert stored.escrow.capture_id == "pending", (
        "the in-flight marker must be retained; clearing it would hide that a "
        "capture may already have been applied"
    )
    assert any(e.kind == "settlement_unresolved" for e in stored.ledger)
    # A second attempt must not reach the provider.
    calls_before = escrow.calls.count("capture_authorization")
    with pytest.raises(ValueError):
        engine.settlement.release(stored)
    assert escrow.calls.count("capture_authorization") == calls_before, (
        "an unresolved job must not be captured again before reconciliation"
    )


def test_reconciliation_finds_a_capture_that_actually_landed(tmp_path):
    """Capture succeeded, the response was lost, and the retry must not double-charge.

    The retry reconciles instead: PayPal is asked what it did, and the answer is
    that the authorization is CAPTURED. The capture id is recovered from the order
    rather than invented.
    """
    escrow = ScriptedEscrow()
    escrow.apply_then_ambiguous = True
    engine = _engine(tmp_path, escrow)
    job = _held_job(engine)

    with pytest.raises(AmbiguousOutcome):
        engine.settlement.release(job)

    stored = engine.store.get(job.id)
    # Stop forcing the ambiguity; the provider is now queried, not called.
    escrow.apply_then_ambiguous = False
    reconciled = engine.reconcile_job(stored)

    assert reconciled.escrow.state is EscrowState.RELEASED
    assert reconciled.status is JobStatus.SETTLED
    assert reconciled.escrow.capture_id.startswith("MOCK-CAPTURE-"), (
        "the real capture id must be recovered from PayPal, not invented"
    )
    assert reconciled.escrow.capture_status == "COMPLETED"
    assert any(e.kind == "settlement_reconciled" for e in reconciled.ledger)
    # Reconciliation must be a read: it never initiates a capture.
    assert escrow.calls.count("capture_authorization") == 1

    # The failure recorded while the outcome was unknown must be reversed, or
    # reputation would contradict a ledger that says the money moved.
    record = engine.reputation.get("seller_nova")
    assert record["failed"] == 0, "the unknown outcome was not the seller's fault"
    assert record["settled"] == 1, "the confirmed settlement must be credited"
    assert record["volume"] == 120.0


def test_reconciliation_clears_the_claim_when_no_capture_landed(tmp_path):
    """If PayPal never applied the capture, the job is retryable again."""
    escrow = ScriptedEscrow()
    escrow.raise_on["capture_authorization"] = AmbiguousOutcome("timed out", code="TIMEOUT")
    engine = _engine(tmp_path, escrow)
    job = _held_job(engine)

    with pytest.raises(AmbiguousOutcome):
        engine.settlement.release(job)
    stored = engine.store.get(job.id)
    assert stored.escrow.state is EscrowState.UNRESOLVED

    reconciled = engine.reconcile_job(stored)
    assert reconciled.escrow.state is EscrowState.HELD, "the hold is intact"
    assert reconciled.escrow.capture_id == "", (
        "the in-flight marker must be withdrawn once PayPal confirms nothing landed"
    )
    # And the job can now settle normally.
    escrow.raise_on.clear()
    settled = engine.settlement.release(reconciled)
    assert settled.status is JobStatus.SETTLED


def test_reconciliation_stays_unresolved_when_paypal_is_unreachable(tmp_path):
    """An unreachable provider is not evidence that no money moved."""
    escrow = ScriptedEscrow()
    escrow.apply_then_ambiguous = True
    engine = _engine(tmp_path, escrow)
    job = _held_job(engine)

    with pytest.raises(AmbiguousOutcome):
        engine.settlement.release(job)
    stored = engine.store.get(job.id)

    escrow.raise_on["get_authorization"] = PayPalError("provider down", code="TRANSPORT_ERROR")
    reconciled = engine.reconcile_job(stored)

    assert reconciled.escrow.state is EscrowState.UNRESOLVED, (
        "failing to reach PayPal must not be read as 'no capture happened'"
    )
    assert any(e.kind == "reconciliation_unavailable" for e in reconciled.ledger)
    # Still blocked from capturing.
    with pytest.raises(ValueError):
        engine.settlement.release(reconciled)


def test_reconciliation_is_refused_for_a_job_that_is_not_unresolved(tmp_path):
    escrow = ScriptedEscrow()
    engine = _engine(tmp_path, escrow)
    job = _held_job(engine)
    with pytest.raises(ValueError):
        engine.settlement.reconcile(job)


def test_an_ambiguous_authorize_is_reconciled_from_the_order(tmp_path):
    """The hold itself can be ambiguous; the order is then the only handle."""
    escrow = ScriptedEscrow()
    engine = _engine(tmp_path, escrow)
    job = engine.create_job(title="Auth ambiguity", brief="b", criteria=CRITERIA,
                            budget_max=Decimal("200"))
    job.seller_id = "seller_nova"
    job.agreed_price = Decimal("120.00")

    # Create the order, then lose the authorize response after it was applied.
    engine.settlement.hold(job)  # mock auto-approves and authorizes in one step
    assert job.escrow.authorization_id

    # Now simulate the ambiguous-authorize state directly: order known, no auth id.
    job.escrow.authorization_id = ""
    job.escrow.authorization_status = ""
    engine.settlement.mark_unresolved(job, "authorize timed out")

    reconciled = engine.reconcile_job(job)
    assert reconciled.escrow.state is EscrowState.HELD, (
        "the order holds an authorization, so the funds are held"
    )
    assert reconciled.escrow.authorization_id.startswith("MOCK-AUTH-")
    assert reconciled.status is JobStatus.ESCROWED


# ------------------------------------------------ D. definite capture failures


def test_a_definite_failure_is_retryable_and_not_wedged(tmp_path):
    """A definite rejection must not leave a pending claim behind."""
    escrow = ScriptedEscrow()
    escrow.raise_on["capture_authorization"] = PayPalError(
        "authorization expired", code="AUTHORIZATION_EXPIRED"
    )
    engine = _engine(tmp_path, escrow)
    job = _held_job(engine)

    with pytest.raises(PayPalError):
        engine.settlement.release(job)

    stored = engine.store.get(job.id)
    assert stored.escrow.capture_id == "", "no claim may survive a definite failure"
    assert stored.escrow.state is EscrowState.HELD, "the hold is untouched and retryable"
    assert stored.status is not JobStatus.UNRESOLVED, (
        "a definite failure is not an ambiguous outcome"
    )
    assert any(e.kind == "capture_failed" for e in stored.ledger)


def test_an_unresolved_job_is_blocked_from_every_settlement_path(tmp_path):
    """Nothing may capture or void a job whose outcome is unknown.

    The guard must hold at the engine entry points too, not only inside
    ``release`` — otherwise a route could still push an unresolved job forward.
    """
    escrow = ScriptedEscrow()
    escrow.raise_on["capture_authorization"] = AmbiguousOutcome("timed out", code="TIMEOUT")
    engine = _engine(tmp_path, escrow)
    job = _held_job(engine)

    with pytest.raises(AmbiguousOutcome):
        engine.settlement.release(job)
    unresolved = engine.store.get(job.id)
    assert unresolved.escrow.state is EscrowState.UNRESOLVED

    # settle_existing refuses a job that is not HELD.
    assert engine.settle_existing(unresolved) is unresolved
    assert unresolved.escrow.state is EscrowState.UNRESOLVED
    # refund refuses too.
    with pytest.raises(ValueError):
        engine.settlement.refund(unresolved)
    # and release still refuses, so no second capture can be attempted.
    with pytest.raises(ValueError):
        engine.settlement.release(unresolved)


def test_a_pending_capture_status_is_not_settlement(tmp_path):
    """A capture PayPal reports as PENDING is not a completed payment."""
    escrow = ScriptedEscrow()
    engine = _engine(tmp_path, escrow)
    job = _held_job(engine)

    original = escrow.capture_authorization

    def capture_pending(authorization_id, **kwargs):
        capture = original(authorization_id, **kwargs)
        escrow._captures[capture.id]["status"] = "PENDING"
        return Capture(id=capture.id, status="PENDING", amount=capture.amount,
                       currency=capture.currency, raw=capture.raw)

    escrow.capture_authorization = capture_pending
    settled = engine.settlement.release(job)

    assert settled.status is JobStatus.UNRESOLVED
    assert settled.escrow.state is EscrowState.UNRESOLVED
    assert engine.reputation.get("seller_nova")["settled"] == 0


def test_marking_unresolved_twice_is_idempotent(tmp_path):
    """The engine and the settlement layer both react to an ambiguous error.

    ``release`` records the unresolved outcome and re-raises; the engine's own
    handler then records it again. Without an idempotent guard that appends a
    duplicate ledger entry and counts the seller's failure twice — so this must go
    through ``engine.run``, which is the path that performs both calls.
    """
    escrow = ScriptedEscrow()
    escrow.raise_on["capture_authorization"] = AmbiguousOutcome("timed out", code="TIMEOUT")
    engine = _engine(tmp_path, escrow)

    job = engine.run(
        engine.create_job(title="Copy", brief="Write the copy.", criteria=CRITERIA,
                          budget_max=Decimal("200")),
        seller_ids=["seller_nova"],
    )

    assert job.status is JobStatus.UNRESOLVED
    assert [e.kind for e in job.ledger].count("settlement_unresolved") == 1, (
        "an ambiguous outcome must be recorded exactly once, not once per layer"
    )
    assert engine.reputation.get("seller_nova")["failed"] == 1, (
        "the seller's failure must be counted once, not once per layer"
    )
