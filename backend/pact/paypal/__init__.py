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

    Three gates protect against a deployment that fakes a settlement:

    1. ``PACT_REQUIRE_REAL_PAYPAL`` makes missing credentials a hard failure.
    2. In production (any ``PACT_ENV`` other than development/test) the mock is
       refused outright, regardless of ``PACT_ALLOW_MOCK_PAYPAL``. A production
       process that cannot reach PayPal must fail, not silently move fake money.
    3. Otherwise the mock is used only when explicitly allowed, and the resulting
       client is labelled so no caller can report its output as real.
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
    if settings.is_production:
        # Never fake a settlement in production. This is not configurable: a
        # production process either has real credentials or it does not settle.
        raise PayPalError(
            "Refusing to start: no PayPal credentials, and this process is running in "
            f"production (PACT_ENV={settings.environment}). A mock settlement moves no "
            "money, so it must never be reported as a real one. Set PAYPAL_CLIENT_ID "
            "and PAYPAL_CLIENT_SECRET, or set PACT_ENV=development to allow the mock."
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
