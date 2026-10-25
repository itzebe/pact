"""PayPal integration for Pact."""

from __future__ import annotations

import logging
from typing import Protocol

from ..config import settings
from .escrow import (
    HOLDABLE_STATUSES,
    SETTLED_CAPTURE_STATUSES,
    AmbiguousOutcome,
    Authorization,
    Capture,
    PayPalError,
    PayPalEscrowClient,
    approval_url,
    money,
)
from .mock import MockPayPalEscrowClient

log = logging.getLogger("pact.paypal")

__all__ = [
    "AmbiguousOutcome",
    "Authorization",
    "Capture",
    "PayPalError",
    "PayPalEscrowClient",
    "MockPayPalEscrowClient",
    "EscrowBackend",
    "build_escrow_client",
    "approval_url",
    "money",
    "HOLDABLE_STATUSES",
    "SETTLED_CAPTURE_STATUSES",
]


class EscrowBackend(Protocol):
    """The escrow surface Pact depends on, real or simulated."""

    environment: str

    @property
    def configured(self) -> bool: ...

    def create_order(self, **kwargs) -> dict: ...
    def authorize_order(self, order_id: str, **kwargs) -> Authorization: ...
    def capture_authorization(self, authorization_id: str, **kwargs) -> Capture: ...
    def void_authorization(self, authorization_id: str, **kwargs) -> None: ...
    def refund_capture(self, capture_id: str, **kwargs) -> dict: ...
    def get_order(self, order_id: str) -> dict: ...
    def get_authorization(self, authorization_id: str) -> Authorization: ...
    def get_capture(self, capture_id: str) -> Capture: ...


def build_escrow_client() -> EscrowBackend:
    """Return a live client when credentials exist, else a clearly-marked mock.

    ``PACT_REQUIRE_REAL_PAYPAL`` makes the absence of credentials a hard startup
    failure. Without it, a production deployment could silently settle against the
    in-memory double and report a "settled" job for which no money ever moved.
    """
    if settings.paypal.configured:
        log.info("PayPal escrow client using %s credentials", settings.paypal.environment)
        return PayPalEscrowClient()
    if settings.require_real_paypal:
        raise PayPalError(
            "PACT_REQUIRE_REAL_PAYPAL is set but no PayPal credentials were found. "
            "Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET, or unset "
            "PACT_REQUIRE_REAL_PAYPAL to allow simulated settlement."
        )
    if settings.allow_mock_paypal:
        log.warning(
            "No PayPal credentials found; running against the in-memory mock. "
            "Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET for real sandbox settlement."
        )
        return MockPayPalEscrowClient()
    raise PayPalError(
        "PayPal credentials are required but PACT_ALLOW_MOCK_PAYPAL is disabled"
    )
