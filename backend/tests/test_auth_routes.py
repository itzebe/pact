"""HTTP surface for customer authentication, and the sensitive-operation gate.

Covers the route placement guarantees (a protected path 401s without a token),
the cookie-based session flow, enumeration resistance, and — most importantly —
that the ``require_verified`` gate refuses a money route for an unverified,
suspended, or revoked customer *without* widening access for anyone else.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from pact.api.auth import issue_token
from pact.core.accounts import AuthError
from pact.main import app

DATABASE_URL = os.getenv("DATABASE_URL", "")

PUBLIC = {
    "/api/auth/register",
    "/api/auth/verify-email",
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/logout",
    "/api/auth/password/forgot",
    "/api/auth/password/reset",
}


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def store():
    from pact.core.auth_store import auth_store

    if DATABASE_URL:
        import psycopg

        with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "TRUNCATE users, email_verifications, password_reset_tokens, refresh_tokens, "
                "login_attempts, email_outbox, auth_events"
            )
    return auth_store


def _email() -> str:
    return f"route_{uuid.uuid4().hex[:10]}@example.com"


def _raw_verification(email: str) -> str:
    from pact.core.auth_store import auth_store

    user = auth_store.get_user_by_email(email)
    if DATABASE_URL:
        import psycopg

        with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT token_raw FROM email_outbox WHERE user_id = %s AND kind = 'verify_email' "
                "ORDER BY created_at DESC LIMIT 1",
                (user.id,),
            )
            return cur.fetchone()[0]
    return next(m.token_raw for m in auth_store._outbox.values()
                if m.user_id == user.id and m.kind == "verify_email")


def _register_verified(client, monkeypatch, password="correct-horse-battery"):
    monkeypatch.setenv("PACT_SELF_SERVICE_AUTH", "true")
    # settings is frozen and read at import; patch the live object the routes use.
    import dataclasses

    from pact.config import settings

    monkeypatch.setattr(
        "pact.api.auth_routes.settings",
        dataclasses.replace(settings, auth=dataclasses.replace(settings.auth, self_service_enabled=True)),
    )
    email = _email()
    assert client.post("/api/auth/register", json={"email": email, "password": password, "role": "buyer"}).status_code == 202
    token = _raw_verification(email)
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    return email, password


def test_registration_is_disabled_by_default(client, monkeypatch):
    """With self-service off the surface is inert, and reports 404 not 403."""
    import dataclasses

    from pact.config import settings

    monkeypatch.setattr(
        "pact.api.auth_routes.settings",
        dataclasses.replace(settings, auth=dataclasses.replace(settings.auth, self_service_enabled=False)),
    )
    response = client.post(
        "/api/auth/register",
        json={"email": _email(), "password": "correct-horse-battery", "role": "buyer"},
    )
    assert response.status_code == 404


def test_protected_auth_routes_require_a_token(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/password/change", json={"current_password": "x", "new_password": "y" * 12}).status_code == 401


def test_admin_role_cannot_be_registered_over_http(client, monkeypatch):
    monkeypatch.setenv("PACT_SELF_SERVICE_AUTH", "true")
    import dataclasses

    from pact.config import settings

    monkeypatch.setattr(
        "pact.api.auth_routes.settings",
        dataclasses.replace(settings, auth=dataclasses.replace(settings.auth, self_service_enabled=True)),
    )
    response = client.post(
        "/api/auth/register",
        json={"email": _email(), "password": "correct-horse-battery", "role": "admin"},
    )
    # The request schema only allows buyer|seller, so admin is a 422.
    assert response.status_code == 422


def test_full_session_flow_over_http(client, monkeypatch):
    email, password = _register_verified(client, monkeypatch)
    login = client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    body = login.json()
    assert "access_token" in body
    # The refresh token is a cookie, never in the JSON body.
    assert "refresh_token" not in body
    raw_refresh = client.cookies.get("pact_refresh")
    assert raw_refresh

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["type"] == "customer"
    assert me.json()["email"] == email

    # Refresh rotates the cookie. The cookie is Secure (production default), so a
    # plain-http test client will not echo it automatically; send it explicitly.
    refreshed = client.post("/api/auth/refresh", cookies={"pact_refresh": raw_refresh})
    assert refreshed.status_code == 200
    assert "refresh_token" not in refreshed.json()

    # Replaying the now-spent cookie is treated as reuse.
    replay = client.post("/api/auth/refresh", cookies={"pact_refresh": raw_refresh})
    assert replay.status_code == 401

    # The refresh cookie carries the hardened attributes.
    set_cookie = refreshed.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie and "Secure" in set_cookie and "SameSite=strict" in set_cookie


def test_logout_clears_the_refresh_cookie(client, monkeypatch):
    email, password = _register_verified(client, monkeypatch)
    client.post("/api/auth/login", json={"email": email, "password": password})
    response = client.post("/api/auth/logout")
    assert response.status_code == 204
    assert "pact_refresh=" in response.headers.get("set-cookie", "")


def test_login_never_echoes_whether_the_address_exists(client, monkeypatch):
    _register_verified(client, monkeypatch)
    unknown = client.post("/api/auth/login", json={"email": _email(), "password": "correct-horse-battery"})
    assert unknown.status_code == 401
    assert unknown.json()["detail"] == "Invalid email or password."


def test_the_sensitive_gate_blocks_an_unverified_customer(client, monkeypatch, store):
    """A verified *login* is required for a money route; an unverified token is refused."""
    import dataclasses
    import secrets

    import jwt

    from pact.config import settings

    # Craft a customer token for a real but unverified account.
    from pact.core.auth_store import UserRecord

    user = UserRecord(id="usr_" + secrets.token_hex(6), email=_email(), role="buyer")
    store.create_user(user)
    token = jwt.encode(
        {"sub": user.id, "role": "buyer", "ver": 0, "exp": 9999999999,
         "iss": "pact", "aud": "pact-api"},
        settings.auth.secret, algorithm="HS256",
    )
    response = client.post(
        "/api/jobs/whatever/run", headers={"Authorization": f"Bearer {token}"}
    )
    # 403 from the gate (unverified), not 404 (which would mean it passed the gate).
    assert response.status_code == 403
    assert "verify" in response.json()["detail"].lower()


def test_the_sensitive_gate_blocks_a_suspended_account(client, monkeypatch, store):
    import secrets

    import jwt

    from pact.config import settings
    from pact.core.auth_store import UserRecord

    user = UserRecord(
        id="usr_" + secrets.token_hex(6), email=_email(), role="buyer",
        status="suspended", email_verified_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
    )
    store.create_user(user)
    token = jwt.encode(
        {"sub": user.id, "role": "buyer", "ver": 0, "exp": 9999999999,
         "iss": "pact", "aud": "pact-api"},
        settings.auth.secret, algorithm="HS256",
    )
    response = client.post("/api/jobs/whatever/run", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_the_sensitive_gate_blocks_a_revoked_token_version(client, monkeypatch, store):
    import secrets
    from datetime import datetime, timezone

    import jwt

    from pact.config import settings
    from pact.core.auth_store import UserRecord

    user = UserRecord(
        id="usr_" + secrets.token_hex(6), email=_email(), role="buyer",
        token_version=5, email_verified_at=datetime.now(timezone.utc),
    )
    store.create_user(user)
    # The token claims version 4; the store says 5, so it has been revoked.
    token = jwt.encode(
        {"sub": user.id, "role": "buyer", "ver": 4, "exp": 9999999999,
         "iss": "pact", "aud": "pact-api"},
        settings.auth.secret, algorithm="HS256",
    )
    response = client.post("/api/jobs/whatever/run", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_the_gate_fails_closed_when_the_store_is_unavailable(client, monkeypatch):
    import dataclasses
    import secrets

    import jwt

    from pact.api import auth as auth_module
    from pact.config import settings
    from pact.core.auth_store import AccountStateError

    class _Down:
        def account_state(self, user_id):
            raise AccountStateError("down")

    monkeypatch.setattr(auth_module, "auth_store", _Down(), raising=False)
    # auth.py imports auth_store lazily inside require_verified, so patch the
    # module attribute it will actually read.
    import pact.core.auth_store as store_module

    monkeypatch.setattr(store_module, "auth_store", _Down())

    token = jwt.encode(
        {"sub": "usr_x", "role": "buyer", "ver": 0, "exp": 9999999999,
         "iss": "pact", "aud": "pact-api"},
        settings.auth.secret, algorithm="HS256",
    )
    response = client.post("/api/jobs/whatever/run", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503, "an unavailable store must fail closed, not open"


def test_a_legacy_operator_token_still_reaches_the_money_routes(client, monkeypatch, buyer_headers):
    """The operator workflow is preserved: no customer row means pre-verified.

    The gate must not turn the pre-existing operator credential into a refusal —
    that would be a regression in a previously verified guarantee.
    """
    response = client.post("/api/jobs/nonexistent-job/run", headers=buyer_headers)
    # The gate passed; the 404 is the missing job, proving no 403/503.
    assert response.status_code == 404


def test_a_wrong_audience_token_is_rejected(client, monkeypatch):
    import jwt

    from pact.config import settings

    token = jwt.encode(
        {"sub": "buyer", "role": "buyer", "exp": 9999999999, "iss": "pact", "aud": "someone-else"},
        settings.auth.secret, algorithm="HS256",
    )
    response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
