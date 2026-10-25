"""Read-only PayPal order inspection: the route that diagnoses a stuck approval.

PACT creates orders with no explicit ``payee``, so PayPal derives the merchant from
the credentials. A buyer signed in as a *different* Sandbox account then sees the
"seller account" warning and, after switching accounts, a generic render failure.
Diagnosing that from the approval link alone is guesswork, so this route asks
PayPal for the order's own record.

Two properties are pinned here: the route must not republish identities (merchant
and payer addresses are redacted), and it must not invent a diagnosis when the
provider is unreachable — a failed lookup is reported as a failure.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    from pact.api import routes
    from pact.core.engine import PactEngine
    from pact.core.llm import MockLLM
    from pact.core.store import JobStore, ReputationLedger
    from pact.main import app
    from pact.paypal import MockPayPalEscrowClient

    engine = PactEngine(
        llm=MockLLM(),
        escrow=MockPayPalEscrowClient(),
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
    )
    monkeypatch.setattr(routes, "engine", engine)
    return TestClient(app)


class _RecordingEscrow:
    """A real-client stand-in that returns a PayPal-shaped order payload."""

    environment = "sandbox"

    def __init__(self, order: dict | None = None, error: Exception | None = None) -> None:
        self.order = order
        self.error = error
        self.calls: list[str] = []

    @property
    def configured(self) -> bool:
        return True

    def get_order(self, order_id: str) -> dict:
        self.calls.append(order_id)
        if self.error is not None:
            raise self.error
        return self.order or {}


def _order() -> dict:
    return {
        "id": "5K809610RF599244X",
        "intent": "AUTHORIZE",
        "status": "CREATED",
        "purchase_units": [
            {
                "reference_id": "job_c41357feb7cf",
                "amount": {"currency_code": "USD", "value": "300.00"},
                "payee": {"email_address": "merchant-sandbox@business.example.com",
                          "merchant_id": "V2MGYQSE3EZCL"},
            }
        ],
        "links": [
            {"rel": "self", "href": "https://api-m.sandbox.paypal.com/v2/checkout/orders/5K809610RF599244X"},
            {"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=5K809610RF599244X"},
        ],
    }


def _held_job(engine, order_id: str = "5K809610RF599244X"):
    """A job sitting in awaiting_approval with a recorded order id."""
    from decimal import Decimal

    from pact.core.models import EscrowState, JobStatus

    job = engine.create_job(title="Approval probe", brief="b", criteria=["c"], budget_max=Decimal("300"))
    job.escrow.order_id = order_id
    job.escrow.approval_url = f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"
    job.escrow.state = EscrowState.AWAITING_APPROVAL
    job.status = JobStatus.AWAITING_APPROVAL
    engine.store.save(job)
    return job


def test_the_route_reports_the_providers_own_order_record(client, monkeypatch):
    from pact.api import routes

    escrow = _RecordingEscrow(_order())
    monkeypatch.setattr(routes.engine, "escrow", escrow)
    job = _held_job(routes.engine)

    body = client.get(f"/api/jobs/{job.id}/paypal-order").json()
    assert escrow.calls == ["5K809610RF599244X"], "the provider must actually be queried"
    assert body["intent"] == "AUTHORIZE"
    assert body["status"] == "CREATED"
    assert body["environment"] == "sandbox"
    unit = body["purchase_units"][0]
    assert unit["amount"] == {"currency_code": "USD", "value": "300.00"}
    assert body["approval_url"] == "https://www.sandbox.paypal.com/checkoutnow?token=5K809610RF599244X"
    assert body["approval_url_matches_stored"] is True


def test_the_merchant_identity_is_redacted(client, monkeypatch):
    from pact.api import routes

    monkeypatch.setattr(routes.engine, "escrow", _RecordingEscrow(_order()))
    job = _held_job(routes.engine)

    raw = client.get(f"/api/jobs/{job.id}/paypal-order").text
    assert "merchant-sandbox@business.example.com" not in raw, "merchant mailbox must not be republished"
    assert "m***@business.example.com" in raw, "the domain should survive so sandbox/live is distinguishable"
    # A non-email identifier is not an address and is safe to keep.
    assert "V2MGYQSE3EZCL" in raw


def test_a_mismatched_stored_url_is_reported_not_hidden(client, monkeypatch):
    """If the stored link differs from the provider's, that must be visible."""
    from pact.api import routes

    order = _order()
    order["links"] = [{"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=DIFFERENT"}]
    monkeypatch.setattr(routes.engine, "escrow", _RecordingEscrow(order))
    job = _held_job(routes.engine)

    body = client.get(f"/api/jobs/{job.id}/paypal-order").json()
    assert body["approval_url_matches_stored"] is False


def test_a_provider_error_is_reported_as_a_failure_not_a_guess(client, monkeypatch):
    """Negative control: an unreachable provider must surface, never be smoothed over."""
    from pact.api import routes
    from pact.paypal import PayPalError

    monkeypatch.setattr(
        routes.engine, "escrow",
        _RecordingEscrow(error=PayPalError("PayPal error INVALID_RESOURCE_ID", status=404, code="INVALID_RESOURCE_ID")),
    )
    job = _held_job(routes.engine)

    response = client.get(f"/api/jobs/{job.id}/paypal-order")
    assert response.status_code == 502
    assert response.json()["detail"]["provider_error"] == "INVALID_RESOURCE_ID"


def test_the_route_refuses_when_the_deployment_is_simulated(client):
    """No PayPal credentials means no provider record to read, so say so."""
    from pact.api import routes

    job = _held_job(routes.engine)
    response = client.get(f"/api/jobs/{job.id}/paypal-order")
    assert response.status_code == 409
    assert "simulated" in response.json()["detail"]


def test_the_route_refuses_a_job_with_no_order(client):
    from decimal import Decimal

    from pact.api import routes

    job = routes.engine.create_job(title="No order", brief="b", criteria=["c"], budget_max=Decimal("100"))
    response = client.get(f"/api/jobs/{job.id}/paypal-order")
    assert response.status_code == 409
    assert "no PayPal order" in response.json()["detail"]
