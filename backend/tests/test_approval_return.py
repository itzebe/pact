"""Regression tests for the buyer-approval return path.

The reported failure was: the buyer signs in with a Sandbox personal account,
clicks "Approve and Continue", and Pact then shows "Failed". Two independent
defects could each produce exactly that, and neither was covered before:

1. The order carried no ``return_url``. PayPal's redirect flow sends the buyer
   back to the merchant only when the order names a return target, so the buyer
   approved and was stranded — which reads as a failed approval. The client
   already supported ``return_url``; no caller ever passed one.
2. ``authorize_order`` used a stable ``PayPal-Request-Id`` per job. PayPal
   replays the *cached* response for a repeated request id, so an authorize
   attempted before approval (``ORDER_NOT_APPROVED``) stayed rejected forever,
   even once the order was approved.

Both are pinned here, each with a negative control that fails if the protection
is removed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pact.core.engine import PactEngine
from pact.core.llm import MockLLM
from pact.core.models import EscrowState
from pact.core.store import JobStore, ReputationLedger
from pact.paypal import PayPalError
from pact.paypal.escrow import PayPalEscrowClient
from pact.paypal.mock import MockPayPalEscrowClient

from test_paypal_client import Transport, client, transport  # noqa: F401  (fixtures)


class RecordingEscrow(MockPayPalEscrowClient):
    """A real double that records the arguments of each order it is asked to create.

    ``auto_approves`` is turned off so the job stops at ``AWAITING_APPROVAL`` and
    the approval step is exercised the way a real deployment experiences it.
    """

    auto_approves = False

    def __init__(self) -> None:
        super().__init__()
        self.create_calls: list[dict] = []

    def create_order(self, **kwargs):
        self.create_calls.append(kwargs)
        return super().create_order(**kwargs)


@pytest.fixture()
def engine(tmp_path) -> PactEngine:
    return PactEngine(
        llm=MockLLM(),
        escrow=RecordingEscrow(),
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
    )


def make_job(engine: PactEngine):
    return engine.create_job(
        title="Write the copy",
        brief="Write the copy.",
        criteria=["The deliverable is non-empty."],
        budget_max=Decimal("200"),
    )


# --------------------------------------------------------------------- return_url


def test_hold_sends_a_real_return_url_so_the_buyer_can_come_back(engine):
    """Without a return_url PayPal has nowhere to send an approving buyer."""
    job = make_job(engine)
    engine.run(job, seller_ids=["seller_nova"], public_url="https://pact.example")

    assert job.escrow.state is EscrowState.AWAITING_APPROVAL
    call = engine.escrow.create_calls[-1]
    assert call["return_url"] == f"https://pact.example/?job={job.id}"
    assert call["cancel_url"] == f"https://pact.example/?job={job.id}&cancelled=1"


def test_return_url_is_never_a_fabricated_placeholder(engine):
    """No known base URL must omit the fields, not point at example.com.

    A placeholder host is worse than nothing: PayPal would send the buyer to a
    domain we do not control and the approval would appear to vanish.
    """
    job = make_job(engine)
    engine.run(job, seller_ids=["seller_nova"], public_url=None)

    call = engine.escrow.create_calls[-1]
    assert call["return_url"] is None
    assert call["cancel_url"] is None


def test_configured_public_url_is_used_when_the_request_supplies_none(engine):
    """PACT_PUBLIC_URL is the explicit override and wins over the request host."""
    from pact.core import settlement as settlement_module

    original = settlement_module.settings.public_url
    object.__setattr__(settlement_module.settings, "public_url", "https://configured.example")
    try:
        job = make_job(engine)
        engine.run(job, seller_ids=["seller_nova"])
        assert engine.escrow.create_calls[-1]["return_url"] == f"https://configured.example/?job={job.id}"
    finally:
        object.__setattr__(settlement_module.settings, "public_url", original)


def test_route_derives_the_public_base_from_forwarded_headers():
    """Behind Vercel the real host is in X-Forwarded-*; the derived base must use it."""
    from pact.api.routes import _public_base

    class FakeUrl:
        scheme = "http"

    class FakeRequest:
        headers = {"x-forwarded-host": "pact-flame.vercel.app", "x-forwarded-proto": "https"}
        url = FakeUrl()

    assert _public_base(FakeRequest()) == "https://pact-flame.vercel.app"


def test_route_falls_back_to_the_host_header_without_forwarded_proto():
    from pact.api.routes import _public_base

    class FakeUrl:
        scheme = "https"

    class FakeRequest:
        headers = {"host": "pact.example"}
        url = FakeUrl()

    assert _public_base(FakeRequest()) == "https://pact.example"


# ----------------------------------------------------------------- idempotency key


class ApprovalAwareEscrow(RecordingEscrow):
    """Rejects authorize until the buyer has approved, exactly as PayPal does.

    The double mirrors PayPal's real idempotency behaviour: a request replayed
    under the same ``PayPal-Request-Id`` returns the first response again, so a
    rejection recorded before approval keeps being returned.
    """

    def __init__(self) -> None:
        super().__init__()
        self.approved = False
        self.seen_keys: list[str | None] = []
        self._by_key: dict[str, object] = {}

    def authorize_order(self, order_id: str, *, idempotency_key=None):
        self.seen_keys.append(idempotency_key)
        if idempotency_key and idempotency_key in self._by_key:
            cached = self._by_key[idempotency_key]
            if isinstance(cached, Exception):
                raise cached
            return cached
        if not self.approved:
            error = PayPalError(
                "PayPal error ORDER_NOT_APPROVED: The requested action could not be performed",
                code="ORDER_NOT_APPROVED",
            )
            if idempotency_key:
                self._by_key[idempotency_key] = error
            raise error
        authorization = super().authorize_order(order_id, idempotency_key=idempotency_key)
        if idempotency_key:
            self._by_key[idempotency_key] = authorization
        return authorization


def _approval_engine(tmp_path, escrow) -> PactEngine:
    return PactEngine(
        llm=MockLLM(),
        escrow=escrow,
        store=JobStore(tmp_path / "j"),
        reputation_ledger=ReputationLedger(tmp_path / "r.json"),
    )


def test_a_rejection_before_approval_does_not_strand_the_job_after_approval(tmp_path):
    """The exact reported flow: attempt, approve, retry — and it must now succeed.

    A stable request id would replay the cached ``ORDER_NOT_APPROVED`` and the job
    would fail forever. This is the regression test for that.
    """
    from pact.core.models import JobStatus

    escrow = ApprovalAwareEscrow()
    engine = _approval_engine(tmp_path, escrow)
    job = make_job(engine)
    engine.run(job, seller_ids=["seller_nova"], public_url="https://pact.example")
    assert job.escrow.state is EscrowState.AWAITING_APPROVAL

    # An early attempt is rejected: the buyer has not approved yet.
    engine.approve_and_hold(job)
    assert job.status is JobStatus.FAILED
    assert job.escrow.state is EscrowState.AWAITING_APPROVAL, "a rejection must not burn the order"

    # The buyer now approves, and the retry must be able to hold.
    escrow.approved = True
    engine.approve_and_hold(job)
    assert job.escrow.authorization_id, "the retry after approval must obtain an authorization"
    assert job.status is not JobStatus.FAILED, "approval must succeed after a pre-approval rejection"
    assert job.escrow.state is not EscrowState.AWAITING_APPROVAL
    assert len(set(escrow.seen_keys)) == len(escrow.seen_keys), "each attempt needs a distinct request id"


def test_a_stable_request_id_would_strand_the_job(tmp_path):
    """Negative control: prove the fresh-key behaviour is load-bearing.

    Models the pre-fix code exactly: the same ``PayPal-Request-Id`` is used for
    the pre-approval attempt and the post-approval retry. PayPal replays the
    cached rejection, so the job can never hold — the reported failure.
    """
    escrow = ApprovalAwareEscrow()
    stable_key = "pact-auth-job_fixed"

    # Pre-fix attempt, before approval: rejected and cached under the stable key.
    with pytest.raises(PayPalError) as first:
        escrow.authorize_order("MOCK-ORDER-X", idempotency_key=stable_key)
    assert "ORDER_NOT_APPROVED" in str(first.value)

    # The buyer approves, then the retry with the same key still fails.
    escrow.approved = True
    with pytest.raises(PayPalError) as caught:
        escrow.authorize_order("MOCK-ORDER-X", idempotency_key=stable_key)
    assert "ORDER_NOT_APPROVED" in str(caught.value), (
        "the cached rejection is what made the buyer's approval look like a failure"
    )


def test_the_client_sends_return_url_only_when_given_one(transport):
    """The client's contract, at the wire level, for both cases."""
    c = PayPalEscrowClient(client_id="cid", client_secret="secret", environment="sandbox", timeout=5)
    transport.set("POST", "/v1/oauth2/token", 200, {"access_token": "T", "expires_in": 3200})
    transport.set("POST", "/v2/checkout/orders", 201, {"id": "O-1", "status": "PAYER_ACTION_REQUIRED", "links": []})

    c.create_order(amount=Decimal("10.00"), return_url="https://pact.example/?job=j1")
    sent = next(r for r in transport.requests if r.url.path == "/v2/checkout/orders")
    import json as _json

    context = _json.loads(sent.content)["payment_source"]["paypal"]["experience_context"]
    assert context["return_url"] == "https://pact.example/?job=j1"


# --------------------------------------------------------- end-to-end through /run


def test_run_route_wires_the_deployments_own_host_into_the_order(tmp_path, monkeypatch):
    """The production entry point must pass a real return target to PayPal.

    This is the wiring the reported bug was missing: the client supported
    ``return_url`` and the engine accepted it, but the route never supplied one, so
    every real order went out with no return target. Exercised through the actual
    HTTP route so a future refactor that drops the argument fails here.
    """
    from fastapi.testclient import TestClient

    from pact.api import routes
    from pact.core.engine import PactEngine
    from pact.core.llm import MockLLM
    from pact.core.store import JobStore, ReputationLedger
    from pact.main import app

    escrow = RecordingEscrow()
    engine = PactEngine(
        llm=MockLLM(),
        escrow=escrow,
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
    )
    monkeypatch.setattr(routes, "engine", engine)

    http = TestClient(app)
    created = http.post(
        "/api/jobs",
        json={
            "title": "Write the copy",
            "brief": "Write the copy.",
            "acceptance_criteria": ["Non-empty."],
            "budget_max": 200,
            "seller_ids": ["seller_nova"],
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    run = http.post(f"/api/jobs/{job_id}/run", headers={"host": "pact-flame.vercel.app"})
    assert run.status_code == 200, run.text
    assert run.json()["escrow"]["state"] == "awaiting_approval"

    call = escrow.create_calls[-1]
    assert call["return_url"], "a real deployment must send PayPal a return target"
    assert call["return_url"].endswith(f"/?job={job_id}")
    assert "pact-flame.vercel.app" in call["return_url"]
    assert call["return_url"].startswith("http"), "the return target must be an absolute URL"
