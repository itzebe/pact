"""The PayPal webhook HTTP route, end to end through the app.

The route is unauthenticated by necessity (PayPal cannot send a bearer token), so
its *only* defence is the signature. These tests drive the real FastAPI route with
the escrow client's verification stubbed at the boundary, and prove:

* an unsigned or failed-signature event is refused and changes nothing;
* a verified event is applied exactly once;
* a redelivery is acknowledged without re-applying;
* the route fails closed when verification or the event store is unavailable;
* no credential or event payload leaks into the response.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from pact.core.models import EscrowState, Job, JobStatus
from pact.core.webhook_store import InMemoryWebhookStore


class WebhookEscrow:
    """A minimal escrow client exposing only what the route needs.

    ``verify_result`` controls the signature outcome, so a test can drive both the
    accept and reject paths without a live PayPal call.
    """

    environment = "sandbox"
    can_verify_webhooks = True

    def __init__(self, verify_result: bool = True) -> None:
        self.verify_result = verify_result
        self.verify_calls = 0
        self.last_headers: dict | None = None

    @property
    def configured(self) -> bool:
        return True

    def verify_webhook_signature(self, headers: dict, event: dict) -> bool:
        self.verify_calls += 1
        self.last_headers = headers
        return self.verify_result

    # The route never calls these, but the protocol expects them.
    def create_order(self, **kwargs): raise NotImplementedError
    def authorize_order(self, *a, **k): raise NotImplementedError
    def capture_authorization(self, *a, **k): raise NotImplementedError
    def void_authorization(self, *a, **k): raise NotImplementedError
    def refund_capture(self, *a, **k): raise NotImplementedError
    def get_order(self, *a, **k): raise NotImplementedError
    def get_authorization(self, *a, **k): raise NotImplementedError
    def get_capture(self, *a, **k): raise NotImplementedError


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


class FakeJobStore:
    def __init__(self, job: Job) -> None:
        self.jobs = {job.id: job}
        self.saves = []

    def get(self, job_id):
        return self.jobs.get(job_id)

    def save(self, job):
        self.jobs[job.id] = job
        self.saves.append(job.id)


@pytest.fixture()
def webhook_env(monkeypatch):
    """Wire the app's engine and webhook store to controllable doubles."""
    from pact.api import routes
    from pact.main import app

    job = make_job()
    escrow = WebhookEscrow()
    store = FakeJobStore(job)
    webhook_store = InMemoryWebhookStore()

    monkeypatch.setattr(routes, "engine", _EngineShim(escrow, store))
    monkeypatch.setattr(routes, "webhook_store", webhook_store)
    return TestClient(app), job, escrow, store


class _EngineShim:
    def __init__(self, escrow, store):
        self.escrow = escrow
        self.store = store

    def settlement_guard(self, job):
        # A no-op guard: the route tests exercise the HTTP contract, not concurrency
        # (that is test_webhook_postgres.py and test_settlement_webhook_race.py).
        from contextlib import nullcontext

        return nullcontext()


def capture_payload(job_id: str, event_id: str = "evt-1") -> dict:
    return {
        "id": event_id,
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAP-1",
            "status": "COMPLETED",
            "custom_id": job_id,
            "amount": {"currency_code": "USD", "value": "120.00"},
        },
    }


def test_a_verified_capture_event_settles_the_job(webhook_env):
    client, job, escrow, store = webhook_env
    response = client.post("/api/paypal/webhook", json=capture_payload(job.id),
                           headers={"paypal-transmission-id": "t1"})
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "applied"
    assert job.escrow.state is EscrowState.RELEASED
    assert job.status is JobStatus.SETTLED


def test_a_failed_signature_is_refused_and_changes_nothing(webhook_env):
    client, job, escrow, store = webhook_env
    escrow.verify_result = False
    response = client.post("/api/paypal/webhook", json=capture_payload(job.id))
    assert response.status_code == 401
    assert job.escrow.state is EscrowState.HELD
    assert store.saves == []


def test_the_signature_headers_reach_the_verifier(webhook_env):
    client, job, escrow, store = webhook_env
    client.post("/api/paypal/webhook", json=capture_payload(job.id),
                headers={"paypal-transmission-id": "txn-9"})
    assert escrow.verify_calls == 1
    assert escrow.last_headers.get("paypal-transmission-id") == "txn-9"


def test_a_redelivery_is_acknowledged_without_re_applying(webhook_env):
    client, job, escrow, store = webhook_env
    first = client.post("/api/paypal/webhook", json=capture_payload(job.id))
    entries = len(job.ledger)
    second = client.post("/api/paypal/webhook", json=capture_payload(job.id))
    assert first.json()["outcome"] == "applied"
    assert second.status_code == 200
    assert second.json()["outcome"] == "duplicate"
    assert len(job.ledger) == entries


def test_a_malformed_body_is_a_400(webhook_env):
    client, job, escrow, store = webhook_env
    response = client.post("/api/paypal/webhook", content=b"{not json",
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 400


def test_an_unconfigured_verifier_returns_503(webhook_env):
    client, job, escrow, store = webhook_env
    escrow.can_verify_webhooks = False
    response = client.post("/api/paypal/webhook", json=capture_payload(job.id))
    assert response.status_code == 503
    assert job.escrow.state is EscrowState.HELD


def test_an_unavailable_event_store_returns_503(webhook_env, monkeypatch):
    from pact.api import routes
    from pact.core.webhook_store import UnavailableWebhookStore

    client, job, escrow, store = webhook_env
    monkeypatch.setattr(routes, "webhook_store", UnavailableWebhookStore(RuntimeError("down")))
    response = client.post("/api/paypal/webhook", json=capture_payload(job.id))
    assert response.status_code == 503
    assert job.escrow.state is EscrowState.HELD


def test_the_response_never_echoes_the_payload_or_a_credential(webhook_env):
    client, job, escrow, store = webhook_env
    response = client.post("/api/paypal/webhook", json=capture_payload(job.id))
    body = response.text
    assert "CAP-1" not in body
    assert "120.00" not in body
    assert "client_secret" not in body


def test_an_event_for_another_job_is_acknowledged(webhook_env):
    client, job, escrow, store = webhook_env
    response = client.post("/api/paypal/webhook", json=capture_payload("job_someone_else"))
    assert response.status_code == 200
    assert response.json()["outcome"] == "unknown_job"
    assert job.escrow.state is EscrowState.HELD
