"""Payment-state invariants: the guarantees that protect real money.

Each test here corresponds to a specific way a settlement system can lie about
what happened. They are written against the provider state machine, using status
values from PayPal's published OpenAPI enums, and they assert that Pact refuses
to record a success the provider has not confirmed.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from pact.core.engine import JobAlreadyRunning, PactEngine
from pact.core.llm import MockLLM
from pact.core.models import EscrowState, JobStatus
from pact.core.store import JobStore, ReputationLedger
from pact.paypal import AmbiguousOutcome, MockPayPalEscrowClient, PayPalError
from pact.paypal.escrow import Authorization, Capture, approval_url


class ScriptedEscrow(MockPayPalEscrowClient):
    """A mock whose individual operations can be made to misbehave on demand.

    Subclassing the real double keeps the state machine intact, so a scripted
    failure is layered on top of correct behaviour rather than replacing it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.force_authorization_status: str | None = None
        self.force_capture_status: str | None = None
        self.raise_on: dict[str, Exception] = {}
        self.calls: list[str] = []

    def _maybe_raise(self, name: str) -> None:
        self.calls.append(name)
        if name in self.raise_on:
            raise self.raise_on[name]

    def authorize_order(self, order_id: str, *, idempotency_key=None):
        self._maybe_raise("authorize_order")
        authorization = super().authorize_order(order_id, idempotency_key=idempotency_key)
        if self.force_authorization_status:
            self._authorizations[authorization.id]["status"] = self.force_authorization_status
            authorization.status = self.force_authorization_status
        return authorization

    def capture_authorization(self, authorization_id: str, **kwargs):
        self._maybe_raise("capture_authorization")
        capture = super().capture_authorization(authorization_id, **kwargs)
        if self.force_capture_status:
            self._captures[capture.id]["status"] = self.force_capture_status
            capture.status = self.force_capture_status
        return capture

    def void_authorization(self, authorization_id: str, *, idempotency_key=None):
        self._maybe_raise("void_authorization")
        return super().void_authorization(authorization_id, idempotency_key=idempotency_key)

    def get_authorization(self, authorization_id: str):
        self._maybe_raise("get_authorization")
        return super().get_authorization(authorization_id)


class NeedsApprovalEscrow(ScriptedEscrow):
    """A double that behaves like real PayPal: the order needs buyer approval."""

    auto_approves = False


CRITERIA = ["Hero headline under 12 words", "Three feature blocks", "A clear call to action"]


@pytest.fixture()
def engine(tmp_path) -> PactEngine:
    return PactEngine(
        llm=MockLLM(),
        escrow=ScriptedEscrow(),
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
    )


def make_job(engine: PactEngine, budget: str = "200", title: str = "Copy"):
    return engine.create_job(
        title=title, brief="Write the copy.", criteria=CRITERIA, budget_max=Decimal(budget)
    )


def held_job(engine: PactEngine, escrow: ScriptedEscrow):
    """Run to the point where funds are held, then hand back the job."""
    job = make_job(engine)
    engine.run(job, seller_ids=["seller_nova"])
    assert job.escrow.state is EscrowState.HELD or job.status is JobStatus.SETTLED
    return job


# --------------------------------------------------------------- invariant C
# Failed, malformed, or inconclusive verification must never trigger capture.


def test_a_denied_authorization_is_not_recorded_as_held(tmp_path):
    """A denied authorization is a definite failure, never held funds."""
    escrow = ScriptedEscrow()
    escrow.force_authorization_status = "DENIED"
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = engine.run(make_job(engine), seller_ids=["seller_nova"])

    assert job.escrow.state is not EscrowState.HELD
    assert job.status is JobStatus.FAILED
    assert job.escrow.capture_id == ""
    assert any(e.kind == "escrow_not_held" for e in job.ledger)
    # The seller must not be credited with a settlement that never happened.
    assert engine.reputation.get("seller_nova")["settled"] == 0


def test_an_already_captured_authorization_is_unresolved_not_failed(tmp_path):
    """If money already moved, that is a reconciliation problem, not a failure."""
    escrow = ScriptedEscrow()
    escrow.force_authorization_status = "CAPTURED"
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = engine.run(make_job(engine), seller_ids=["seller_nova"])
    assert job.escrow.state is EscrowState.UNRESOLVED
    assert job.status is JobStatus.UNRESOLVED


def test_a_pending_capture_is_not_recorded_as_settled(tmp_path):
    """Only capture_status COMPLETED means the seller was actually paid."""
    escrow = ScriptedEscrow()
    escrow.force_capture_status = "PENDING"
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = engine.run(make_job(engine), seller_ids=["seller_nova"])

    assert job.escrow.state is EscrowState.UNRESOLVED
    assert job.status is JobStatus.UNRESOLVED
    assert any(e.kind == "capture_not_completed" for e in job.ledger)
    assert engine.reputation.get("seller_nova")["settled"] == 0


def test_a_failed_capture_is_not_recorded_as_settled(tmp_path):
    escrow = ScriptedEscrow()
    escrow.force_capture_status = "FAILED"
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = engine.run(make_job(engine), seller_ids=["seller_nova"])
    assert job.escrow.state is EscrowState.UNRESOLVED
    assert job.status is not JobStatus.SETTLED


def test_verification_failure_never_reaches_capture(engine):
    """The capture call must not be made at all when verification fails."""
    escrow = engine.escrow
    job = engine.run(make_job(engine), seller_ids=["seller_quill"])
    assert job.verification.passed is False
    assert "capture_authorization" not in escrow.calls, "a failed delivery must never be captured"
    assert job.escrow.capture_id == ""


# --------------------------------------------------------------- invariant D
# A task must not be captured more than once.


def test_a_second_capture_attempt_is_refused(tmp_path):
    escrow = ScriptedEscrow()
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = make_job(engine)
    engine.run(job, seller_ids=["seller_nova"])
    assert job.escrow.state is EscrowState.RELEASED

    with pytest.raises(ValueError, match="Cannot release escrow in state"):
        engine.settlement.release(job)


def test_settle_existing_is_a_no_op_once_settled(tmp_path):
    escrow = ScriptedEscrow()
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = make_job(engine)
    engine.run(job, seller_ids=["seller_nova"])
    captures_before = escrow.calls.count("capture_authorization")

    engine.settle_existing(job)
    assert escrow.calls.count("capture_authorization") == captures_before


def test_concurrent_capture_attempts_capture_only_once(tmp_path):
    """Racing settlement calls must reach the provider exactly once.

    Two threads both pass the "is it held?" check under a naive implementation and
    both call capture. The lock plus the pre-claim means only one wins.
    """
    escrow = ScriptedEscrow()
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = make_job(engine)
    job.seller_id = "seller_nova"
    job.agreed_price = Decimal("120.00")
    engine.settlement.hold(job)
    assert job.escrow.state is EscrowState.HELD

    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def attempt() -> None:
        try:
            barrier.wait(timeout=5)
            engine.settlement.release(job)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert escrow.calls.count("capture_authorization") == 1, "capture must reach the provider once"
    assert len(errors) == 3, "every losing attempt must be refused, not silently accepted"
    assert job.escrow.state is EscrowState.RELEASED
    assert job.status is JobStatus.SETTLED


# --------------------------------------------------------------- invariant F
# A timeout must not be reported as a definite failure.


def test_an_ambiguous_authorization_timeout_is_unresolved_not_failed(tmp_path):
    escrow = ScriptedEscrow()
    escrow.raise_on["authorize_order"] = AmbiguousOutcome("timed out", code="TIMEOUT")
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = engine.run(make_job(engine), seller_ids=["seller_nova"])

    assert job.status is JobStatus.UNRESOLVED, "an unknown outcome is not a failure"
    assert job.escrow.state is EscrowState.UNRESOLVED
    assert any(e.kind == "settlement_unresolved" for e in job.ledger)
    # It must not be retried automatically.
    assert escrow.calls.count("authorize_order") == 1


def test_an_ambiguous_capture_timeout_does_not_report_settlement(tmp_path):
    escrow = ScriptedEscrow()
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = make_job(engine)
    engine.run(job, seller_ids=["seller_nova"])

    # Re-run a release against a freshly held job, this time timing out.
    escrow2 = ScriptedEscrow()
    escrow2.raise_on["capture_authorization"] = AmbiguousOutcome("timed out", code="TIMEOUT")
    engine2 = PactEngine(
        llm=MockLLM(), escrow=escrow2,
        store=JobStore(tmp_path / "j2"), reputation_ledger=ReputationLedger(tmp_path / "r2.json"),
    )
    job2 = engine2.run(make_job(engine2), seller_ids=["seller_nova"])
    assert job2.status is JobStatus.UNRESOLVED
    assert job2.escrow.state is not EscrowState.RELEASED
    assert engine2.reputation.get("seller_nova")["settled"] == 0


def test_a_plain_authorization_error_is_a_failure_not_unresolved(tmp_path):
    """A definite rejection is a failure; only unknown outcomes are unresolved."""
    escrow = ScriptedEscrow()
    escrow.raise_on["authorize_order"] = PayPalError("order not approved", code="ORDER_NOT_APPROVED")
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = engine.run(make_job(engine), seller_ids=["seller_nova"])
    assert job.status is JobStatus.FAILED
    assert job.escrow.state is not EscrowState.HELD


# --------------------------------------------------------------- invariant E
# A void must be confirmed, and must not be reported as a refund.


def test_void_is_confirmed_against_the_provider(tmp_path):
    escrow = ScriptedEscrow()
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = engine.run(make_job(engine), seller_ids=["seller_quill"])
    voided = next(e for e in job.ledger if e.kind == "escrow_voided")
    assert voided.detail["provider_confirmed"] is True
    assert "get_authorization" in escrow.calls


def test_an_unconfirmed_void_is_recorded_as_unconfirmed(tmp_path):
    """If the provider cannot confirm the void, say so rather than assuming."""
    escrow = ScriptedEscrow()
    escrow.raise_on["get_authorization"] = PayPalError("unreachable")
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = engine.run(make_job(engine), seller_ids=["seller_quill"])
    voided = next(e for e in job.ledger if e.kind == "escrow_voided")
    assert voided.detail["provider_confirmed"] is False


def test_a_void_attempt_failure_leaves_the_hold_intact(tmp_path):
    escrow = ScriptedEscrow()
    escrow.raise_on["void_authorization"] = PayPalError("void rejected", code="AUTHORIZATION_ALREADY_CAPTURED")
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = engine.run(make_job(engine), seller_ids=["seller_quill"])
    assert job.escrow.state is not EscrowState.VOIDED
    assert any(e.kind == "void_failed" for e in job.ledger)


# --------------------------------------------------------------- invariant A
# Buyer approval must precede authorization where PayPal requires it.


def test_a_real_style_order_stops_at_awaiting_approval(tmp_path):
    """Without buyer approval, no hold may be claimed and no work may start."""
    escrow = NeedsApprovalEscrow()
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = engine.run(make_job(engine), seller_ids=["seller_nova"])

    assert job.status is JobStatus.AWAITING_APPROVAL
    assert job.escrow.state is EscrowState.AWAITING_APPROVAL
    assert job.escrow.approval_url, "the buyer needs a URL to approve at"
    assert job.escrow.authorization_id == "", "no authorization may exist before approval"
    assert job.delivery is None, "no work may be produced before funds are held"
    assert "authorize_order" not in escrow.calls


def test_approving_completes_the_lifecycle(tmp_path):
    escrow = NeedsApprovalEscrow()
    engine = PactEngine(
        llm=MockLLM(), escrow=escrow,
        store=JobStore(tmp_path / "j"), reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )
    job = engine.run(make_job(engine), seller_ids=["seller_nova"])
    assert job.status is JobStatus.AWAITING_APPROVAL

    job = engine.approve_and_hold(job)
    assert job.escrow.state is EscrowState.RELEASED
    assert job.status is JobStatus.SETTLED
    assert job.delivery is not None


def test_approve_is_refused_when_no_approval_is_pending(engine):
    job = engine.run(make_job(engine), seller_ids=["seller_nova"])
    with pytest.raises(ValueError, match="not awaiting approval"):
        engine.approve_and_hold(job)


def test_approval_url_is_read_from_hateoas_links():
    order = {
        "id": "ORDER-1",
        "links": [
            {"rel": "self", "href": "https://api-m.sandbox.paypal.com/v2/checkout/orders/ORDER-1"},
            {"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-1"},
        ],
    }
    assert approval_url(order) == "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-1"
    assert approval_url({"id": "X", "links": []}) is None


# --------------------------------------------------------------- invariant H
# Concurrent runs must not produce contradictory terminal states.


def test_a_job_that_is_already_running_is_refused(engine):
    job = make_job(engine)
    job.status = JobStatus.VERIFYING
    with pytest.raises(JobAlreadyRunning):
        engine.run(job)


def test_a_settled_job_cannot_be_run_again(engine):
    job = engine.run(make_job(engine), seller_ids=["seller_nova"])
    assert job.status is JobStatus.SETTLED
    with pytest.raises(JobAlreadyRunning):
        engine.run(job)


# --------------------------------------------------------------- invariant K
# Simulated and real operations must stay distinguishable.


def test_simulated_settlement_is_labelled_in_every_ledger_entry(engine):
    job = engine.run(make_job(engine), seller_ids=["seller_nova"])
    for kind in ("escrow_order_created", "escrow_held", "escrow_released"):
        entry = next(e for e in job.ledger if e.kind == kind)
        assert entry.detail["mode"] == "mock"
    assert job.escrow.mode == "mock"


def test_simulated_identifiers_are_never_presented_as_paypal_ids(engine):
    """Every identifier the double mints must be visibly a mock."""
    job = engine.run(make_job(engine), seller_ids=["seller_nova"])
    assert job.escrow.order_id.startswith("MOCK-")
    assert job.escrow.authorization_id.startswith("MOCK-")
    assert job.escrow.capture_id.startswith("MOCK-")


def test_the_real_client_does_not_auto_approve():
    """Only the double may skip buyer approval."""
    from pact.paypal.escrow import PayPalEscrowClient

    assert not getattr(PayPalEscrowClient, "auto_approves", False)
    assert MockPayPalEscrowClient.auto_approves is True
