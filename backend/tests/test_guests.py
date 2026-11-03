"""The public judge (guest) experience, exercised through the real HTTP surface.

A judge must be able to open the deployed product and use it with no credential
to supply, and must not be able to reach any other visitor's records or any
operator capability. These tests hold both halves of that in place:

* **zero-friction access** — a session is provisioned on request, with no token,
  form, or email step;
* **isolation and least privilege** — a guest is an ordinary buyer scoped to its
  own workspace, refused on every admin route, and refused on another guest's
  records with a 404 (not a 403 that would confirm the record exists);
* **the money-route protections are unchanged** — CSRF on cookie-authenticated
  mutations, the guest session is cookie-only (never a bearer token), and the
  existing bearer/operator paths still work exactly as before.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from pact.config import settings

BODY = {
    "title": "Landing page copy",
    "brief": "Write hero and feature copy for a launch page.",
    "acceptance_criteria": ["Hero under 12 words", "Three feature blocks", "Clear call to action"],
    "budget_max": 300,
}

GUESTS_ON = dataclasses.replace(
    settings, auth=dataclasses.replace(settings.auth, guests_enabled=True)
)


def _enable_guests(monkeypatch) -> None:
    """Enable guest sessions on the *live* settings objects the routes consult.

    ``settings`` is frozen and read at import time, so the enabled object is
    patched onto each module that reads it rather than mutated.
    """
    import pact.api.auth as auth
    import pact.api.auth_routes as auth_routes
    import pact.api.routes as routes
    import pact.core.guests as guests

    for module in (auth, auth_routes, routes, guests):
        monkeypatch.setattr(module, "settings", GUESTS_ON)


@pytest.fixture()
def store(monkeypatch):
    """A fresh, process-local auth store so guest counters do not leak between tests."""
    import pact.core.accounts as accounts_mod
    import pact.core.auth_store as auth_store_mod
    import pact.core.guests as guests_mod
    from pact.core.auth_store import InMemoryAuthStore

    fresh = InMemoryAuthStore()
    monkeypatch.setattr(auth_store_mod, "auth_store", fresh)
    monkeypatch.setattr(accounts_mod.accounts, "store", fresh)
    monkeypatch.setattr(guests_mod.guests, "store", fresh)
    return fresh


@pytest.fixture()
def client(tmp_path, monkeypatch, store) -> TestClient:
    """A fresh app + engine, with guest sessions enabled and no bearer credential."""
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
    _enable_guests(monkeypatch)
    return TestClient(app, base_url="https://testserver")


def _new_guest(client: TestClient) -> dict:
    response = client.post("/api/auth/guest", json={})
    assert response.status_code == 201, response.text
    return response.json()


def _csrf_headers(session: dict) -> dict[str, str]:
    return {"X-CSRF-Token": session["csrf_token"]}


# --------------------------------------------------------------------- disabled


def test_the_guest_surface_is_inert_until_enabled(client, monkeypatch):
    """Off by default: a deployment that did not opt in must not serve guests."""
    import dataclasses as dc

    import pact.api.auth_routes as auth_routes

    monkeypatch.setattr(
        auth_routes, "settings", dc.replace(settings, auth=dc.replace(settings.auth, guests_enabled=False))
    )
    assert client.post("/api/auth/guest", json={}).status_code == 404


# ------------------------------------------------------------ zero-friction access


def test_a_first_visit_provisions_a_workspace_with_no_credential(client):
    """The whole onboarding is one request, and it carries no credential."""
    session = _new_guest(client)
    assert session["type"] == "guest"
    assert session["role"] == "buyer"
    assert session["subject"].startswith("gst_")
    assert session["csrf_token"]

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["subject"] == session["subject"]
    assert me.json()["email_verified"] is True


def test_the_session_cookie_is_hardened(client):
    response = client.post("/api/auth/guest", json={})
    cookie = response.headers.get("set-cookie", "")
    assert "pact_guest=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/api" in cookie
    # The CSRF token is never placed in a cookie; it travels only in the body.
    assert response.json()["csrf_token"] not in cookie


def test_a_guest_can_use_the_product_end_to_end(client):
    """Create a job, run it to a terminal state, and read its own records."""
    session = _new_guest(client)
    created = client.post("/api/jobs", json=BODY, headers=_csrf_headers(session))
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["buyer_id"] == session["subject"]

    ran = client.post(f"/api/jobs/{job['id']}/run", headers=_csrf_headers(session))
    assert ran.status_code == 200, ran.text
    final = client.get(f"/api/jobs/{job['id']}").json()
    assert final["status"] in {"settled", "voided"}
    assert final["ledger_chain_valid"] is True
    # The ledger (the receipt of what happened) is readable by its owner.
    assert any(e["kind"] == "job_created" for e in final["ledger"])


def test_a_guest_can_refund_its_own_settled_payment(client):
    session = _new_guest(client)
    job_id = client.post("/api/jobs", json=BODY, headers=_csrf_headers(session)).json()["id"]
    client.post(f"/api/jobs/{job_id}/run", headers=_csrf_headers(session))
    job = client.get(f"/api/jobs/{job_id}").json()
    if job["escrow"]["state"] != "released":
        pytest.skip("the deterministic mock did not settle this run")
    refunded = client.post(
        f"/api/jobs/{job_id}/refund", json={"amount": None, "reason": "judge check"},
        headers=_csrf_headers(session),
    )
    assert refunded.status_code == 200, refunded.text
    assert refunded.json()["escrow"]["state"] in {"refunded", "released"}


# ------------------------------------------------------------------- isolation


def test_a_guest_cannot_read_another_guests_records(client):
    a = _new_guest(client)
    job_id = client.post("/api/jobs", json=BODY, headers=_csrf_headers(a)).json()["id"]

    other = TestClient(client.app, base_url="https://testserver")
    b = _new_guest(other)
    assert b["subject"] != a["subject"]

    # 404, not 403: the API does not confirm that another workspace's job exists.
    assert other.get(f"/api/jobs/{job_id}").status_code == 404
    assert other.get("/api/jobs").json()["count"] == 0


def test_a_guest_cannot_drive_another_guests_job(client):
    a = _new_guest(client)
    job_id = client.post("/api/jobs", json=BODY, headers=_csrf_headers(a)).json()["id"]

    other = TestClient(client.app, base_url="https://testserver")
    b = _new_guest(other)
    # A mutation on a record the caller does not own is refused (403). The read
    # route is the one that deliberately answers 404 to avoid confirming that the
    # record exists; see test_a_guest_cannot_read_another_guests_records.
    assert other.post(f"/api/jobs/{job_id}/run", headers=_csrf_headers(b)).status_code == 403
    assert other.post(f"/api/jobs/{job_id}/cancel", headers=_csrf_headers(b)).status_code == 403
    assert other.post(f"/api/jobs/{job_id}/refund", headers=_csrf_headers(b)).status_code == 403


def test_a_guest_cannot_reach_operator_routes(client):
    session = _new_guest(client)
    job_id = client.post("/api/jobs", json=BODY, headers=_csrf_headers(session)).json()["id"]
    # Reconciliation and the provider order record are admin-only.
    assert client.post(f"/api/jobs/{job_id}/reconcile", headers=_csrf_headers(session)).status_code == 403
    assert client.get(f"/api/jobs/{job_id}/paypal-order").status_code == 403
    assert client.delete(f"/api/jobs/{job_id}", headers=_csrf_headers(session)).status_code == 403


# ------------------------------------------------------------------------ CSRF


def test_a_cookie_authenticated_mutation_without_csrf_is_refused(client):
    _new_guest(client)
    assert client.post("/api/jobs", json=BODY).status_code == 403


def test_a_wrong_csrf_token_is_refused(client):
    _new_guest(client)
    assert client.post("/api/jobs", json=BODY, headers={"X-CSRF-Token": "not-the-token"}).status_code == 403


def test_a_read_does_not_require_csrf(client):
    _new_guest(client)
    assert client.get("/api/jobs").status_code == 200


def test_a_bearer_request_is_exempt_from_csrf(client):
    """An operator token is header-authenticated, so CSRF cannot apply to it."""
    from pact.api.auth import issue_token

    headers = {"Authorization": f"Bearer {issue_token('buyer', 'buyer')}"}
    assert client.post("/api/jobs", json=BODY, headers=headers).status_code == 201


# ---------------------------------------------------------- session integrity


def test_a_tampered_session_cookie_is_refused(client):
    _new_guest(client)
    raw = client.cookies.get("pact_guest")
    client.cookies.set("pact_guest", raw[:-3] + "aaa")
    assert client.get("/api/jobs").status_code == 401


def test_an_expired_session_is_refused(client, store):
    """A session past its expiry is refused even though its signature is valid."""
    import pact.core.guests as guests_mod

    user = guests_mod.guests.provision()
    token, _csrf, _exp = guests_mod.issue_guest_session(
        user.id, token_version=user.token_version, ttl_hours=-1
    )
    client.cookies.set("pact_guest", token)
    assert client.get("/api/jobs").status_code == 401


def test_a_revoked_session_is_refused(client, store):
    """Bumping the account token version invalidates every outstanding session."""
    session = _new_guest(client)
    assert client.get("/api/jobs").status_code == 200
    store.update_user(session["subject"], token_version=1)
    assert client.get("/api/jobs").status_code == 401


def test_a_guest_session_is_not_accepted_as_a_bearer_token(client):
    """The cookie credential must not become a header credential.

    Accepting it as a bearer token would create a path around the CSRF check.
    """
    _new_guest(client)
    raw = client.cookies.get("pact_guest")
    headers = {"Authorization": f"Bearer {raw}"}
    fresh = TestClient(client.app, base_url="https://testserver")
    assert fresh.get("/api/jobs", headers=headers).status_code == 401


def test_a_missing_session_is_a_401(client):
    assert client.get("/api/jobs").status_code == 401


def test_guest_logout_clears_the_session(client):
    _new_guest(client)
    assert client.post("/api/auth/guest/logout").status_code == 204
    # The client drops the cleared cookie on the next request.
    assert client.get("/api/jobs").status_code == 401


# --------------------------------------------------------------- abuse controls


def test_a_guest_job_quota_is_enforced(client, monkeypatch):
    import dataclasses as dc

    import pact.api.routes as routes
    import pact.core.guests as guests_mod

    small = dc.replace(settings, auth=dc.replace(settings.auth, guests_enabled=True, guest_max_jobs=2))
    monkeypatch.setattr(routes, "settings", small)
    monkeypatch.setattr(guests_mod, "settings", small)

    session = _new_guest(client)
    for _ in range(2):
        assert client.post("/api/jobs", json=BODY, headers=_csrf_headers(session)).status_code == 201
    assert client.post("/api/jobs", json=BODY, headers=_csrf_headers(session)).status_code == 429


def test_new_guest_sessions_are_rate_limited_per_address(client, monkeypatch):
    import dataclasses as dc

    import pact.api.auth_routes as auth_routes

    capped = dc.replace(
        settings, auth=dc.replace(settings.auth, guests_enabled=True, guest_new_per_ip=2)
    )
    monkeypatch.setattr(auth_routes, "settings", capped)
    assert client.post("/api/auth/guest", json={}).status_code == 201
    assert client.post("/api/auth/guest", json={}).status_code == 201
    assert client.post("/api/auth/guest", json={}).status_code == 429


def test_the_per_address_cap_uses_the_proxy_written_entry(client, monkeypatch):
    """A forged leftmost ``x-forwarded-for`` entry must not reset the counter.

    Vercel's edge overwrites this header, so the *rightmost* value is the one the
    platform wrote. Reading the leftmost (client-supplied) entry would let a caller
    mint unlimited workspaces by sending a fresh fake value each time.
    """
    import dataclasses as dc

    import pact.api.auth_routes as auth_routes

    capped = dc.replace(
        settings, auth=dc.replace(settings.auth, guests_enabled=True, guest_new_per_ip=2)
    )
    monkeypatch.setattr(auth_routes, "settings", capped)

    real = "203.0.113.9"
    for forged in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        response = client.post(
            "/api/auth/guest", json={}, headers={"x-forwarded-for": f"{forged}, {real}"}
        )
    # Three attempts from the same real address: the third is refused even though
    # every request carried a different leftmost value.
    assert response.status_code == 429


# --------------------------------------------------- sandbox buyer hint (gated)


def test_a_sandbox_buyer_hint_is_offered_to_a_guest_when_configured(client, monkeypatch):
    """A judge has no sandbox buyer account, so the approval step needs one.

    The hint is the whole point of the public demo: without it the real PayPal
    flow stops at "buyer approval required" with nothing to click.
    """
    import dataclasses as dc

    import pact.api.auth_routes as auth_routes

    configured = dc.replace(
        settings,
        auth=dc.replace(settings.auth, guests_enabled=True),
        paypal=dc.replace(
            settings.paypal,
            environment="sandbox",
            base_url_override="",
            sandbox_buyer_email="sb-buyer@personal.example.com",
            sandbox_buyer_password="hunter2",
        ),
    )
    monkeypatch.setattr(auth_routes, "settings", configured)

    body = _new_guest(client)
    assert body["sandbox_buyer"] == {
        "email": "sb-buyer@personal.example.com",
        "password": "hunter2",
        "note": "PayPal Sandbox test buyer — no real money is involved.",
    }


def test_the_sandbox_buyer_hint_is_withheld_on_a_live_host(client, monkeypatch):
    """A live-money deployment must never hand out a buyer credential."""
    import dataclasses as dc

    import pact.api.auth_routes as auth_routes

    live = dc.replace(
        settings,
        auth=dc.replace(settings.auth, guests_enabled=True),
        paypal=dc.replace(
            settings.paypal,
            environment="live",
            base_url_override="",
            sandbox_buyer_email="sb-buyer@personal.example.com",
            sandbox_buyer_password="hunter2",
        ),
    )
    monkeypatch.setattr(auth_routes, "settings", live)

    assert _new_guest(client)["sandbox_buyer"] is None


def test_the_sandbox_buyer_hint_is_absent_when_unset(client):
    assert _new_guest(client)["sandbox_buyer"] is None
