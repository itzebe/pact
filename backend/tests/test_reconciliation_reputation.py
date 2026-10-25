"""Reconciliation must not punish a seller for an outcome that was never its fault.

``mark_unresolved`` charges the seller a failure while an outcome is unknown,
because that is the honest reading at the time. When reconciliation later
establishes that nothing was captured — the hold is intact, or the authorize
simply landed — that failure has to be withdrawn, or a transient provider timeout
would permanently depress a blameless seller's score and distort who wins the
next job.

The same path has to stay reachable after a crash. The in-flight capture claim is
persisted *before* the provider call so two instances cannot both capture; if the
process dies in between, the job is left ``HELD`` with a claim whose outcome
nobody recorded. It must be blocked from settling, but it must still be
reconcilable — otherwise the job is wedged forever and the hold can never be
released or returned.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pact.core.engine import PactEngine
from pact.core.llm import MockLLM
from pact.core.models import EscrowState, JobStatus
from pact.core.store import JobStore, ReputationLedger
from pact.paypal import AmbiguousOutcome, MockPayPalEscrowClient, PayPalError
from pact.paypal.escrow import Authorization

CRITERIA = ["The deliverable states the headline result"]


class ScriptedEscrow(MockPayPalEscrowClient):
    """A provider double whose calls can fail ambiguously, before or after applying.

    The distinction matters: a capture that was *applied* and then lost its
    response must be found by reconciliation, while one that timed out *before*
    applying must leave the hold intact. Conflating them would make the
    reconciliation tests pass for the wrong reason.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.capture_raises_before_apply = False
        self.capture_applies_then_raises = False
        self.authorize_applies_then_raises = False
        self.hide_authorization_from_order = False

    def authorize_order(self, order_id: str, **kwargs) -> Authorization:
        self.calls.append("authorize_order")
        authorization = super().authorize_order(order_id, **kwargs)
        if self.hide_authorization_from_order:
            # The authorize never landed, so the order exposes nothing.
            self._orders[order_id]["purchase_units"][0].pop("payments", None)
        if self.authorize_applies_then_raises:
            raise AmbiguousOutcome("authorize applied but the response was lost", code="TIMEOUT")
        return authorization

    def capture_authorization(self, authorization_id: str, **kwargs):
        self.calls.append("capture_authorization")
        if self.capture_raises_before_apply:
            raise AmbiguousOutcome("capture timed out before it was applied", code="TIMEOUT")
        capture = super().capture_authorization(authorization_id, **kwargs)
        if self.capture_applies_then_raises:
            raise AmbiguousOutcome(f"capture {capture.id} applied, response lost", code="TIMEOUT")
        return capture


def _engine(tmp_path, escrow):
    return PactEngine(
        llm=MockLLM(),
        escrow=escrow,
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
    )


def _held_job(engine: PactEngine) -> object:
    job = engine.create_job(title="Reconcile", brief="b", criteria=CRITERIA,
                            budget_max=Decimal("200"))
    job.seller_id = "seller_nova"
    job.agreed_price = Decimal("120.00")
    engine.settlement.hold(job)
    assert job.escrow.state is EscrowState.HELD
    engine.store.save(job)
    return job


# ------------------------------------------- a transient timeout is not a fault


def test_reconciling_an_intact_hold_reverses_the_provisional_failure(tmp_path):
    """An ambiguous capture that never landed must not leave the seller penalised.

    The seller delivered, the provider timed out, and PayPal confirms the hold is
    still open. Charging that to the seller would make a blameless agent lose
    score — and, because reputation feeds seller selection, would route future work
    away from it for no reason.
    """
    escrow = ScriptedEscrow()
    escrow.capture_raises_before_apply = True
    engine = _engine(tmp_path, escrow)
    job = _held_job(engine)

    with pytest.raises(AmbiguousOutcome):
        engine.settlement.release(job)
    assert engine.reputation.get("seller_nova")["failed"] == 1

    # The provider is now queried rather than called: no capture landed.
    escrow.capture_raises_before_apply = False
    reconciled = engine.reconcile_job(engine.store.get(job.id))

    assert reconciled.escrow.state is EscrowState.HELD, "the hold is intact and retryable"
    record = engine.reputation.get("seller_nova")
    assert record["failed"] == 0, "a transient timeout was not the seller's fault"
    assert any(e.kind == "reputation_corrected" for e in reconciled.ledger)

    # And the job settles normally afterwards, ending on an unblemished record.
    settled = engine.settlement.release(reconciled)
    assert settled.status is JobStatus.SETTLED
    final = engine.reputation.get("seller_nova")
    assert final["failed"] == 0 and final["settled"] == 1 and final["volume"] == 120.0


def test_reconciling_a_lost_authorize_that_landed_reverses_the_failure(tmp_path):
    """An ambiguous authorize that actually applied is a hold, not a failure."""
    escrow = ScriptedEscrow()
    engine = _engine(tmp_path, escrow)
    job = engine.create_job(title="Auth", brief="b", criteria=CRITERIA,
                            budget_max=Decimal("200"))
    job.seller_id = "seller_nova"
    job.agreed_price = Decimal("120.00")
    engine.settlement.hold(job)

    # Model the lost response: the order holds the authorization, but the job
    # never learned its id, and the outcome was recorded as unknown.
    job.escrow.authorization_id = ""
    job.escrow.authorization_status = ""
    engine.settlement.mark_unresolved(job, "authorize timed out")
    assert engine.reputation.get("seller_nova")["failed"] == 1

    reconciled = engine.reconcile_job(job)
    assert reconciled.escrow.state is EscrowState.HELD
    assert engine.reputation.get("seller_nova")["failed"] == 0, (
        "the authorize landed, so the seller was never at fault"
    )


def test_an_authorize_that_genuinely_never_landed_still_counts_as_a_failure(tmp_path):
    """The reversal must be earned, not unconditional.

    When the order holds no authorization, the hold truly never landed and the
    job fails. The failure must stay on the seller's record.
    """
    escrow = ScriptedEscrow()
    escrow.authorize_applies_then_raises = True
    escrow.hide_authorization_from_order = True
    engine = _engine(tmp_path, escrow)
    job = engine.create_job(title="Auth", brief="b", criteria=CRITERIA,
                            budget_max=Decimal("200"))
    job.seller_id = "seller_nova"
    job.agreed_price = Decimal("120.00")
    with pytest.raises(AmbiguousOutcome):
        engine.settlement.hold(job)
    assert engine.reputation.get("seller_nova")["failed"] == 1

    reconciled = engine.reconcile_job(job)
    assert reconciled.status is JobStatus.FAILED
    assert engine.reputation.get("seller_nova")["failed"] == 1, (
        "no hold exists, so this failure is real and must not be reversed"
    )


# --------------------------------- an interrupted in-flight claim stays resolvable


def _interrupted_claim(engine: PactEngine, job):
    """Persist the in-flight claim and reload, as a crash mid-capture would."""
    job.escrow.capture_id = "pending"
    engine.store.save(job)
    return engine.store.get(job.id)


def test_an_interrupted_capture_claim_is_blocked_from_settling(tmp_path):
    """The claim must still stop a blind retry, which is its whole purpose."""
    escrow = ScriptedEscrow()
    engine = _engine(tmp_path, escrow)
    job = _interrupted_claim(engine, _held_job(engine))

    with pytest.raises(ValueError, match="already captured"):
        engine.settlement.release(job)
    # settle_existing must also refuse to reach the provider for a claimed job.
    calls_before = escrow.calls.count("capture_authorization")
    assert engine.settle_existing(job) is job
    assert escrow.calls.count("capture_authorization") == calls_before


def test_an_interrupted_claim_can_be_voided(tmp_path):
    """A void moves no money, so it must not be blocked by a capture claim.

    Otherwise a crashed capture would make the hold impossible to return, leaving
    the buyer's funds reserved with no way to release them.
    """
    engine = _engine(tmp_path, ScriptedEscrow())
    job = _interrupted_claim(engine, _held_job(engine))

    voided = engine.settlement.refund(job, reason="delivery failed")
    assert voided.status is JobStatus.VOIDED
    assert voided.escrow.state is EscrowState.VOIDED
    assert voided.escrow.capture_id == "", "a void must not leave a capture id behind"


def test_an_interrupted_claim_is_reconcilable_when_no_capture_landed(tmp_path):
    """The job must not be wedged: reconciliation clears it and it settles."""
    engine = _engine(tmp_path, ScriptedEscrow())
    job = _interrupted_claim(engine, _held_job(engine))

    reconciled = engine.reconcile_job(job)
    assert reconciled.escrow.state is EscrowState.HELD
    assert reconciled.escrow.capture_id == "", "the withdrawn claim must be cleared"
    assert any(e.kind == "settlement_reconciled" for e in reconciled.ledger)

    settled = engine.settlement.release(reconciled)
    assert settled.status is JobStatus.SETTLED
    assert settled.escrow.capture_status == "COMPLETED"


def test_an_interrupted_claim_is_reconcilable_when_the_capture_did_land(tmp_path):
    """If the lost call had applied, reconciliation must find the real capture."""
    escrow = ScriptedEscrow()
    engine = _engine(tmp_path, escrow)
    job = _interrupted_claim(engine, _held_job(engine))

    # The capture happened remotely before the crash, so the provider knows it.
    real = escrow.capture_authorization(job.escrow.authorization_id)
    reconciled = engine.reconcile_job(job)

    assert reconciled.status is JobStatus.SETTLED
    assert reconciled.escrow.state is EscrowState.RELEASED
    assert reconciled.escrow.capture_id == real.id, (
        "the real capture id must be recovered from the provider, not invented"
    )
    assert reconciled.escrow.capture_status == "COMPLETED"


def test_repeated_reconciliation_does_not_double_reverse_or_duplicate_entries(tmp_path):
    """Processing an unresolved state twice must not double-credit the seller.

    A reconciliation that is safe to call repeatedly is the point; if a second
    call withdrew another failure, a seller could be credited for a failure that
    was never charged.
    """
    escrow = ScriptedEscrow()
    escrow.capture_raises_before_apply = True
    engine = _engine(tmp_path, escrow)
    job = _held_job(engine)

    with pytest.raises(AmbiguousOutcome):
        engine.settlement.release(job)
    escrow.capture_raises_before_apply = False

    first = engine.reconcile_job(engine.store.get(job.id))
    assert engine.reputation.get("seller_nova")["failed"] == 0
    corrections = [e for e in first.ledger if e.kind == "reputation_corrected"]
    assert len(corrections) == 1

    # A second reconciliation of the now-HELD job is refused, and the record is
    # untouched — the failure count cannot go below zero.
    with pytest.raises(ValueError):
        engine.reconcile_job(first)
    assert engine.reputation.get("seller_nova")["failed"] == 0


def test_the_reversal_is_load_bearing(tmp_path):
    """Negative control: the phantom failure returns if the reversal is disabled.

    Without this, the fix could be a no-op and the tests above would still pass
    because they only assert the corrected end state.
    """
    escrow = ScriptedEscrow()
    escrow.capture_raises_before_apply = True
    engine = _engine(tmp_path, escrow)
    job = _held_job(engine)

    with pytest.raises(AmbiguousOutcome):
        engine.settlement.release(job)
    escrow.capture_raises_before_apply = False

    # Break exactly the correction under test.
    engine.reputation.record_reconciliation_reversed = lambda agent_id: engine.reputation.get(agent_id)

    engine.reconcile_job(engine.store.get(job.id))
    assert engine.reputation.get("seller_nova")["failed"] == 1, (
        "with the reversal disabled the seller stays penalised for a timeout that "
        "was never its fault — which is the bug this guards"
    )
