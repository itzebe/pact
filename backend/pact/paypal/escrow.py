"""PayPal Orders v2 escrow client.

Pact needs *escrow* semantics: hold a buyer's funds, release them only when the
deliverable passes verification, and return them untouched otherwise. PayPal
exposes exactly those primitives through Orders v2 with ``intent=AUTHORIZE``:

    create (intent=AUTHORIZE)  ->  authorize  ->  capture   (release)
                                              \\->  void      (return)

The published PayPal Agent Toolkit cannot express this: it hard-codes
``"intent": "CAPTURE"`` in its order payload builder, so it can only do
immediate payment. Settlement that is conditional on a verified outcome has to
be built on the raw REST API, which is what this module does.

Every call is idempotent via ``PayPal-Request-Id`` so a retried request cannot
double-charge a sandbox or live buyer.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("pact.paypal")


class PayPalError(RuntimeError):
    """A PayPal API call returned a non-success response."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.code = code


def money(amount: Decimal | float | int | str) -> str:
    """Format a value as a PayPal amount string with exactly two decimals."""
    return str(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass
class Authorization:
    """A held authorization on the buyer's funding instrument."""

    id: str
    status: str
    amount: str
    currency: str
    expires_at: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def is_holdable(self) -> bool:
        return self.status.upper() in {"CREATED", "PENDING"}


@dataclass
class Capture:
    id: str
    status: str
    amount: str
    currency: str
    raw: dict[str, Any] | None = None


class PayPalEscrowClient:
    """Minimal, purpose-built client for PayPal's escrow lifecycle."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        *,
        environment: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.client_id = client_id if client_id is not None else settings.paypal.client_id
        self.client_secret = client_secret if client_secret is not None else settings.paypal.client_secret
        self.environment = (environment or settings.paypal.environment).lower()
        self.base_url = "https://api-m.paypal.com" if self.environment == "live" else "https://api-m.sandbox.paypal.com"
        self._timeout = timeout
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # ------------------------------------------------------------------ auth

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def access_token(self) -> str:
        """Fetch (and cache) an OAuth2 client-credentials token."""
        if not self.configured:
            raise PayPalError("PayPal credentials are not configured (PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET)")
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(self.client_id, self.client_secret),
                headers={"Accept": "application/json"},
            )
        if response.status_code != 200:
            raise PayPalError(
                "Could not obtain a PayPal access token",
                status=response.status_code,
                body=_safe_json(response),
            )
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 3200))
        return self._token

    def _request(self, method: str, path: str, *, json_body: dict | None = None, idempotency_key: str | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["PayPal-Request-Id"] = idempotency_key

        with httpx.Client(timeout=self._timeout) as client:
            response = client.request(method, f"{self.base_url}{path}", headers=headers, json=json_body)

        body = _safe_json(response)
        if response.status_code >= 400:
            raise PayPalError(
                _describe_error(response, body),
                status=response.status_code,
                body=body,
                code=_error_code(body),
            )
        return body or {}

    # --------------------------------------------------------------- escrow

    def create_order(
        self,
        *,
        amount: Decimal | float | str,
        currency: str | None = None,
        description: str = "",
        reference_id: str | None = None,
        return_url: str | None = None,
        cancel_url: str | None = None,
        custom_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Create an order that *reserves* rather than charges: intent=AUTHORIZE."""
        currency = currency or settings.currency
        purchase_unit: dict[str, Any] = {
            "amount": {"currency_code": currency, "value": money(amount)},
            "description": description[:127],
        }
        if reference_id:
            purchase_unit["reference_id"] = reference_id
        if custom_id:
            purchase_unit["custom_id"] = custom_id
        if return_url or cancel_url:
            purchase_unit["payment_source"] = {
                "paypal": {
                    "experience_context": {
                        "return_url": return_url or "https://example.com/return",
                        "cancel_url": cancel_url or "https://example.com/cancel",
                        "user_action": "PAY_NOW",
                        "shipping_preference": "NO_SHIPPING",
                    }
                }
            }

        return self._request(
            "POST",
            "/v2/checkout/orders",
            json_body={"intent": "AUTHORIZE", "purchase_units": [purchase_unit]},
            idempotency_key=idempotency_key or f"pact-order-{uuid.uuid4()}",
        )

    def authorize_order(self, order_id: str, *, idempotency_key: str | None = None) -> Authorization:
        """Place a hold on the buyer's funds for an approved order."""
        body = self._request(
            "POST",
            f"/v2/checkout/orders/{order_id}/authorize",
            json_body={},
            idempotency_key=idempotency_key or f"pact-auth-{order_id}",
        )
        return _parse_authorization(body)

    def capture_authorization(
        self,
        authorization_id: str,
        *,
        amount: Decimal | float | str | None = None,
        currency: str | None = None,
        note_to_payer: str = "",
        idempotency_key: str | None = None,
    ) -> Capture:
        """Release held funds to the seller. Optionally capture less than the hold."""
        payload: dict[str, Any] = {}
        if amount is not None:
            payload["amount"] = {"currency_code": currency or settings.currency, "value": money(amount)}
        if note_to_payer:
            payload["note_to_payer"] = note_to_payer[:255]

        body = self._request(
            "POST",
            f"/v2/payments/authorizations/{authorization_id}/capture",
            json_body=payload or None,
            idempotency_key=idempotency_key or f"pact-capture-{authorization_id}",
        )
        return Capture(
            id=body.get("id", ""),
            status=body.get("status", ""),
            amount=str(body.get("amount", {}).get("value", "")),
            currency=str(body.get("amount", {}).get("currency_code", "")),
            raw=body,
        )

    def void_authorization(self, authorization_id: str, *, idempotency_key: str | None = None) -> None:
        """Release the hold without moving money. This is the refund path."""
        self._request(
            "POST",
            f"/v2/payments/authorizations/{authorization_id}/void",
            json_body=None,
            idempotency_key=idempotency_key or f"pact-void-{authorization_id}",
        )

    def refund_capture(
        self,
        capture_id: str,
        *,
        amount: Decimal | float | str | None = None,
        currency: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Reverse an already-captured payment (used for post-settlement disputes)."""
        payload: dict[str, Any] = {}
        if amount is not None:
            payload["amount"] = {"currency_code": currency or settings.currency, "value": money(amount)}
        return self._request(
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            json_body=payload or None,
            idempotency_key=idempotency_key or f"pact-refund-{capture_id}",
        )

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def get_authorization(self, authorization_id: str) -> Authorization:
        return _parse_authorization(self._request("GET", f"/v2/payments/authorizations/{authorization_id}"))


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:2000]}


def _error_code(body: Any) -> str | None:
    if isinstance(body, dict):
        if body.get("name"):
            return str(body["name"])
        details = body.get("details")
        if isinstance(details, list) and details and isinstance(details[0], dict):
            return details[0].get("issue")
    return None


def _describe_error(response: httpx.Response, body: Any) -> str:
    name = _error_code(body) or f"HTTP {response.status_code}"
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("message", ""))
        details = body.get("details")
        if isinstance(details, list) and details:
            issues = [d.get("issue", "") for d in details if isinstance(d, dict)]
            description = [d.get("description", "") for d in details if isinstance(d, dict)]
            detail = detail or "; ".join(filter(None, issues + description))
    return f"PayPal error {name}: {detail}".strip()


def _parse_authorization(body: dict) -> Authorization:
    unit = (body.get("purchase_units") or [{}])[0]
    payments = unit.get("payments") or {}
    authorizations = payments.get("authorizations") or []
    if authorizations:
        node = authorizations[0]
    else:
        node = body
    amount = node.get("amount") or {}
    return Authorization(
        id=node.get("id", ""),
        status=node.get("status", ""),
        amount=str(amount.get("value", "")),
        currency=str(amount.get("currency_code", "")),
        expires_at=node.get("expiration_time"),
        raw=body,
    )
