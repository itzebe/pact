"""Test-wide configuration.

The environment is set *before* any ``pact`` module is imported, because
``pact.config.settings`` reads the environment once at import time and the
module-level engine is constructed from it. Getting this wrong means the app
either refuses to start (production without credentials) or serves unauthenticated
(no signing secret), so it must happen first.
"""

from __future__ import annotations

import os

os.environ.setdefault("PACT_ENV", "test")
os.environ.setdefault("PACT_ALLOW_MOCK_PAYPAL", "true")
# A fixed, obviously-fake secret. Real deployments mint their own.
os.environ.setdefault("PACT_AUTH_SECRET", "test-only-signing-secret-0123456789abcdefghijklmnop")
os.environ.setdefault("PACT_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
os.environ.setdefault("PACT_CORS_ORIGINS", "http://localhost:5173")

import pytest  # noqa: E402

from pact.api.auth import issue_token  # noqa: E402

BUYER = "buyer"
OTHER_BUYER = "buyer_other"
SELLER = "seller_atlas"
ADMIN = "admin"


def bearer(subject: str, role: str) -> dict[str, str]:
    """Headers carrying a signed token for ``subject`` in ``role``."""
    return {"Authorization": f"Bearer {issue_token(subject, role)}"}


@pytest.fixture()
def buyer_headers() -> dict[str, str]:
    return bearer(BUYER, "buyer")


@pytest.fixture()
def other_buyer_headers() -> dict[str, str]:
    return bearer(OTHER_BUYER, "buyer")


@pytest.fixture()
def seller_headers() -> dict[str, str]:
    return bearer(SELLER, "seller")


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return bearer(ADMIN, "admin")
