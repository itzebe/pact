"""PayPal integration for Pact."""

from __future__ import annotations

import logging
from typing import Protocol

from ..config import settings
from .escrow import Authorization, Capture, PayPalError, PayPalEscrowClient, money
from .mock import MockPayPalEscrowClient

log = logging.getLogger("pact.paypal")

__all__ = [
    "Authorization",
    "Capture",
    "PayPalError",
    "PayPalEscrowClient",
    "MockPayPalEscrowClient",
    "EscrowBackend",
    "build_escrow_client",
    "money",
]


class EscrowBackend(Protocol):
    """The escrow surface Pact depends on, real or simulated."""

    @property
    def configured(self) -> bool: ...

    def create_order(self, **kwargs) -> dict: ...
    def authorize_order(self, order_id: str, **kwargs) -> Authorization: ...
    def capture_authorization(self, authorization_id: str, **kwargs) -> Capture: ...
    def void_authorization(self, authorization_id: str, **kwargs) -> None: ...
    def refund_capture(self, capture_id: str, **kwargs) -> dict: ...
    def get_order(self, order_id: str) -> dict: ...


def build_escrow_client() -> EscrowBackend:
    """Return a live client when credentials exist, else a clearly-marked mock."""
    if settings.paypal.configured:
        log.info("PayPal escrow client using %s credentials", settings.paypal.environment)
        return PayPalEscrowClient()
    if settings.allow_mock_paypal:
        log.warning(
            "No PayPal credentials found; running against the in-memory mock. "
            "Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET for real sandbox settlement."
        )
        return MockPayPalEscrowClient()
    raise PayPalError(
        "PayPal credentials are required but PACT_ALLOW_MOCK_PAYPAL is disabled"
    )
