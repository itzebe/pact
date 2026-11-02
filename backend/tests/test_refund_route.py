"""The refund HTTP route: authorization and conflict handling end to end.

A refund returns money PayPal has already captured, so the route's job is to get
the *authorization* right and to translate engine failures into honest status
codes — never a 200 for a refund that did not happen, never a 500 for a caller
mistake.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from pact.core.models import EscrowState, JobStatus


class RecordingEscrow:
    """A mock capture/refund backend that records the calls it receives."""

    environment = "mock"

    def __init__(self) -> None:
        self.refunds: list[tuple] = []
        self._refunded = Decimal("0.00")
        self.raise_on_refund: Exception | None = None

    @property
    def configured(self) -> bool:
        return False

    def refund_capture(self, capture_id, *, amount=None, currency=None, idempotency_key=None):
        from pact.paypal import PayPalError

        self.refunds.append((capture_id, str(amount), idempotency_key))
        if self.raise_on_refund is not None:
            raise self.raise_on_refund
        requested = Decimal(str(amount))
        if self._refunded + requested > Decimal("120.00"):
            raise PayPalError("too much", code="REFUND_AMOUNT_EXCEEDED")
        self._refunded += requested
        return {"id": "REF-1", "status": "COMPLETED",
                "amount": {"currency_code": currency or "USD", "value": str(amount)}}


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    from pact.api import routes
    from pact.core.engine import PactEngine
    from pact.core.llm import MockLLM
    from pact.core.store import JobStore, ReputationLedger
    from pact.main import app

    engine = PactEngine(
        llm=MockLLM(),
        escrow=RecordingEscrow(),
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
    )
    monkeypatch.setattr(routes, "engine", engine)
    return TestClient(app)


def _released_job(engine, buyer: str = "buyer"):
    job = engine.create_job(
        title="Copy", brief="b", criteria=["c"], budget_max=Decimal("300")
    )
    job.buyer_id = buyer
    job.seller_id = "seller_atlas"
    job.agreed_price = Decimal("120.00")
    job.escrow.amount = Decimal("120.00")
    job.escrow.capture_id = "CAP-1"
    job.escrow.capture_status = "COMPLETED"
    job.escrow.state = EscrowState.RELEASED
    job.status = JobStatus.SETTLED
    engine.store.save(job)
    return job


def _headers(subject: str, role: str) -> dict[str, str]:
    from pact.api.auth import issue_token

    return {"Authorization": f"Bearer {issue_token(subject, role)}"}


def test_the_owning_buyer_can_refund_a_capture(client, monkeypatch):
    from pact.api import routes

    job = _released_job(routes.engine)
    resp = client.post(f"/api/jobs/{job.id}/refund", headers=_headers("buyer", "buyer"), json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["escrow"]["state"] == "refunded"
    assert body["escrow"]["refunded_amount"] == 120.0


def test_a_partial_refund_is_reported_as_released(client, monkeypatch):
    from pact.api import routes

    job = _released_job(routes.engine)
    resp = client.post(
        f"/api/jobs/{job.id}/refund",
        headers=_headers("buyer", "buyer"),
        json={"amount": 40.0, "reason": "partial dispute"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["escrow"]["state"] == "released"
    assert body["escrow"]["refunded_amount"] == 40.0


def test_an_admin_can_refund_any_job(client, monkeypatch):
    from pact.api import routes

    job = _released_job(routes.engine, buyer="someone_else")
    resp = client.post(f"/api/jobs/{job.id}/refund", headers=_headers("admin", "admin"), json={})
    assert resp.status_code == 200, resp.text


def test_another_buyer_cannot_refund_the_job(client, monkeypatch):
    from pact.api import routes

    job = _released_job(routes.engine)
    resp = client.post(
        f"/api/jobs/{job.id}/refund", headers=_headers("buyer_other", "buyer"), json={}
    )
    # Not the owner: the route refuses. It must not leak that the job exists as
    # something refundable, so either 403 or 404 is acceptable, never 200.
    assert resp.status_code in {403, 404}


def test_a_seller_cannot_refund(client, monkeypatch):
    from pact.api import routes

    job = _released_job(routes.engine)
    resp = client.post(
        f"/api/jobs/{job.id}/refund", headers=_headers("seller_atlas", "seller"), json={}
    )
    assert resp.status_code == 403


def test_an_unauthenticated_refund_is_refused(client, monkeypatch):
    from pact.api import routes

    job = _released_job(routes.engine)
    resp = client.post(f"/api/jobs/{job.id}/refund", json={})
    assert resp.status_code == 401


def test_refunding_a_held_job_is_a_conflict(client, monkeypatch):
    from pact.api import routes

    job = _released_job(routes.engine)
    job.escrow.state = EscrowState.HELD
    job.escrow.capture_id = ""
    routes.engine.store.save(job)
    resp = client.post(f"/api/jobs/{job.id}/refund", headers=_headers("buyer", "buyer"), json={})
    assert resp.status_code == 409


def test_an_over_large_refund_is_a_conflict_not_a_500(client, monkeypatch):
    from pact.api import routes

    job = _released_job(routes.engine)
    resp = client.post(
        f"/api/jobs/{job.id}/refund",
        headers=_headers("buyer", "buyer"),
        json={"amount": 999.0},
    )
    assert resp.status_code == 409, resp.text


def test_a_provider_failure_is_a_502(client, monkeypatch):
    from pact.api import routes
    from pact.paypal import PayPalError

    job = _released_job(routes.engine)
    routes.engine.escrow.raise_on_refund = PayPalError("down", code="PROVIDER_ERROR")
    resp = client.post(f"/api/jobs/{job.id}/refund", headers=_headers("buyer", "buyer"), json={})
    assert resp.status_code == 502, resp.text


def test_a_negative_amount_is_rejected_by_validation(client, monkeypatch):
    from pact.api import routes

    job = _released_job(routes.engine)
    resp = client.post(
        f"/api/jobs/{job.id}/refund",
        headers=_headers("buyer", "buyer"),
        json={"amount": -5.0},
    )
    assert resp.status_code == 422, resp.text


def test_the_response_never_carries_a_credential(client, monkeypatch):
    from pact.api import routes

    job = _released_job(routes.engine)
    resp = client.post(f"/api/jobs/{job.id}/refund", headers=_headers("buyer", "buyer"), json={})
    assert "secret" not in resp.text.lower()
