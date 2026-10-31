"""Security and reliability regression tests.

Each test here pins a defect that was real enough to be worth fixing before
launch. They are written against the HTTP surface, because that is the boundary
an attacker actually reaches.

The environment is fixed by ``tests/conftest.py`` (auth configured, mock PayPal
allowed, ``PACT_ENV=test``), so a token can be minted and a job can be run
without external services.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pact.api.auth import issue_token
from pact.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    """A fresh app + store. The client itself carries no credentials."""
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


def _auth(subject: str, role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(subject, role)}"}


BUYER = _auth("buyer", "buyer")
OTHER_BUYER = _auth("buyer_other", "buyer")
ADMIN = _auth("admin", "admin")
SELLER = _auth("seller_atlas", "seller")

BODY = {
    "title": "Landing page copy",
    "brief": "Write hero and feature copy for a launch page.",
    "acceptance_criteria": ["Hero under 12 words", "Three feature blocks", "Clear call to action"],
    "budget_max": 300,
}


def _create(client: TestClient, headers: dict[str, str] = BUYER) -> str:
    response = client.post("/api/jobs", json=BODY, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --------------------------------------------------------------------- auth basics


def test_health_is_public_so_a_probe_can_reach_it(client):
    """Liveness must not require a credential, or no monitor could call it."""
    assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/jobs", "get"),
        ("/api/jobs", "post"),
        ("/api/reputation", "get"),
        ("/api/ledger", "get"),
        ("/api/jobs/whatever", "get"),
    ],
)
def test_protected_routes_refuse_an_anonymous_caller(client, path, method):
    if method == "post":
        response = client.post(path, json=BODY)
    else:
        response = client.get(path)
    assert response.status_code == 401, f"{method.upper()} {path} -> {response.status_code}"


def test_a_garbage_token_is_rejected(client):
    response = client.get("/api/jobs", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_a_token_signed_with_the_wrong_secret_is_rejected(client):
    """A forged token is the whole point of signing; a bad signature must fail."""
    forged = issue_token(
        "attacker", "admin", secret="a-completely-different-secret-value-of-sufficient-length"
    )
    response = client.get("/api/jobs", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_an_expired_token_is_rejected(client):
    past = datetime.now(timezone.utc) - timedelta(days=2)
    stale = issue_token("buyer", "buyer", now=past, ttl=timedelta(hours=1))
    response = client.get("/api/jobs", headers={"Authorization": f"Bearer {stale}"})
    assert response.status_code == 401


def test_a_non_bearer_scheme_is_rejected(client):
    token = issue_token("buyer", "buyer")
    response = client.get("/api/jobs", headers={"Authorization": f"Basic {token}"})
    assert response.status_code == 401


def test_a_token_with_an_unknown_role_is_rejected(client):
    """A role that is not one of the three is refused, not silently accepted."""
    with pytest.raises(ValueError):
        issue_token("someone", "superuser")


def test_an_unsupported_signing_algorithm_fails_closed(client, monkeypatch):
    """``PACT_AUTH_ALGORITHM=none`` must never be consulted by the verifier.

    The token below is unsigned (alg ``none``); a server that honoured the
    configured algorithm would accept it as an admin.
    """
    import jwt

    from pact.config import settings as pact_settings

    forged = jwt.encode({"sub": "attacker", "role": "admin"}, key="", algorithm="none")
    original = pact_settings.auth.algorithm
    object.__setattr__(pact_settings.auth, "algorithm", "none")
    try:
        response = client.get("/api/jobs", headers={"Authorization": f"Bearer {forged}"})
    finally:
        object.__setattr__(pact_settings.auth, "algorithm", original)
    assert response.status_code == 503


def test_validate_runtime_flags_an_unsupported_algorithm():
    from pact.config import AuthSettings, Settings

    bad = Settings(
        auth=AuthSettings(secret="a-sufficiently-long-random-secret-value", algorithm="none"),
        environment="production",
    )
    assert any("PACT_AUTH_ALGORITHM" in p for p in bad.validate_runtime())


# -------------------------------------------------------------- tenant isolation


def test_a_buyer_cannot_read_another_buyers_job(client):
    """The IDOR that matters: guessing a job id must not expose someone's money."""
    job_id = _create(client, BUYER)
    response = client.get(f"/api/jobs/{job_id}", headers=OTHER_BUYER)
    assert response.status_code == 404, "a non-owner must not learn the job exists"


def test_a_buyer_cannot_run_another_buyers_job(client):
    job_id = _create(client, BUYER)
    response = client.post(f"/api/jobs/{job_id}/run", headers=OTHER_BUYER)
    assert response.status_code == 403


def test_a_buyer_cannot_delete_another_buyers_job(client):
    job_id = _create(client, BUYER)
    assert client.delete(f"/api/jobs/{job_id}", headers=OTHER_BUYER).status_code == 403


def test_the_job_list_shows_only_the_callers_own_jobs(client):
    mine_id = _create(client, BUYER)
    _create(client, OTHER_BUYER)
    mine = client.get("/api/jobs", headers=BUYER).json()["jobs"]
    assert [j["id"] for j in mine] == [mine_id]


def test_the_admin_sees_every_job(client):
    _create(client, BUYER)
    _create(client, OTHER_BUYER)
    assert client.get("/api/jobs", headers=ADMIN).json()["count"] == 2


def test_the_audit_ledger_is_scoped_to_the_caller(client):
    """The cross-job ledger must not become a cross-tenant feed.

    An unscoped feed let any authenticated principal read every other buyer's job
    titles, agreed prices, and ledger detail.
    """
    mine_id = _create(client, BUYER)
    other_id = _create(client, OTHER_BUYER)

    visible = client.get("/api/ledger", headers=BUYER).json()["entries"]
    job_ids = {row["job_id"] for row in visible}
    assert mine_id in job_ids
    assert other_id not in job_ids, "a buyer must not see another buyer's ledger"

    admin_ids = {row["job_id"] for row in client.get("/api/ledger", headers=ADMIN).json()["entries"]}
    assert {mine_id, other_id} <= admin_ids, "an admin sees everything"


# --------------------------------------------------------------- seller scoping


def test_a_seller_sees_a_job_they_were_invited_to(client):
    from pact.api import routes
    from pact.core.models import Offer

    job = routes.engine.create_job(
        title="Copy", brief="b", criteria=["x"], budget_max=100, buyer_id="buyer"
    )
    job.offers.append(Offer(seller_id="seller_atlas", price=10))
    routes.engine.store.save(job)

    ids = {j["id"] for j in client.get("/api/jobs", headers=SELLER).json()["jobs"]}
    assert job.id in ids, "an invited seller must see the job they quoted on"


def test_seller_scoping_is_not_truncated_by_the_global_limit(client):
    """A seller's older job must survive when many other jobs are newer.

    Filtering a globally-truncated page dropped a seller's older jobs once the
    total exceeded the limit, which is exactly the 10x-traffic failure mode.
    """
    from pact.api import routes

    store = routes.engine.store
    job = routes.engine.create_job(
        title="Oldest", brief="b", criteria=["x"], budget_max=100, buyer_id="buyer"
    )
    job.seller_id = "seller_nova"
    store.save(job)

    for _ in range(120):
        filler = routes.engine.create_job(
            title="Newer", brief="b", criteria=["x"], budget_max=100, buyer_id="someone"
        )
        filler.seller_id = "seller_other"
        store.save(filler)

    # The visible page is capped well below 120, yet the old seller job is found
    # because the filter runs before the limit.
    visible = store.list(limit=50, seller_id="seller_nova")
    assert job.id in {j.id for j in visible}


def test_a_seller_does_not_see_a_job_they_are_not_party_to(client):
    from pact.api import routes

    job = routes.engine.create_job(
        title="Not yours", brief="b", criteria=["x"], budget_max=100, buyer_id="buyer"
    )
    job.seller_id = "seller_other"
    routes.engine.store.save(job)
    assert job.id not in {j["id"] for j in client.get("/api/jobs", headers=SELLER).json()["jobs"]}


# --------------------------------------------------------- role separation (admin)


def test_a_buyer_cannot_reconcile(client):
    """Reconciliation can settle real money, so it is admin-only."""
    job_id = _create(client)
    response = client.post(f"/api/jobs/{job_id}/reconcile", headers=BUYER)
    assert response.status_code == 403


def test_a_buyer_cannot_delete_a_job(client):
    job_id = _create(client)
    assert client.delete(f"/api/jobs/{job_id}", headers=BUYER).status_code == 403


def test_a_buyer_cannot_read_the_provider_order(client):
    """The paypal-order route uses the deployment's own provider credentials."""
    job_id = _create(client)
    assert client.get(f"/api/jobs/{job_id}/paypal-order", headers=BUYER).status_code == 403


# ------------------------------------------------------------ identity from token


def test_buyer_id_in_the_body_is_ignored(client):
    """A caller cannot post a job *as* someone else.

    ``buyer_id`` is still accepted for compatibility, but the principal from the
    verified token overwrites it.
    """
    response = client.post("/api/jobs", json={**BODY, "buyer_id": "someone_else"}, headers=BUYER)
    assert response.status_code == 201
    assert response.json()["buyer_id"] == "buyer"


def test_a_new_job_on_a_mock_deployment_is_not_reported_as_real(client):
    """A job created against the mock must never claim its settlement is real.

    The flag is stamped from the live escrow client at creation, not left at the
    record's optimistic default, so the very first response a consumer sees is
    honest.
    """
    created = client.post("/api/jobs", json=BODY, headers=BUYER).json()
    assert created["simulated"] is True
    assert created["settlement_is_real"] is False
    assert created["escrow_mode"] == "mock"


# ------------------------------------------------------------- load behaviour


def test_excess_runs_are_refused_rather_than_queued_without_bound(tmp_path, monkeypatch):
    """Under 10x traffic a run must shed load, not pile up unboundedly.

    With every run slot held, a new run is refused with ``ServerBusy`` (surfaced
    as 429) instead of waiting forever behind a queue.
    """
    import threading
    import time

    from pact.core.engine import PactEngine, ServerBusy
    from pact.core.llm import MockLLM
    from pact.core.store import JobStore, ReputationLedger
    from pact.paypal import MockPayPalEscrowClient

    engine = PactEngine(
        llm=MockLLM(),
        escrow=MockPayPalEscrowClient(),
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
    )
    job = engine.create_job(title="Probe", brief="x" * 20, criteria=["c"], budget_max=100)

    holders = [threading.Thread(target=lambda: (engine._run_slots.acquire(), time.sleep(0.8)))
               for _ in range(engine._run_slots._value)]
    for t in holders:
        t.start()
    time.sleep(0.1)
    try:
        with pytest.raises(ServerBusy):
            engine.run(job)
    finally:
        for t in holders:
            t.join()


def test_a_run_that_overruns_its_deadline_is_abandoned_not_wedged(tmp_path):
    """A run past its deadline must leave the job reclaimable, not stuck.

    The worker thread cannot be killed, so the job is marked failed and its lease
    released; otherwise the job would sit in ``negotiating`` forever with no
    worker and no expiry.
    """
    import time

    from pact.config import settings as pact_settings
    from pact.core.engine import PactEngine, RunDeadlineExceeded
    from pact.core.llm import MockLLM
    from pact.core.models import JobStatus
    from pact.core.store import JobStore, ReputationLedger
    from pact.paypal import MockPayPalEscrowClient

    engine = PactEngine(
        llm=MockLLM(),
        escrow=MockPayPalEscrowClient(),
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
    )
    job = engine.create_job(title="Probe", brief="x" * 20, criteria=["c"], budget_max=100)

    def hang(*_args, **_kwargs):
        time.sleep(30)

    original_inner = engine._run_inner
    original_timeout = pact_settings.run_timeout_seconds
    engine._run_inner = hang
    object.__setattr__(pact_settings, "run_timeout_seconds", 1)
    try:
        with pytest.raises(RunDeadlineExceeded):
            engine.run(job)
    finally:
        engine._run_inner = original_inner
        object.__setattr__(pact_settings, "run_timeout_seconds", original_timeout)

    fresh = engine.store.get(job.id)
    assert fresh.status is JobStatus.FAILED
    assert fresh.run_lease_expires_at is None, "the lease must be released so the job is reclaimable"


# ------------------------------------------------------- deletion safety (money)


def test_a_job_with_a_live_hold_cannot_be_deleted(client):
    """Deleting a job holding funds would strand the buyer's money."""
    job_id = _create(client)
    # Run to a state that holds funds. seller_quill fails verification -> voided,
    # which is terminal; use the default sellers and accept either terminal state,
    # then assert the *pre-terminal* case directly.
    from pact.api import routes
    from pact.core.models import EscrowState, JobStatus

    job = routes.engine.store.get(job_id)
    job.escrow.state = EscrowState.HELD
    job.status = JobStatus.ESCROWED
    routes.engine.store.save(job)

    response = client.delete(f"/api/jobs/{job_id}", headers=ADMIN)
    assert response.status_code == 409
    assert "cancel" in response.json()["detail"].lower()


def test_a_resolved_job_can_be_deleted_and_then_reads_as_missing(client):
    job_id = _create(client)
    assert client.post(f"/api/jobs/{job_id}/run", headers=BUYER).status_code == 200

    assert client.delete(f"/api/jobs/{job_id}", headers=ADMIN).status_code == 204
    # Tombstoned, so it is indistinguishable from a job that never existed.
    assert client.get(f"/api/jobs/{job_id}", headers=BUYER).status_code == 404


# -------------------------------------------------------------------- SSE stream


def test_the_stream_requires_a_token(client):
    job_id = _create(client)
    assert client.get(f"/api/jobs/{job_id}/stream").status_code == 401


def test_the_stream_rejects_a_full_token_in_the_query(client):
    """A real token in the URL would leak to logs/history/Referer; only a ticket is allowed."""
    job_id = _create(client)
    token = issue_token("buyer", "buyer")
    assert client.get(f"/api/jobs/{job_id}/stream?token={token}").status_code == 401


def test_the_stream_accepts_a_scoped_ticket_minted_for_the_job(client):
    job_id = _create(client)
    ticket = client.post(f"/api/jobs/{job_id}/stream-ticket", headers=BUYER).json()["ticket"]
    response = client.get(f"/api/jobs/{job_id}/stream?ticket={ticket}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")


def test_a_stream_ticket_for_one_job_cannot_open_another(client):
    job_id = _create(client)
    other_id = _create(client, OTHER_BUYER)
    ticket = client.post(f"/api/jobs/{job_id}/stream-ticket", headers=BUYER).json()["ticket"]
    assert client.get(f"/api/jobs/{other_id}/stream?ticket={ticket}").status_code == 403


def test_a_stream_ticket_cannot_be_replayed_against_a_normal_route(client):
    """A leaked URL must not become a usable credential for the money routes."""
    job_id = _create(client)
    ticket = client.post(f"/api/jobs/{job_id}/stream-ticket", headers=BUYER).json()["ticket"]
    assert client.get("/api/jobs", headers={"Authorization": f"Bearer {ticket}"}).status_code == 401


def test_a_non_owner_cannot_mint_a_stream_ticket(client):
    job_id = _create(client, BUYER)
    assert client.post(f"/api/jobs/{job_id}/stream-ticket", headers=OTHER_BUYER).status_code == 404


def test_the_stream_still_accepts_a_bearer_header(client):
    """A non-browser client can keep using the header."""
    job_id = _create(client)
    response = client.get(f"/api/jobs/{job_id}/stream", headers=BUYER)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")


# ------------------------------------------------------------- request validation


def test_an_oversized_body_is_rejected_before_parsing(client):
    huge = "x" * (70 * 1024)
    response = client.post("/api/jobs", json={**BODY, "brief": huge}, headers=BUYER)
    assert response.status_code == 413


def test_an_oversized_chunked_body_is_also_rejected(client):
    """A body with no Content-Length must not slip past the cap.

    A streaming (chunked) request declares no size, so a Content-Length check
    alone let an arbitrarily large body be buffered in memory — a one-request
    memory-exhaustion DoS. The cap must hold on the bytes actually received.
    """
    def body():
        yield b'{"title":"Valid title","brief":"'
        for _ in range(200):
            yield b"x" * 4096
        yield b'","acceptance_criteria":["c"],"budget_max":1}'

    response = client.post(
        "/api/jobs", content=body(), headers={**BUYER, "Content-Type": "application/json"}
    )
    assert response.status_code == 413


def test_too_many_criteria_are_rejected(client):
    response = client.post(
        "/api/jobs",
        json={**BODY, "acceptance_criteria": [f"criterion {i}" for i in range(50)]},
        headers=BUYER,
    )
    assert response.status_code == 422


def test_a_non_finite_budget_is_a_422_not_a_500(client):
    raw = b'{"title":"Valid title","brief":"Valid brief","acceptance_criteria":["c"],"budget_max":1e999}'
    response = client.post(
        "/api/jobs", content=raw, headers={**BUYER, "Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert "Internal error" not in response.text


# ------------------------------------------------------------- config fail-closed


def test_runtime_validation_flags_a_production_deployment_without_auth():
    """The startup check must refuse a production deployment with no secret."""
    import dataclasses

    from pact.config import AuthSettings, Settings

    broken = Settings(
        auth=AuthSettings(secret=""),
        environment="production",
        allow_mock_paypal=True,
        paypal=dataclasses.replace(settings.paypal, client_id="", client_secret=""),
    )
    problems = broken.validate_runtime()
    assert any("PACT_AUTH_SECRET" in p for p in problems)
    assert any("mock" in p.lower() for p in problems)


def test_runtime_validation_is_quiet_for_a_sound_production_deployment():
    import dataclasses

    from pact.config import AuthSettings, Settings

    sound = Settings(
        auth=AuthSettings(secret="a-sufficiently-long-random-secret-value"),
        environment="production",
        allow_mock_paypal=False,
        paypal=dataclasses.replace(settings.paypal, client_id="id", client_secret="secret"),
    )
    assert sound.validate_runtime() == []


def test_runtime_validation_rejects_a_relative_public_url():
    from pact.config import AuthSettings, Settings

    bad = Settings(
        auth=AuthSettings(secret="a-sufficiently-long-random-secret-value"),
        environment="development",
        public_url="pact.example",
    )
    assert any("PACT_PUBLIC_URL" in p for p in bad.validate_runtime())
