"""In-memory stand-in for :class:`PayPalEscrowClient`.

Judges may not have sandbox credentials to hand, so Pact can run its whole
lifecycle against this double. It enforces the same state machine as PayPal —
an authorization can be captured or voided exactly once, and nothing else — so
the settlement logic under test is the real logic, not a stub that always
succeeds.

Every identifier it mints is prefixed ``MOCK-`` and every record it returns is
flagged, so a mock settlement can never be mistaken for real money movement in
the ledger or the UI.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .escrow import Authorization, Capture, PayPalError, money


class MockPayPalEscrowClient:
    """Same surface as the real client, backed by a dict.

    ``auto_approves`` is True because there is no browser in a headless demo: the
    double stands in for a buyer who has already approved the order. The real
    client does not set it, so the approval step is exercised only where it is
    real.
    """

    environment = "mock"
    auto_approves = True

    def __init__(self) -> None:
        self._orders: dict[str, dict[str, Any]] = {}
        self._authorizations: dict[str, dict[str, Any]] = {}
        self._captures: dict[str, dict[str, Any]] = {}

    @property
    def configured(self) -> bool:
        return True

    # ------------------------------------------------------------- lifecycle

    def create_order(self, *, amount, currency=None, description="", reference_id=None,
                     return_url=None, cancel_url=None, custom_id=None, idempotency_key=None) -> dict:
        order_id = f"MOCK-ORDER-{uuid.uuid4().hex[:12].upper()}"
        order = {
            "id": order_id,
            "status": "PAYER_ACTION_REQUIRED",
            "intent": "AUTHORIZE",
            "mock": True,
            "links": [
                {"rel": "self", "href": f"https://api-m.sandbox.paypal.com/v2/checkout/orders/{order_id}"},
                {"rel": "payer-action", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"},
            ],
            "purchase_units": [
                {
                    "reference_id": reference_id or "default",
                    "custom_id": custom_id,
                    "description": description,
                    "amount": {"currency_code": currency or "USD", "value": money(amount)},
                }
            ],
        }
        self._orders[order_id] = order
        return order

    def authorize_order(self, order_id: str, *, idempotency_key=None) -> Authorization:
        order = self._orders.get(order_id)
        if order is None:
            raise PayPalError(f"Unknown order {order_id}", code="RESOURCE_NOT_FOUND")
        if order["status"] == "COMPLETED":
            raise PayPalError("Order has already been authorized", code="ORDER_ALREADY_AUTHORIZED")

        unit = order["purchase_units"][0]
        authorization_id = f"MOCK-AUTH-{uuid.uuid4().hex[:12].upper()}"
        record = {
            "id": authorization_id,
            "status": "CREATED",
            "amount": unit["amount"],
            "expiration_time": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
            "order_id": order_id,
            "mock": True,
        }
        self._authorizations[authorization_id] = record
        order["status"] = "COMPLETED"
        order["authorization_id"] = authorization_id
        # PayPal attaches the authorization to the order's purchase unit, and that
        # is where it must be found when a response was lost.
        unit["payments"] = {"authorizations": [record]}

        return Authorization(
            id=authorization_id,
            status="CREATED",
            amount=record["amount"]["value"],
            currency=record["amount"]["currency_code"],
            expires_at=record["expiration_time"],
            raw=record,
        )

    def capture_authorization(self, authorization_id: str, *, amount=None, currency=None,
                              note_to_payer="", idempotency_key=None) -> Capture:
        record = self._authorizations.get(authorization_id)
        if record is None:
            raise PayPalError(f"Unknown authorization {authorization_id}", code="RESOURCE_NOT_FOUND")
        if record["status"] != "CREATED":
            raise PayPalError(
                f"Authorization is {record['status']} and can no longer be captured",
                code="AUTHORIZATION_ALREADY_CAPTURED",
            )

        captured_amount = money(amount) if amount is not None else record["amount"]["value"]
        if Decimal(captured_amount) > Decimal(record["amount"]["value"]):
            raise PayPalError("Cannot capture more than the authorized amount", code="CAPTURE_AMOUNT_EXCEEDED")

        capture_id = f"MOCK-CAPTURE-{uuid.uuid4().hex[:12].upper()}"
        capture = {
            "id": capture_id,
            "status": "COMPLETED",
            "amount": {"currency_code": currency or record["amount"]["currency_code"], "value": captured_amount},
            "authorization_id": authorization_id,
            "note_to_payer": note_to_payer,
            "mock": True,
        }
        self._captures[capture_id] = capture
        record["status"] = "CAPTURED"
        record["capture_id"] = capture_id
        order = self._orders.get(record.get("order_id", ""))
        if order is not None:
            payments = order["purchase_units"][0].setdefault("payments", {})
            payments["captures"] = [capture]

        return Capture(id=capture_id, status="COMPLETED", amount=captured_amount,
                       currency=capture["amount"]["currency_code"], raw=capture)

    def void_authorization(self, authorization_id: str, *, idempotency_key=None) -> None:
        record = self._authorizations.get(authorization_id)
        if record is None:
            raise PayPalError(f"Unknown authorization {authorization_id}", code="RESOURCE_NOT_FOUND")
        if record["status"] != "CREATED":
            raise PayPalError(
                f"Authorization is {record['status']} and can no longer be voided",
                code="AUTHORIZATION_ALREADY_CAPTURED",
            )
        record["status"] = "VOIDED"

    def refund_capture(self, capture_id: str, *, amount=None, currency=None, idempotency_key=None) -> dict:
        capture = self._captures.get(capture_id)
        if capture is None:
            raise PayPalError(f"Unknown capture {capture_id}", code="RESOURCE_NOT_FOUND")
        refund_id = f"MOCK-REFUND-{uuid.uuid4().hex[:12].upper()}"
        refund = {
            "id": refund_id,
            "status": "COMPLETED",
            "amount": {"currency_code": currency or capture["amount"]["currency_code"],
                       "value": money(amount) if amount is not None else capture["amount"]["value"]},
            "mock": True,
        }
        capture["status"] = "REFUNDED"
        return refund

    def get_order(self, order_id: str) -> dict:
        order = self._orders.get(order_id)
        if order is None:
            raise PayPalError(f"Unknown order {order_id}", code="RESOURCE_NOT_FOUND")
        return order

    def get_capture(self, capture_id: str) -> Capture:
        capture = self._captures.get(capture_id)
        if capture is None:
            raise PayPalError(f"Unknown capture {capture_id}", code="RESOURCE_NOT_FOUND")
        return Capture(
            id=capture["id"],
            status=capture["status"],
            amount=capture["amount"]["value"],
            currency=capture["amount"]["currency_code"],
            raw=capture,
        )

    def get_authorization(self, authorization_id: str) -> Authorization:
        record = self._authorizations.get(authorization_id)
        if record is None:
            raise PayPalError(f"Unknown authorization {authorization_id}", code="RESOURCE_NOT_FOUND")
        return Authorization(
            id=record["id"],
            status=record["status"],
            amount=record["amount"]["value"],
            currency=record["amount"]["currency_code"],
            expires_at=record["expiration_time"],
            raw=record,
        )
