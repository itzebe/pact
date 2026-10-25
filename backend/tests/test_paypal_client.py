"""Tests for the real PayPal client's HTTP layer.

These exercise ``PayPalEscrowClient`` itself — request construction, response
parsing, and error classification — with only the network boundary stubbed via
``httpx.MockTransport``. That keeps the code under test real while removing the
need for live sandbox credentials.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from pact.paypal import escrow as escrow_module
from pact.paypal.escrow import (
    AmbiguousOutcome,
    PayPalError,
    PayPalEscrowClient,
    approval_url,
    money,
)


class Transport:
    """A scriptable httpx transport that records the requests it receives."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: dict[str, tuple[int, dict]] = {}
        self.raise_on: dict[str, Exception] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        if key in self.raise_on:
            raise self.raise_on[key]
        status, body = self.responses.get(key, (200, {}))
        return httpx.Response(status, json=body, request=request)

    def set(self, method: str, path: str, status: int, body: dict) -> None:
        self.responses[f"{method} {path}"] = (status, body)


@pytest.fixture()
def transport(monkeypatch) -> Transport:
    t = Transport()

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(t.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(escrow_module.httpx, "Client", fake_client)
    return t


@pytest.fixture()
def client(transport) -> PayPalEscrowClient:
    transport.set("POST", "/v1/oauth2/token", 200, {"access_token": "TOKEN-123", "expires_in": 3200})
    return PayPalEscrowClient(
        client_id="cid", client_secret="secret", environment="sandbox", timeout=5
    )


def test_create_order_requests_authorize_intent(client, transport):
    transport.set("POST", "/v2/checkout/orders", 201, {
        "id": "ORDER-1", "status": "CREATED", "intent": "AUTHORIZE",
        "links": [
            {"rel": "self", "href": "https://api-m.sandbox.paypal.com/v2/checkout/orders/ORDER-1"},
            {"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-1"},
        ],
    })
    order = client.create_order(amount=Decimal("120.00"), currency="USD", description="Copy", reference_id="job_1")

    assert order["id"] == "ORDER-1"
    sent = next(r for r in transport.requests if r.url.path == "/v2/checkout/orders")
    payload = json.loads(sent.content)
    assert payload["intent"] == "AUTHORIZE", "escrow requires AUTHORIZE, never CAPTURE"
    assert payload["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "120.00"}
    assert sent.headers["PayPal-Request-Id"], "every call must be idempotent"


def test_approval_url_is_returned_for_a_real_order(client, transport):
    transport.set("POST", "/v2/checkout/orders", 201, {
        "id": "ORDER-2", "status": "CREATED",
        "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-2"}],
    })
    order = client.create_order(amount=Decimal("10.00"))
    assert approval_url(order) == "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-2"


def test_authorize_parses_the_nested_authorization(client, transport):
    transport.set("POST", "/v2/checkout/orders/ORDER-1/authorize", 201, {
        "id": "ORDER-1",
        "status": "COMPLETED",
        "purchase_units": [{
            "payments": {"authorizations": [{
                "id": "AUTH-1", "status": "CREATED",
                "amount": {"currency_code": "USD", "value": "120.00"},
                "expiration_time": "2026-11-20T00:00:00Z",
            }]}
        }],
    })
    authorization = client.authorize_order("ORDER-1")
    assert authorization.id == "AUTH-1"
    assert authorization.status == "CREATED"
    assert authorization.amount == "120.00"
    assert authorization.is_holdable


def test_capture_parses_the_capture_status(client, transport):
    transport.set("POST", "/v2/payments/authorizations/AUTH-1/capture", 201, {
        "id": "CAP-1", "status": "COMPLETED",
        "amount": {"currency_code": "USD", "value": "120.00"},
    })
    capture = client.capture_authorization("AUTH-1")
    assert capture.id == "CAP-1"
    assert capture.status == "COMPLETED"


def test_void_treats_204_as_success(client, transport):
    transport.set("POST", "/v2/payments/authorizations/AUTH-1/void", 204, {})
    client.void_authorization("AUTH-1")  # must not raise


def test_get_authorization_supports_void_confirmation(client, transport):
    transport.set("GET", "/v2/payments/authorizations/AUTH-1", 200, {
        "id": "AUTH-1", "status": "VOIDED",
        "amount": {"currency_code": "USD", "value": "120.00"},
    })
    assert client.get_authorization("AUTH-1").status == "VOIDED"


def test_get_capture_supports_reconciliation(client, transport):
    transport.set("GET", "/v2/payments/captures/CAP-1", 200, {
        "id": "CAP-1", "status": "PENDING",
        "amount": {"currency_code": "USD", "value": "120.00"},
    })
    assert client.get_capture("CAP-1").status == "PENDING"


def test_a_4xx_becomes_a_plain_error_with_the_provider_code(client, transport):
    transport.set("POST", "/v2/checkout/orders/ORDER-1/authorize", 422, {
        "name": "UNPROCESSABLE_ENTITY",
        "message": "The requested action could not be performed.",
        "details": [{"issue": "ORDER_NOT_APPROVED", "description": "Payer has not yet approved the Order."}],
    })
    with pytest.raises(PayPalError) as info:
        client.authorize_order("ORDER-1")
    assert not getattr(info.value, "ambiguous", False)
    assert info.value.status == 422
    assert info.value.code == "ORDER_NOT_APPROVED"
    assert "not yet approved" in str(info.value)


def test_a_timeout_is_ambiguous_not_a_definite_failure(client, transport):
    transport.raise_on["POST /v2/payments/authorizations/AUTH-1/capture"] = httpx.TimeoutException("timed out")
    with pytest.raises(AmbiguousOutcome) as info:
        client.capture_authorization("AUTH-1")
    assert info.value.ambiguous is True


def test_a_connection_error_is_ambiguous(client, transport):
    transport.raise_on["POST /v2/checkout/orders"] = httpx.ConnectError("connection refused")
    with pytest.raises(AmbiguousOutcome):
        client.create_order(amount=Decimal("5.00"))


def test_a_5xx_is_ambiguous_because_the_operation_may_have_applied(client, transport):
    transport.set("POST", "/v2/payments/authorizations/AUTH-1/capture", 503, {"name": "INTERNAL_SERVER_ERROR"})
    with pytest.raises(AmbiguousOutcome) as info:
        client.capture_authorization("AUTH-1")
    assert info.value.status == 503


def test_a_bad_token_request_is_a_plain_error(transport):
    transport.set("POST", "/v1/oauth2/token", 401, {"error": "invalid_client"})
    c = PayPalEscrowClient(client_id="bad", client_secret="bad", environment="sandbox")
    with pytest.raises(PayPalError, match="access token"):
        c.access_token()


def test_the_access_token_is_reused_across_calls(client, transport):
    transport.set("POST", "/v2/checkout/orders", 201, {"id": "O", "status": "CREATED"})
    client.create_order(amount=Decimal("1.00"))
    client.create_order(amount=Decimal("1.00"))
    token_calls = [r for r in transport.requests if r.url.path == "/v1/oauth2/token"]
    assert len(token_calls) == 1, "the token must be cached, not fetched per call"


def test_base_url_follows_the_environment():
    assert "sandbox" in PayPalEscrowClient(client_id="a", client_secret="b", environment="sandbox").base_url
    assert "sandbox" not in PayPalEscrowClient(client_id="a", client_secret="b", environment="live").base_url


def test_money_formats_and_rejects_junk():
    assert money(Decimal("1")) == "1.00"
    assert money("2.5") == "2.50"
    assert money(3.005) == "3.01"
    with pytest.raises(ValueError):
        money("not-a-number")
