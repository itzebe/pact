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
        "id": "ORDER-2", "status": "PAYER_ACTION_REQUIRED",
        "links": [{"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-2"}],
    })
    order = client.create_order(amount=Decimal("10.00"))
    assert approval_url(order) == "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-2"


def test_create_order_sends_payment_source_at_order_level(client, transport):
    """The buyer-approval link only exists if ``payment_source`` is sent.

    PayPal ignores ``payment_source`` nested inside a purchase unit and falls
    back to the legacy ``CREATED``/``rel:approve`` order, whose checkout page
    cannot render the redirect flow. Without this field at the order level the
    buyer gets "Things don't appear to be working at the moment".
    """
    transport.set("POST", "/v2/checkout/orders", 201, {"id": "ORDER-3", "status": "PAYER_ACTION_REQUIRED"})
    client.create_order(
        amount=Decimal("300.00"),
        currency="USD",
        return_url="https://pact.example/api/paypal/return",
        cancel_url="https://pact.example/api/paypal/cancel",
    )

    payload = json.loads(next(r for r in transport.requests if r.url.path == "/v2/checkout/orders").content)
    assert payload["payment_source"]["paypal"]["experience_context"]["return_url"] == (
        "https://pact.example/api/paypal/return"
    )
    assert payload["payment_source"]["paypal"]["experience_context"]["cancel_url"] == (
        "https://pact.example/api/paypal/cancel"
    )
    assert "payment_source" not in payload["purchase_units"][0], (
        "payment_source is an order-level field; PayPal ignores it inside a purchase unit"
    )


def test_create_order_omits_placeholder_return_urls(client, transport):
    """A caller that supplies no URLs must not get fabricated example.com ones."""
    transport.set("POST", "/v2/checkout/orders", 201, {"id": "ORDER-4", "status": "PAYER_ACTION_REQUIRED"})
    client.create_order(amount=Decimal("5.00"))

    payload = json.loads(next(r for r in transport.requests if r.url.path == "/v2/checkout/orders").content)
    context = payload["payment_source"]["paypal"]["experience_context"]
    assert "return_url" not in context
    assert "cancel_url" not in context


def test_create_order_asks_for_an_immediately_authorizable_method(client, transport):
    """Escrow holds funds at approval time, so the buyer must fund instantly.

    An eCheck could be approved but not authorized, which would strand the job
    after the buyer believes they have paid.
    """
    transport.set("POST", "/v2/checkout/orders", 201, {"id": "ORDER-5", "status": "PAYER_ACTION_REQUIRED"})
    client.create_order(amount=Decimal("42.00"))

    payload = json.loads(next(r for r in transport.requests if r.url.path == "/v2/checkout/orders").content)
    context = payload["payment_source"]["paypal"]["experience_context"]
    assert context["payment_method_preference"] == "IMMEDIATE_PAYMENT_REQUIRED"
    assert context["user_action"] == "PAY_NOW"
    assert context["shipping_preference"] == "NO_SHIPPING"


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


# --------------------------------------------------------------- payment parsing
#
# An order and a Payments v2 authorization/capture are different resources that
# both carry `id` and `status`. `_parse_authorization`/`_parse_capture` are called
# with *both* shapes: `get_authorization`/`get_capture` return the payment, while
# the order-reconciliation path passes an order. Treating an order as a payment
# invents one, so these pin the discrimination.

UNPAID_ORDER = {
    "id": "ORDER-UNPAID-1",
    "status": "CREATED",  # legacy order shape: no payment_source, no approval link
    "intent": "AUTHORIZE",
    "purchase_units": [{"amount": {"currency_code": "USD", "value": "2.50"}}],
    "links": [{"rel": "self"}, {"rel": "approve"}],
}


def test_an_order_with_no_authorization_yields_no_authorization():
    """The order's own id must never be reported as an authorization id.

    This is the bug the order-reconciliation path tripped on: an order that holds
    no authorization returned the order's id and status as if they were an
    authorization's, so the "no authorization exists" guard was skipped and a
    hold was recorded that PayPal never placed.
    """
    from pact.paypal.escrow import _parse_authorization

    parsed = _parse_authorization(UNPAID_ORDER)
    assert parsed.id == "", "an order is not an authorization; it must not supply an id"
    assert parsed.status == "", "the order status (CREATED) is not an authorization status"


def test_an_order_with_no_capture_yields_no_capture():
    from pact.paypal.escrow import _parse_capture

    parsed = _parse_capture(UNPAID_ORDER)
    assert parsed.id == "", "an order is not a capture; it must not supply an id"
    assert parsed.status == ""


def test_an_order_that_holds_an_authorization_still_parses():
    """The genuine ambiguous-authorize case must keep working."""
    from pact.paypal.escrow import _parse_authorization

    order = {
        "id": "ORDER-PAID-1",
        "status": "COMPLETED",
        "purchase_units": [
            {
                "amount": {"currency_code": "USD", "value": "120.00"},
                "payments": {
                    "authorizations": [
                        {"id": "AUTH-9", "status": "CREATED",
                         "amount": {"currency_code": "USD", "value": "120.00"}}
                    ]
                },
            }
        ],
    }
    parsed = _parse_authorization(order)
    assert parsed.id == "AUTH-9"
    assert parsed.status == "CREATED"
    assert parsed.amount == "120.00"


def test_a_bare_authorization_body_is_still_parsed():
    """`get_authorization` returns the resource itself, not an order."""
    from pact.paypal.escrow import _parse_authorization

    parsed = _parse_authorization(
        {"id": "AUTH-DIRECT", "status": "CAPTURED", "amount": {"currency_code": "USD", "value": "7.00"}}
    )
    assert parsed.id == "AUTH-DIRECT"
    assert parsed.status == "CAPTURED"


def test_a_bare_capture_body_is_still_parsed():
    from pact.paypal.escrow import _parse_capture

    parsed = _parse_capture(
        {"id": "CAP-DIRECT", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "7.00"}}
    )
    assert parsed.id == "CAP-DIRECT"
    assert parsed.status == "COMPLETED"


def test_malformed_payment_containers_degrade_to_absent():
    """Wire data can be the wrong shape; parsing must not raise."""
    from pact.paypal.escrow import _parse_authorization, _parse_capture

    for bad in [
        {"purchase_units": None},
        {"purchase_units": []},
        {"purchase_units": [None]},
        {"purchase_units": [{"payments": None}]},
        {"purchase_units": [{"payments": {"authorizations": None}}]},
        {"purchase_units": [{"payments": {"authorizations": []}}]},
        {"purchase_units": [{"payments": {"authorizations": [None]}}]},
        {"purchase_units": ["nonsense"]},
    ]:
        assert _parse_authorization(bad).id == ""
        assert _parse_capture(bad).id == ""
    assert _parse_authorization({}).id == ""
    assert _parse_capture({}).id == ""


def test_the_parse_fix_is_load_bearing():
    """Negative control: with the old fallback, an order masquerades as a payment.

    Reproduces the pre-fix behaviour directly, so the assertions above cannot pass
    for an unrelated reason (for example the parser returning an empty object for
    every input).
    """
    from pact.paypal.escrow import _parse_authorization

    def old_parse_authorization(body: dict):
        unit = (body.get("purchase_units") or [{}])[0]
        payments = unit.get("payments") or {}
        authorizations = payments.get("authorizations") or []
        node = authorizations[0] if authorizations else body
        return node

    masqueraded = old_parse_authorization(UNPAID_ORDER)
    assert masqueraded["id"] == "ORDER-UNPAID-1", (
        "the old fallback returned the order id as an authorization id"
    )
    assert masqueraded["status"] == "CREATED", (
        "and CREATED is a HOLDABLE status, so the caller recorded a hold that never existed"
    )
    assert _parse_authorization(UNPAID_ORDER).id == "", "the fix removes that masquerade"



def test_money_formats_and_rejects_junk():
    assert money(Decimal("1")) == "1.00"
    assert money("2.5") == "2.50"
    assert money(3.005) == "3.01"
    with pytest.raises(ValueError):
        money("not-a-number")
