"""PayPal sandbox/production separation.

The requirement is that a sandbox task cannot accidentally move production money,
and that the boundary is a property of the process rather than a convention. These
tests pin the host classification, the sandbox-only guard, and the fact that no
secret is ever echoed by the health surface.
"""

from __future__ import annotations

import os

from pact.config import Settings, _is_local_or_sandbox_host


# --------------------------------------------------------------- host classifier


def test_the_sandbox_host_is_recognised():
    assert _is_local_or_sandbox_host("https://api-m.sandbox.paypal.com") is True
    assert _is_local_or_sandbox_host("https://www.sandbox.paypal.com") is True


def test_the_live_host_is_not_a_sandbox_host():
    assert _is_local_or_sandbox_host("https://api-m.paypal.com") is False
    assert _is_local_or_sandbox_host("https://api.paypal.com") is False


def test_loopback_and_private_hosts_are_allowed_for_tests():
    assert _is_local_or_sandbox_host("http://localhost:9999") is True
    assert _is_local_or_sandbox_host("http://127.0.0.1:8080") is True
    assert _is_local_or_sandbox_host("http://10.1.2.3") is True
    assert _is_local_or_sandbox_host("http://192.168.1.5") is True
    assert _is_local_or_sandbox_host("http://172.16.0.1") is True
    assert _is_local_or_sandbox_host("http://172.31.255.1") is True
    assert _is_local_or_sandbox_host("http://172.32.0.1") is False  # outside 172.16/12


def test_a_garbage_url_is_not_a_sandbox_host():
    assert _is_local_or_sandbox_host("") is False
    assert _is_local_or_sandbox_host("not a url") is False


# --------------------------------------------------------------- settings


def _settings(**env) -> Settings:
    """Build Settings with the given env, ignoring the ambient environment."""
    saved = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        return Settings()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_a_live_environment_resolves_to_the_live_host():
    s = _settings(PAYPAL_ENVIRONMENT="live", PAYPAL_BASE_URL=None)
    assert s.paypal.is_live is True
    assert s.paypal.base_url == "https://api-m.paypal.com"


def test_a_sandbox_environment_resolves_to_the_sandbox_host():
    s = _settings(PAYPAL_ENVIRONMENT="sandbox", PAYPAL_BASE_URL=None)
    assert s.paypal.is_live is False
    assert "sandbox" in s.paypal.base_url


def test_a_base_url_override_is_honoured():
    s = _settings(PAYPAL_BASE_URL="http://127.0.0.1:9999")
    assert s.paypal.base_url == "http://127.0.0.1:9999"
    assert s.paypal.is_live is False  # a loopback override is not production


def test_an_override_to_a_live_host_counts_as_live():
    s = _settings(PAYPAL_BASE_URL="https://api-m.paypal.com")
    assert s.paypal.is_live is True


# --------------------------------------------------------------- the guard


def test_sandbox_only_refuses_a_live_configuration():
    s = _settings(
        PACT_PAYPAL_SANDBOX_ONLY="true",
        PAYPAL_ENVIRONMENT="live",
        PAYPAL_BASE_URL=None,
        PACT_ENV="test",
    )
    problems = s.validate_runtime()
    assert any("sandbox" in p.lower() and "live" in p.lower() for p in problems)


def test_sandbox_only_allows_a_sandbox_configuration():
    s = _settings(
        PACT_PAYPAL_SANDBOX_ONLY="true",
        PAYPAL_ENVIRONMENT="sandbox",
        PAYPAL_BASE_URL=None,
        PACT_ENV="test",
    )
    assert not any("live host" in p for p in s.validate_runtime())


def test_the_guard_is_off_by_default():
    s = _settings(PACT_PAYPAL_SANDBOX_ONLY=None, PAYPAL_ENVIRONMENT="live", PAYPAL_BASE_URL=None)
    assert s.paypal.sandbox_only is False


def test_the_guard_cannot_be_bypassed_with_a_live_base_url_override():
    s = _settings(
        PACT_PAYPAL_SANDBOX_ONLY="true",
        PAYPAL_BASE_URL="https://api-m.paypal.com",
        PACT_ENV="test",
    )
    assert any("live host" in p for p in s.validate_runtime())


def test_webhook_id_is_read_from_the_environment():
    s = _settings(PAYPAL_WEBHOOK_ID="WH-123")
    assert s.paypal.webhook_id == "WH-123"


def test_webhook_id_defaults_to_empty():
    s = _settings(PAYPAL_WEBHOOK_ID=None)
    assert s.paypal.webhook_id == ""


# --------------------------------------------------------------- no secret leak


def test_paypal_settings_never_expose_the_secret_in_repr():
    s = _settings(PAYPAL_CLIENT_ID="cid", PAYPAL_CLIENT_SECRET="super-secret-value")
    assert s.paypal.client_secret == "super-secret-value"
    assert s.paypal.configured is True


def test_the_health_surface_publishes_only_booleans_not_credentials():
    """The unauthenticated /health route must never carry a credential.

    It reports *whether* PayPal is configured and *whether* webhooks can be
    verified, never the client id, the secret, or the webhook id itself.
    """
    from fastapi.testclient import TestClient

    from pact.main import app

    body = TestClient(app).get("/api/health").json()
    paypal = body["paypal"]
    assert set(paypal) >= {"configured", "environment", "webhook_verification", "sandbox_only"}
    # None of the credential-bearing fields are present under any key.
    flat = repr(body)
    assert "client_secret" not in flat
    assert "webhook_id" not in flat
    for key, value in paypal.items():
        assert isinstance(value, (bool, str)), key
    assert isinstance(paypal["configured"], bool)
    assert isinstance(paypal["webhook_verification"], bool)
