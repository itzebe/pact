"""Money-route rate limiting.

Two properties matter. First, the limiter itself must count correctly and must
*refuse* rather than allow when it cannot make a decision — a limit that silently
stops applying under load is worse than a brief refusal on a money route. Second,
the route wiring must actually apply the limit and must key it on an identity a
caller cannot trivially rotate (a guest cannot reset its budget by minting a fresh
session). The cross-instance property is proven separately, against real
PostgreSQL, in ``test_rate_limit_postgres.py``.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from pact.core.rate_limit import (
    InMemoryRateLimiter,
    RateLimitRule,
    UnavailableRateLimiter,
    identity_for,
)


# ------------------------------------------------------------------ the limiter


def test_allows_up_to_the_limit_then_refuses():
    limiter = InMemoryRateLimiter()
    rule = RateLimitRule(limit=3, window_seconds=300)
    for i in range(3):
        decision = limiter.check("s", "k", rule)
        assert decision.allowed, f"request {i + 1} should be allowed"
        assert decision.remaining == 2 - i
    refused = limiter.check("s", "k", rule)
    assert refused.allowed is False
    assert refused.remaining == 0


def test_a_refused_request_carries_a_positive_retry_after():
    limiter = InMemoryRateLimiter()
    rule = RateLimitRule(limit=1, window_seconds=300)
    limiter.check("s", "k", rule)
    refused = limiter.check("s", "k", rule)
    assert refused.allowed is False
    assert refused.retry_after >= 1, "never tell a client to retry in zero seconds"
    assert refused.headers()["Retry-After"] == str(refused.retry_after)


def test_distinct_keys_have_independent_budgets():
    limiter = InMemoryRateLimiter()
    rule = RateLimitRule(limit=1, window_seconds=300)
    assert limiter.check("s", "a", rule).allowed is True
    assert limiter.check("s", "b", rule).allowed is True
    assert limiter.check("s", "a", rule).allowed is False


def test_distinct_scopes_do_not_share_a_budget():
    limiter = InMemoryRateLimiter()
    rule = RateLimitRule(limit=1, window_seconds=300)
    assert limiter.check("run", "k", rule).allowed is True
    assert limiter.check("refund", "k", rule).allowed is True


def test_the_window_slides_so_an_old_request_stops_counting():
    limiter = InMemoryRateLimiter()
    rule = RateLimitRule(limit=1, window_seconds=1)
    assert limiter.check("s", "k", rule).allowed is True
    assert limiter.check("s", "k", rule).allowed is False
    time.sleep(1.05)
    assert limiter.check("s", "k", rule).allowed is True, "the window must expire"


def test_an_unavailable_shared_limiter_refuses_everything():
    """A shared limit that cannot be evaluated must refuse, not allow."""
    limiter = UnavailableRateLimiter(RuntimeError("db down"))
    decision = limiter.check("s", "k", RateLimitRule(limit=10, window_seconds=300))
    assert decision.allowed is False
    assert decision.scope == "unavailable"


# ---------------------------------------------------------------- the identity


def test_an_authenticated_identity_is_its_subject():
    assert identity_for("buyer_1", False, "iphash") == "u:buyer_1"


def test_a_guest_identity_is_the_address_not_the_subject():
    """A guest cannot reset its budget by minting a fresh session.

    Two guest subjects from one address map to the same identity, so the second
    inherits the first's spent budget.
    """
    first = identity_for("gst_1", True, "iphash")
    second = identity_for("gst_2", True, "iphash")
    assert first == second == "g:iphash"


def test_an_anonymous_identity_is_the_address():
    assert identity_for(None, False, "iphash") == "ip:iphash"


# ----------------------------------------------------------------- the routes


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
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


BODY = {
    "title": "Landing page copy",
    "brief": "Write hero and feature copy for a launch page.",
    "acceptance_criteria": ["Hero under 12 words", "Three feature blocks", "Clear call to action"],
    "budget_max": 300,
}


def _patch_limit(monkeypatch, limit: int, window: int = 300) -> None:
    from pact.api import routes

    monkeypatch.setattr(routes, "money_rule", lambda name: RateLimitRule(limit, window))


def test_a_money_route_refuses_after_the_limit(client, monkeypatch):
    _patch_limit(monkeypatch, limit=2)
    assert client.post("/api/jobs", json=BODY).status_code == 201
    assert client.post("/api/jobs", json=BODY).status_code == 201
    refused = client.post("/api/jobs", json=BODY)
    assert refused.status_code == 429
    assert "Retry-After" in refused.headers
    assert refused.headers["X-RateLimit-Limit"] == "2"
    assert refused.headers["X-RateLimit-Remaining"] == "0"


def test_the_budget_is_per_identity_not_global(client, monkeypatch):
    from pact.api.auth import issue_token

    _patch_limit(monkeypatch, limit=1)
    other = {"Authorization": f"Bearer {issue_token('buyer_two', 'buyer')}"}
    assert client.post("/api/jobs", json=BODY).status_code == 201
    assert client.post("/api/jobs", json=BODY).status_code == 429
    # A different authenticated buyer has its own budget.
    assert client.post("/api/jobs", json=BODY, headers=other).status_code == 201


def test_a_guest_cannot_reset_its_budget_by_minting_a_new_session(monkeypatch):
    """A guest identity carries the client address, so fresh sessions share a budget.

    Exercised on the enforcement helper directly with two guest principals that
    share one address: the second is refused even though its subject is new.
    """
    from fastapi import HTTPException

    from pact.api import routes
    from pact.api.auth import Principal

    _patch_limit(monkeypatch, limit=1)

    class _Request:
        headers = {"x-forwarded-for": "203.0.113.9"}
        client = None

    request = _Request()
    first = Principal(subject="gst_aaa", role="buyer", via_cookie=True)
    second = Principal(subject="gst_bbb", role="buyer", via_cookie=True)

    routes._enforce_rate_limit(request, first, "run")  # consumes the budget
    with pytest.raises(HTTPException) as excinfo:
        routes._enforce_rate_limit(request, second, "run")
    assert excinfo.value.status_code == 429


def test_health_reports_the_rate_limit_scope(client):
    """A reader can see whether the limit is shared or single-instance only."""
    body = client.get("/api/health").json()
    assert body["rate_limit"]["enabled"] is True
    assert body["rate_limit"]["scope"] in {"memory", "postgres", "unavailable"}
    # ``shared`` must agree with the scope, so the surface cannot overstate itself.
    assert body["rate_limit"]["shared"] == (body["rate_limit"]["scope"] == "postgres")


def test_the_limit_is_skipped_when_disabled(client, monkeypatch):
    from dataclasses import replace

    from pact import config
    from pact.api import routes

    _patch_limit(monkeypatch, limit=1)
    # Disable the guard on a copy of the real settings, so every other field the
    # route reads (currency, auth, ...) is still present and real.
    patched = replace(config.settings, rate_limit=replace(config.settings.rate_limit, enabled=False))
    monkeypatch.setattr(routes, "settings", patched)
    for _ in range(3):
        assert client.post("/api/jobs", json=BODY).status_code == 201

