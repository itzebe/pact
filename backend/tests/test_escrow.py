"""Escrow state machine tests.

These assert the guarantees the whole product rests on: funds are held rather
than taken, released only once, and returned untouched on failure. If any of
these break, Pact's central claim breaks with them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pact.paypal import MockPayPalEscrowClient
from pact.paypal.escrow import PayPalError, approval_url


@pytest.fixture()
def escrow() -> MockPayPalEscrowClient:
    return MockPayPalEscrowClient()


def test_create_order_reserves_funds_rather_than_charging(escrow):
    """The order must be an AUTHORIZE order, not an immediate capture."""
    order = escrow.create_order(amount=Decimal("120.00"), description="test")
    assert order["intent"] == "AUTHORIZE"
    # A real PayPal order that needs buyer approval comes back
    # PAYER_ACTION_REQUIRED with a rel:payer-action link; the double mirrors it.
    assert order["status"] == "PAYER_ACTION_REQUIRED"
    assert order["purchase_units"][0]["amount"]["value"] == "120.00"
    assert approval_url(order) == f"https://www.sandbox.paypal.com/checkoutnow?token={order['id']}"


def test_authorize_places_a_hold(escrow):
    order = escrow.create_order(amount=Decimal("120.00"))
    authorization = escrow.authorize_order(order["id"])
    assert authorization.status == "CREATED"
    assert authorization.amount == "120.00"
    assert authorization.is_holdable


def test_capture_releases_held_funds(escrow):
    order = escrow.create_order(amount=Decimal("120.00"))
    authorization = escrow.authorize_order(order["id"])
    capture = escrow.capture_authorization(authorization.id)
    assert capture.status == "COMPLETED"
    assert capture.amount == "120.00"


def test_void_returns_funds_and_moves_nothing(escrow):
    order = escrow.create_order(amount=Decimal("120.00"))
    authorization = escrow.authorize_order(order["id"])
    escrow.void_authorization(authorization.id)
    assert escrow.get_authorization(authorization.id).status == "VOIDED"


def test_authorization_cannot_be_settled_twice(escrow):
    """Double settlement is the failure mode that would cost real money."""
    order = escrow.create_order(amount=Decimal("50.00"))
    authorization = escrow.authorize_order(order["id"])
    escrow.capture_authorization(authorization.id)
    with pytest.raises(PayPalError, match="no longer be captured"):
        escrow.capture_authorization(authorization.id)


def test_voided_authorization_cannot_then_be_captured(escrow):
    order = escrow.create_order(amount=Decimal("50.00"))
    authorization = escrow.authorize_order(order["id"])
    escrow.void_authorization(authorization.id)
    with pytest.raises(PayPalError, match="no longer be captured"):
        escrow.capture_authorization(authorization.id)


def test_capture_cannot_exceed_the_authorized_amount(escrow):
    """A seller must not be able to extract more than the agreed price."""
    order = escrow.create_order(amount=Decimal("50.00"))
    authorization = escrow.authorize_order(order["id"])
    with pytest.raises(PayPalError, match="more than the authorized"):
        escrow.capture_authorization(authorization.id, amount=Decimal("500.00"))


def test_partial_capture_is_allowed(escrow):
    """Partial release is how a partially-satisfied delivery would be handled."""
    order = escrow.create_order(amount=Decimal("100.00"))
    authorization = escrow.authorize_order(order["id"])
    capture = escrow.capture_authorization(authorization.id, amount=Decimal("60.00"))
    assert capture.amount == "60.00"


def test_order_cannot_be_authorized_twice(escrow):
    order = escrow.create_order(amount=Decimal("10.00"))
    escrow.authorize_order(order["id"])
    with pytest.raises(PayPalError, match="already been authorized"):
        escrow.authorize_order(order["id"])


def test_unknown_order_is_rejected(escrow):
    with pytest.raises(PayPalError, match="Unknown order"):
        escrow.authorize_order("MOCK-ORDER-DOES-NOT-EXIST")


def test_amounts_are_formatted_to_two_decimals(escrow):
    order = escrow.create_order(amount=Decimal("99.999"))
    assert order["purchase_units"][0]["amount"]["value"] == "100.00"


def test_mock_records_are_flagged_so_they_cannot_be_mistaken_for_real(escrow):
    order = escrow.create_order(amount=Decimal("10.00"))
    assert order["mock"] is True
    assert order["id"].startswith("MOCK-")
