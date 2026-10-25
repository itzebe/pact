"""PayPal Orders v2 escrow client.

Pact needs *escrow* semantics: hold a buyer's funds, release them only when the
deliverable passes verification, and return them untouched otherwise. PayPal
exposes those primitives through Orders v2 with ``intent=AUTHORIZE``:

    create (intent=AUTHORIZE)  ->  buyer approves  ->  authorize  ->  capture (release)
                                                                 \\->  void    (return)

Verified against PayPal's published OpenAPI specifications
(``paypal/paypal-rest-api-specifications``):

* ``checkout_payment_intent`` is ``['CAPTURE', 'AUTHORIZE']``, so ``AUTHORIZE``
  is a supported intent rather than a guess.
* ``authorization_status`` is
  ``['CREATED', 'CAPTURED', 'DENIED', 'PARTIALLY_CAPTURED', 'VOIDED', 'PENDING']``.
  There is no ``EXPIRED`` value: an expired hold simply stops being capturable.
* ``capture_status`` is
  ``['COMPLETED', 'DECLINED', 'PARTIALLY_REFUNDED', 'PENDING', 'REFUNDED', 'FAILED']``.
  Only ``COMPLETED`` means money moved.
* The authorize endpoint documents that "the buyer must first approve the order
  or a valid payment_source must be provided in the request", so a caller that
  skips approval cannot authorize. :func:`approval_url` surfaces the HATEOAS
  ``rel:payer-action`` link (``rel:approve`` on a legacy order) for that step.
* Voiding is documented as impossible once a payment is fully captured, which is
  why the capture and void paths are mutually exclusive.

Every call is idempotent via ``PayPal-Request-Id``, so a retried request cannot
double-charge a sandbox or live buyer. Transport failures raise
:class:`AmbiguousOutcome` rather than a plain error, because a timeout does not
prove the remote operation failed.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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


class AmbiguousOutcome(PayPalError):
    """A request failed in a way that does not prove the remote operation failed.

    A timeout, a dropped connection, or a 5xx response means the provider may or
    may not have applied the operation. Callers must reconcile by querying the
    provider rather than retrying blindly, because a retry can double-charge.
    """

    ambiguous = True


# Authorization states that still permit a capture or a void, per the Payments v2
# `authorization_status` enum (CREATED, CAPTURED, DENIED, PARTIALLY_CAPTURED,
# VOIDED, PENDING). Anything else must not be treated as held funds.
HOLDABLE_STATUSES = frozenset({"CREATED", "PENDING"})
# Capture states that mean money actually moved, per the Payments v2
# `capture_status` enum (COMPLETED, DECLINED, PARTIALLY_REFUNDED, PENDING,
# REFUNDED, FAILED).
SETTLED_CAPTURE_STATUSES = frozenset({"COMPLETED"})


def money(amount: Decimal | float | int | str) -> str:
    """Format a value as a PayPal amount string with exactly two decimals.

    Raises ``ValueError`` (not ``decimal.InvalidOperation``) for unusable input, so
    a caller guarding with ``except ValueError`` cannot be surprised by an
    unhandled decimal error.
    """
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Not a valid amount: {amount!r}") from exc
    try:
        return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        # NaN or Infinity reach here.
        raise ValueError(f"Not a finite amount: {amount!r}") from exc


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
            try:
                response = client.post(
                    f"{self.base_url}/v1/oauth2/token",
                    data={"grant_type": "client_credentials"},
                    auth=(self.client_id, self.client_secret),
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise PayPalError("Could not reach PayPal to obtain an access token", code="TRANSPORT_ERROR") from exc
        if response.status_code != 200:
            raise PayPalError(
                "Could not obtain a PayPal access token (check PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET)",
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

        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.request(method, f"{self.base_url}{path}", headers=headers, json=json_body)
        except httpx.TimeoutException as exc:
            # The request may still have been applied remotely. Never report this
            # as a definite failure, and never retry it blindly.
            raise AmbiguousOutcome(
                f"PayPal request to {path} timed out; the remote outcome is unknown",
                code="TIMEOUT",
            ) from exc
        except httpx.HTTPError as exc:
            raise AmbiguousOutcome(
                f"PayPal request to {path} failed at the transport layer; the remote outcome is unknown",
                code="TRANSPORT_ERROR",
            ) from exc

        # A 5xx can also mean the operation was applied before the failure.
        if response.status_code >= 500:
            raise AmbiguousOutcome(
                f"PayPal returned HTTP {response.status_code} for {path}; the remote outcome is unknown",
                status=response.status_code,
                code="PROVIDER_ERROR",
            )

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

        # ``payment_source`` is an order-level field. PayPal ignores it inside a
        # purchase unit, and without it a redirect flow gets the legacy
        # ``CREATED``/``rel:approve`` order that its checkout page cannot render —
        # the buyer sees "Things don't appear to be working at the moment".
        # Sending it here makes PayPal return the ``PAYER_ACTION_REQUIRED`` order
        # with the ``rel:payer-action`` link this flow hands to the buyer.
        experience_context: dict[str, Any] = {
            "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
            # Escrow must place a hold at approval time, so the buyer needs an
            # instantly authorizable method (an eCheck could be approved but not
            # authorized). Mirrors PayPal's documented AUTHORIZE example.
            "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
            "landing_page": "LOGIN",
        }
        if return_url:
            experience_context["return_url"] = return_url
        if cancel_url:
            experience_context["cancel_url"] = cancel_url

        return self._request(
            "POST",
            "/v2/checkout/orders",
            json_body={
                "intent": "AUTHORIZE",
                "purchase_units": [purchase_unit],
                "payment_source": {"paypal": {"experience_context": experience_context}},
            },
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

    def get_capture(self, capture_id: str) -> Capture:
        """Fetch a capture's real status, for reconciling an ambiguous outcome."""
        body = self._request("GET", f"/v2/payments/captures/{capture_id}")
        amount = body.get("amount") or {}
        return Capture(
            id=body.get("id", capture_id),
            status=body.get("status", ""),
            amount=str(amount.get("value", "")),
            currency=str(amount.get("currency_code", "")),
            raw=body,
        )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:2000]}


def _error_code(body: Any) -> str | None:
    """Return the most specific error code PayPal supplied.

    ``details[0].issue`` names the actual cause (``ORDER_NOT_APPROVED``,
    ``AUTHORIZATION_ALREADY_CAPTURED``); ``name`` is only the HTTP-level category
    (``UNPROCESSABLE_ENTITY``). The specific one is what a caller can act on, so it
    wins when present.
    """
    if isinstance(body, dict):
        details = body.get("details")
        if isinstance(details, list) and details and isinstance(details[0], dict):
            issue = details[0].get("issue")
            if issue:
                return str(issue)
        if body.get("name"):
            return str(body["name"])
    return None


def _describe_error(response: httpx.Response, body: Any) -> str:
    """Build a message that keeps the specific cause, not just the HTTP category.

    PayPal's top-level ``message`` is generic ("The requested action could not be
    performed."), while ``details[].description`` names the real cause. Both are
    kept so a log line is actionable.
    """
    name = _error_code(body) or f"HTTP {response.status_code}"
    parts: list[str] = []
    if isinstance(body, dict):
        if body.get("message"):
            parts.append(str(body["message"]))
        details = body.get("details")
        if isinstance(details, list):
            for d in details:
                if isinstance(d, dict) and d.get("description"):
                    parts.append(str(d["description"]))
    detail = " ".join(parts)
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


def _parse_capture(body: dict) -> Capture:
    """Read the first capture PayPal attached to an order or authorization.

    Used to recover a capture whose id was lost with a failed response: the order
    is authoritative about what was captured against it.
    """
    unit = (body.get("purchase_units") or [{}])[0]
    payments = unit.get("payments") or {}
    captures = payments.get("captures") or []
    node = captures[0] if captures else body
    amount = node.get("amount") or {}
    return Capture(
        id=node.get("id", ""),
        status=node.get("status", ""),
        amount=str(amount.get("value", "")),
        currency=str(amount.get("currency_code", "")),
        raw=body,
    )


def approval_url(order: dict) -> str | None:
    """Return the buyer-approval URL from a created order's HATEOAS links.

    PayPal requires the buyer to approve an order before it can be authorized
    (unless a valid ``payment_source`` is supplied in the request). A modern
    order carries a ``rel:payer-action`` link to that page; an order created
    without ``payment_source`` falls back to the legacy ``rel:approve``, so both
    are accepted. A caller that cannot obtain approval must not proceed to
    authorize.
    """
    for link in order.get("links") or []:
        if not isinstance(link, dict):
            continue
        rel = str(link.get("rel", ""))
        if rel in {"approve", "payer-action"} and link.get("href"):
            return str(link["href"])
    return None
