"""Customer account lifecycle: registration, verification, sessions, recovery.

These exercise :class:`pact.core.accounts.AccountsService` directly. They run
against PostgreSQL when ``DATABASE_URL`` is set (the production path) and fall
back to the in-memory store otherwise, so the security decisions — enumeration
resistance, role confinement, single-use tokens, family revocation — are covered
in both modes.
"""

from __future__ import annotations

import os

import pytest

from pact.core import passwords
from pact.core.accounts import AccountsService, AuthError

DATABASE_URL = os.getenv("DATABASE_URL", "")


@pytest.fixture()
def store():
    if DATABASE_URL:
        from pact.core.auth_store import PostgresAuthStore

        s = PostgresAuthStore(DATABASE_URL)
        import psycopg

        with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "TRUNCATE users, email_verifications, password_reset_tokens, refresh_tokens, "
                "login_attempts, email_outbox, auth_events"
            )
        return s
    from pact.core.auth_store import InMemoryAuthStore

    return InMemoryAuthStore()


@pytest.fixture()
def svc(store):
    return AccountsService(store)


def _register_and_verify(svc, store, email, password="correct-horse-battery"):
    svc.register(email=email, password=password, role="buyer")
    user = store.get_user_by_email(email)
    # Pull the raw token straight from the outbox, as the email link would carry it.
    _verify_via_outbox(svc, store, user.id)
    return store.get_user_by_email(email)


def _verify_via_outbox(svc, store, user_id):
    """Find the newest verification token and consume it, whatever the store."""
    if DATABASE_URL:
        import psycopg

        with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT token_raw FROM email_outbox WHERE user_id = %s AND kind = 'verify_email' "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            )
            row = cur.fetchone()
        raw = row[0]
    else:
        raw = next(
            m.token_raw for m in store._outbox.values()
            if m.user_id == user_id and m.kind == "verify_email"
        )
    svc.verify_email(raw)


def test_registration_creates_only_a_buyer_or_seller(svc):
    svc.register(email="a@example.com", password="correct-horse-battery", role="buyer")
    with pytest.raises(AuthError) as exc:
        svc.register(email="b@example.com", password="correct-horse-battery", role="admin")
    assert exc.value.status_code == 400


def test_registration_response_is_identical_for_new_and_existing_addresses(svc):
    first = svc.register(email="dup@example.com", password="correct-horse-battery", role="buyer")
    second = svc.register(email="dup@example.com", password="correct-horse-battery", role="buyer")
    assert first == second, "the response must not reveal whether the address existed"


def test_a_reserved_machine_identity_cannot_be_registered(svc):
    with pytest.raises(AuthError):
        svc.register(email="seller_atlas", password="correct-horse-battery", role="seller")


def test_login_before_verification_fails_generically(svc, store):
    svc.register(email="unverified@example.com", password="correct-horse-battery", role="buyer")
    with pytest.raises(AuthError) as exc:
        svc.login(email="unverified@example.com", password="correct-horse-battery")
    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid email or password."


def test_login_with_a_wrong_password_is_generic(svc, store):
    _register_and_verify(svc, store, "ok@example.com")
    with pytest.raises(AuthError) as exc:
        svc.login(email="ok@example.com", password="wrong-password-here")
    assert exc.value.status_code == 401


def test_login_for_an_unknown_address_is_generic(svc):
    with pytest.raises(AuthError) as exc:
        svc.login(email="nobody@example.com", password="correct-horse-battery")
    assert exc.value.status_code == 401


def test_successful_login_issues_an_access_and_refresh_pair(svc, store):
    user = _register_and_verify(svc, store, "sess@example.com")
    session = svc.login(email="sess@example.com", password="correct-horse-battery")
    assert session["access_token"] and session["refresh_token"]
    assert session["user"]["id"] == user.id
    assert session["user"]["email_verified"] is True


def test_refresh_rotates_and_reuse_revokes_the_family(svc, store):
    _register_and_verify(svc, store, "rot@example.com")
    session = svc.login(email="rot@example.com", password="correct-horse-battery")
    first = session["refresh_token"]

    rotated = svc.refresh(first)
    assert rotated["refresh_token"] != first

    # Replaying the spent token is theft: the family is revoked.
    with pytest.raises(AuthError):
        svc.refresh(first)
    # The token issued by the legitimate rotation is now also dead.
    with pytest.raises(AuthError):
        svc.refresh(rotated["refresh_token"])


def test_login_is_rate_limited_after_repeated_failures(svc, store):
    _register_and_verify(svc, store, "rl@example.com")
    for _ in range(8):
        with pytest.raises(AuthError):
            svc.login(email="rl@example.com", password="wrong-password-here")
    with pytest.raises(AuthError) as exc:
        svc.login(email="rl@example.com", password="correct-horse-battery")
    assert exc.value.status_code == 429, "the throttle must refuse even the right password"


def test_suspension_blocks_login_and_refresh(svc, store):
    user = _register_and_verify(svc, store, "susp@example.com")
    session = svc.login(email="susp@example.com", password="correct-horse-battery")
    svc.suspend(user.id)
    with pytest.raises(AuthError):
        svc.login(email="susp@example.com", password="correct-horse-battery")
    with pytest.raises(AuthError):
        svc.refresh(session["refresh_token"])


def test_password_reset_is_single_use_and_revokes_sessions(svc, store):
    user = _register_and_verify(svc, store, "reset@example.com")
    session = svc.login(email="reset@example.com", password="correct-horse-battery")

    svc.request_password_reset(email="reset@example.com")
    raw = _latest_reset_token(store, user.id)
    svc.reset_password(raw_token=raw, new_password="a-brand-new-password")

    # The old session is gone...
    with pytest.raises(AuthError):
        svc.refresh(session["refresh_token"])
    # ...the token is single-use...
    with pytest.raises(AuthError):
        svc.reset_password(raw_token=raw, new_password="another-password-x")
    # ...and the new password works while the old one does not.
    with pytest.raises(AuthError):
        svc.login(email="reset@example.com", password="correct-horse-battery")
    assert svc.login(email="reset@example.com", password="a-brand-new-password")


def _latest_reset_token(store, user_id):
    if DATABASE_URL:
        import psycopg

        with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT token_raw FROM email_outbox WHERE user_id = %s AND kind = 'password_reset' "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            )
            return cur.fetchone()[0]
    return next(
        m.token_raw for m in store._outbox.values()
        if m.user_id == user_id and m.kind == "password_reset"
    )


def test_change_password_requires_the_current_one(svc, store):
    user = _register_and_verify(svc, store, "chg@example.com")
    with pytest.raises(AuthError) as exc:
        svc.change_password(user_id=user.id, current_password="nope-nope-nope", new_password="a-long-new-password")
    assert exc.value.status_code == 401
    svc.change_password(user_id=user.id, current_password="correct-horse-battery", new_password="a-long-new-password")
    assert svc.login(email="chg@example.com", password="a-long-new-password")


def test_reset_request_for_unknown_address_is_uniform(svc):
    message = svc.request_password_reset(email="ghost@example.com")
    assert "reset" in message.lower()


def test_password_hashing_roundtrip_and_pepper(monkeypatch):
    stored = passwords.hash_password("correct-horse-battery")
    assert passwords.verify_password("correct-horse-battery", stored)
    assert not passwords.verify_password("wrong", stored)

    monkeypatch.setenv("PACT_PASSWORD_PEPPER", "server-side-secret")
    peppered = passwords.hash_password("correct-horse-battery")
    assert passwords.verify_password("correct-horse-battery", peppered)
    # A peppered hash is not verifiable without the pepper.
    monkeypatch.delenv("PACT_PASSWORD_PEPPER")
    assert not passwords.verify_password("correct-horse-battery", peppered)


def test_short_passwords_are_refused(svc):
    with pytest.raises(AuthError):
        svc.register(email="short@example.com", password="tiny", role="buyer")


def test_outbound_email_is_bounded_per_address(svc, store):
    """Repeated resets cannot queue unbounded messages to one mailbox.

    Every send uses a fresh idempotency key (so a legitimate resend is never
    blocked by a stale one), which means only a per-address cap stops an email
    bomb. The cap is enforced before the message is queued.
    """
    from pact.core.accounts import OUTBOUND_EMAIL_LIMIT

    svc.register(email="bomb@example.com", password="correct-horse-battery", role="buyer")
    user = store.get_user_by_email("bomb@example.com")
    # Registration itself queues one verification message and consumes one slot.
    baseline = _outbox_count(store)
    assert baseline == 1

    queued = []
    for _ in range(OUTBOUND_EMAIL_LIMIT + 5):
        before = _outbox_count(store)
        svc._queue_reset(user, ip_hash=None)
        queued.append(_outbox_count(store) - before)

    # The address may receive at most OUTBOUND_EMAIL_LIMIT messages in the window,
    # so at most that many minus the verification already queued get through.
    assert sum(queued) == OUTBOUND_EMAIL_LIMIT - 1, queued
    assert all(n == 1 for n in queued[: OUTBOUND_EMAIL_LIMIT - 1])
    assert all(n == 0 for n in queued[OUTBOUND_EMAIL_LIMIT - 1:]), queued


def _outbox_count(store) -> int:
    if DATABASE_URL:
        import psycopg

        with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM email_outbox")
            return cur.fetchone()[0]
    return len(store._outbox)
