"""API contract tests, exercised through the real FastAPI app.

These call the HTTP surface the judges will use, so they cover routing, request
parsing, status codes, and response shape — not just the engine underneath.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    """A client backed by a fresh store, with no LLM and no PayPal credentials.

    The client carries a signed *buyer* token by default, so the tests below read
    as the ordinary buyer flow. Tests that need another role pass ``headers=``
    explicitly, which replaces the default for that call.
    """
    from pact import config
    from pact.api import routes
    from pact.api.auth import issue_token
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
    test_client = TestClient(app)
    test_client.headers.update({"Authorization": f"Bearer {issue_token('buyer', 'buyer')}"})
    return test_client


def _admin_headers() -> dict[str, str]:
    from pact.api.auth import issue_token

    return {"Authorization": f"Bearer {issue_token('admin', 'admin')}"}


BODY = {
    "title": "Landing page copy",
    "brief": "Write hero and feature copy for a launch page.",
    "acceptance_criteria": ["Hero under 12 words", "Three feature blocks", "Clear call to action"],
    "budget_max": 300,
}


def create(client: TestClient, **overrides) -> str:
    payload = {**BODY, **overrides}
    response = client.post("/api/jobs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_health_reports_modes(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["paypal"]["mode"] == "mock"
    assert body["llm"]["is_mock"] is True


def test_root_describes_the_api_in_dev_mode(client):
    """With no built frontend present, / is a JSON description, not an error."""
    body = client.get("/").json()
    assert body["name"] == "Pact"
    assert body["health"] == "/api/health"


def test_create_job_validates_input(client):
    assert client.post("/api/jobs", json={**BODY, "title": "ab"}).status_code == 422
    assert client.post("/api/jobs", json={**BODY, "budget_max": 0}).status_code == 422
    assert client.post("/api/jobs", json={**BODY, "acceptance_criteria": []}).status_code == 422


def test_a_full_run_settles_or_voids_and_keeps_the_chain_valid(client):
    job_id = create(client)
    assert client.post(f"/api/jobs/{job_id}/run").status_code == 200
    job = client.get(f"/api/jobs/{job_id}").json()

    assert job["status"] in {"settled", "voided"}
    assert job["escrow"]["state"] in {"released", "voided"}
    assert job["ledger_chain_valid"] is True
    assert job["escrow"]["mode"] == "mock"


def test_seller_selection_accepts_the_comma_form_the_ui_sends(client):
    """The browser sends ?seller_ids=a,b — that must actually restrict the run.

    Regression guard: declared as a bare ``list[str]`` this route parsed the value
    as a body field, so the query parameter was dropped and every run invited all
    sellers. ``Query()`` is what makes the comma form work.
    """
    job_id = create(client)
    response = client.post(f"/api/jobs/{job_id}/run?seller_ids=seller_nova")
    assert response.status_code == 200
    job = response.json()
    invited = next(e for e in job["ledger"] if e["kind"] == "negotiation_started")
    assert "seller_nova" in invited["summary"]
    assert "seller_atlas" not in invited["summary"]
    assert "seller_quill" not in invited["summary"]
    assert job["seller_id"] == "seller_nova"


def test_seller_selection_accepts_the_repeated_form(client):
    job_id = create(client)
    response = client.post(f"/api/jobs/{job_id}/run?seller_ids=seller_atlas&seller_ids=seller_nova")
    assert response.status_code == 200
    invited = next(e for e in response.json()["ledger"] if e["kind"] == "negotiation_started")
    assert "seller_atlas" in invited["summary"]
    assert "seller_nova" in invited["summary"]
    assert "seller_quill" not in invited["summary"]


def test_an_unknown_seller_is_rejected(client):
    job_id = create(client)
    response = client.post(f"/api/jobs/{job_id}/run?seller_ids=evil_seller")
    assert response.status_code == 400
    assert "evil_seller" in response.json()["detail"]


def test_an_unknown_seller_in_a_comma_list_is_rejected(client):
    job_id = create(client)
    response = client.post(f"/api/jobs/{job_id}/run?seller_ids=seller_atlas,evil_seller")
    assert response.status_code == 400


def test_running_a_finished_job_is_a_conflict(client):
    job_id = create(client)
    client.post(f"/api/jobs/{job_id}/run")
    assert client.post(f"/api/jobs/{job_id}/run").status_code == 409


def test_approving_a_job_that_needs_no_approval_is_a_conflict(client):
    job_id = create(client)
    client.post(f"/api/jobs/{job_id}/run")
    response = client.post(f"/api/jobs/{job_id}/approve")
    assert response.status_code == 409


def test_an_unknown_job_is_a_404(client):
    assert client.get("/api/jobs/job_nope").status_code == 404
    assert client.post("/api/jobs/job_nope/run").status_code == 404


def test_an_unknown_api_path_returns_json_not_the_spa(client):
    """A bad API path must not be swallowed by a catch-all static mount."""
    response = client.get("/api/definitely-not-a-route")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_the_ledger_is_append_only_across_a_run(client):
    job_id = create(client)
    client.post(f"/api/jobs/{job_id}/run")
    entries = client.get(f"/api/jobs/{job_id}").json()["ledger"]
    seqs = [e["seq"] for e in entries]
    assert seqs == list(range(len(seqs))), "sequence numbers must be contiguous"
    for prev, cur in zip(entries, entries[1:]):
        assert cur["prev_hash"] == prev["hash"], "each entry must chain to the previous"
    assert entries[0]["prev_hash"] == ""


def test_reputation_only_reflects_real_outcomes(client):
    job_id = create(client)
    client.post(f"/api/jobs/{job_id}/run")
    job = client.get(f"/api/jobs/{job_id}").json()
    agents = {
        a["agent_id"]: a
        for a in client.get("/api/reputation").json()["agents"]
    }

    winner = agents[job["seller_id"]]
    if job["status"] == "settled":
        assert winner["settled"] == 1
    elif job["status"] == "voided":
        assert winner["voided"] == 1
        assert winner["settled"] == 0


def test_a_voided_job_records_no_capture(client):
    """End to end: a failed delivery must leave no capture id anywhere."""
    job_id = create(client)
    client.post(f"/api/jobs/{job_id}/run?seller_ids=seller_quill")
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["verification"]["passed"] is False
    assert job["escrow"]["capture_id"] == ""
    assert job["status"] == "voided"
    kinds = [e["kind"] for e in job["ledger"]]
    assert "escrow_voided" in kinds
    assert "escrow_released" not in kinds


def test_jobs_list_and_global_ledger(client):
    create(client)
    assert len(client.get("/api/jobs").json()["jobs"]) == 1
    assert "entries" in client.get("/api/ledger").json()


def test_non_finite_budget_is_a_422_not_a_500(client):
    """A rejected input must stay a rejection.

    Pydantic echoes the offending value back in the validation error. For
    ``1e999`` that value is ``inf``, and Starlette renders JSON with
    ``allow_nan=False``, so serialising the 422 raised ValueError and became a
    500. The handler now sanitises non-finite floats before rendering.
    """
    for raw in (b'{"title":"Valid title","brief":"Valid brief","acceptance_criteria":["c"],"budget_max":1e999}',
                b'{"title":"Valid title","brief":"Valid brief","acceptance_criteria":["c"],"budget_max":-1e999}'):
        response = client.post("/api/jobs", content=raw, headers={"Content-Type": "application/json"})
        assert response.status_code == 422, response.text
        assert "Internal error" not in response.text
        assert response.json()["detail"], "the rejection must explain itself"


def test_an_ordinary_invalid_budget_is_still_rejected(client):
    for value in ('"abc"', "null", "-5", '"NaN"', '"Infinity"'):
        response = client.post(
            "/api/jobs",
            content=f'{{"title":"Valid title","brief":"Valid brief","acceptance_criteria":["c"],"budget_max":{value}}}'.encode(),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422, f"{value} -> {response.status_code}"


def test_reconcile_is_refused_for_a_job_that_is_not_unresolved(client):
    """The endpoint must not be usable to re-run an ordinary settlement."""
    job_id = create(client)
    assert client.post(f"/api/jobs/{job_id}/run").status_code == 200

    response = client.post(f"/api/jobs/{job_id}/reconcile", headers=_admin_headers())
    assert response.status_code == 409, response.text
    assert "unresolved" in response.json()["detail"]


def test_reconcile_resolves_a_job_whose_capture_outcome_was_lost(client):
    """End to end through HTTP: an ambiguous capture is resolved by asking PayPal."""
    from decimal import Decimal

    from pact.api import routes
    from pact.paypal import AmbiguousOutcome

    engine = routes.engine
    escrow = engine.escrow

    # A job holding funds, ready to settle. Built through the engine so the test
    # exercises the reconcile endpoint rather than the verification heuristic.
    job = engine.create_job(title="Reconcile", brief="b", criteria=["c"],
                            budget_max=Decimal("100"))
    job.seller_id = "seller_nova"
    job.agreed_price = Decimal("50.00")
    engine.settlement.hold(job)
    engine.store.save(job)

    original = escrow.capture_authorization

    def apply_then_lose(authorization_id, **kwargs):
        capture = original(authorization_id, **kwargs)
        raise AmbiguousOutcome(f"capture {capture.id} applied, response lost", code="TIMEOUT")

    escrow.capture_authorization = apply_then_lose
    try:
        engine.settlement.release(job)
    except AmbiguousOutcome:
        pass
    finally:
        escrow.capture_authorization = original

    unresolved = client.get(f"/api/jobs/{job.id}").json()
    assert unresolved["status"] == "unresolved", unresolved
    assert unresolved["escrow"]["capture_id"] == "pending"

    reconciled = client.post(f"/api/jobs/{job.id}/reconcile", headers=_admin_headers())
    assert reconciled.status_code == 200, reconciled.text
    result = reconciled.json()
    assert result["status"] == "settled", result
    assert result["escrow"]["capture_status"] == "COMPLETED"
    assert result["escrow"]["capture_id"].startswith("MOCK-CAPTURE-")


def test_health_reports_storage_backend(client):
    body = client.get("/api/health").json()
    assert body["storage"]["backend"] == "filesystem"
    assert "durable" in body["storage"]
    assert body["paypal"]["mode"] == "mock"
    assert "require_real" in body["paypal"]
