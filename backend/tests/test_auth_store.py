"""Durable account store: sessions, revocation, rate limiting, and the email outbox.

The PostgreSQL cases prove the properties that only a shared store can give:
a refresh token rotates exactly once across instances, a suspension is visible
everywhere, and concurrent outbox dispatchers never claim the same message. The
in-memory cases exercise the same interface for the single-instance path.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from pact.core.auth_store import (
    STATUS_ACTIVE,
    STATUS_SUSPENDED,
    AccountStateError,
    InMemoryAuthStore,
    OutboxMessage,
    UserRecord,
    normalize_email,
    token_hash,
)

DATABASE_URL = os.getenv("DATABASE_URL", "")
pg_only = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set")


def _user(email: str, **kw) -> UserRecord:
    import secrets

    return UserRecord(id="usr_" + secrets.token_hex(6), email=email, role="buyer", **kw)


# --------------------------------------------------------------------------- #
# In-memory (single-instance) cases
# --------------------------------------------------------------------------- #


def test_inmemory_user_roundtrip_and_state():
    store = InMemoryAuthStore()
    user = _user("a@example.com", email_verified_at=datetime.now(timezone.utc))
    store.create_user(user)
    assert store.get_user_by_id(user.id).email == "a@example.com"
    assert store.get_user_by_email("A@EXAMPLE.COM").id == user.id
    assert store.account_state(user.id) == (STATUS_ACTIVE, 0, True)


def test_inmemory_duplicate_email_is_refused():
    store = InMemoryAuthStore()
    store.create_user(_user("dup@example.com"))
    with pytest.raises(Exception):
        store.create_user(_user("dup@example.com"))


def test_inmemory_refresh_rotation_is_single_use():
    store = InMemoryAuthStore()
    user = _user("r@example.com")
    store.create_user(user)
    store.create_refresh_token("raw-1", user.id, "fam-1", timedelta(days=1))
    result = store.rotate_refresh_token("raw-1", timedelta(days=1))
    assert isinstance(result, tuple) and result[0] == user.id
    # Presenting the spent token again is reported as reuse, not as a valid token.
    assert store.rotate_refresh_token("raw-1", timedelta(days=1)) == "reuse"


def test_inmemory_revoke_family_kills_rotated_children():
    store = InMemoryAuthStore()
    user = _user("f@example.com")
    store.create_user(user)
    store.create_refresh_token("raw-1", user.id, "fam-x", timedelta(days=1))
    _, fam, raw2 = store.rotate_refresh_token("raw-1", timedelta(days=1))
    assert store.revoke_family(fam) >= 1
    assert store.rotate_refresh_token(raw2, timedelta(days=1)) is None


def test_inmemory_email_verification_is_single_use_and_expires():
    store = InMemoryAuthStore()
    store.create_email_verification("vtok", "usr_1", "v@example.com", timedelta(hours=1))
    assert store.consume_email_verification("vtok") == "usr_1"
    assert store.consume_email_verification("vtok") is None  # already consumed
    store.create_email_verification("expired", "usr_2", "e@example.com", timedelta(seconds=-1))
    assert store.consume_email_verification("expired") is None


def test_inmemory_rate_limit_counts_only_failures_in_window():
    store = InMemoryAuthStore()
    store.record_attempt("login", "h1", False)
    store.record_attempt("login", "h1", False)
    store.record_attempt("login", "h1", True)  # a success is not counted
    assert store.count_recent_failures("login", "h1", 900) == 2
    assert store.count_recent_failures("login", "h2", 900) == 0


def test_inmemory_outbox_idempotency_key_is_unique():
    store = InMemoryAuthStore()
    first = OutboxMessage(id="m1", kind="verify_email", user_id="u", to_email="a@b.co",
                          token_hash="h", idempotency_key="k1", attempts=0)
    dup = OutboxMessage(id="m2", kind="verify_email", user_id="u", to_email="a@b.co",
                        token_hash="h2", idempotency_key="k1", attempts=0)
    assert store.enqueue_email(first) is True
    assert store.enqueue_email(dup) is False, "a duplicate key must be a no-op"


# --------------------------------------------------------------------------- #
# PostgreSQL (shared, cross-instance) cases
# --------------------------------------------------------------------------- #


@pytest.fixture()
def pg_store():
    from pact.core.auth_store import PostgresAuthStore

    store = PostgresAuthStore(DATABASE_URL)
    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE users, email_verifications, password_reset_tokens, refresh_tokens, "
            "login_attempts, email_outbox, auth_events"
        )
    return store


@pg_only
def test_pg_user_roundtrip(pg_store):
    user = _user("pg@example.com", email_verified_at=datetime.now(timezone.utc))
    pg_store.create_user(user)
    got = pg_store.get_user_by_email("PG@example.com")
    assert got is not None and got.id == user.id
    assert pg_store.account_state(user.id) == (STATUS_ACTIVE, 0, True)


@pg_only
def test_pg_refresh_rotation_is_single_use_across_instances(pg_store):
    from pact.core.auth_store import PostgresAuthStore

    user = _user("pg-rot@example.com", email_verified_at=datetime.now(timezone.utc))
    pg_store.create_user(user)
    pg_store.create_refresh_token("raw-pg", user.id, "fam-pg", timedelta(days=1))

    # A second instance, with its own store object, shares the same database.
    other = PostgresAuthStore(DATABASE_URL)
    assert isinstance(other.rotate_refresh_token("raw-pg", timedelta(days=1)), tuple)
    assert other.rotate_refresh_token("raw-pg", timedelta(days=1)) == "reuse"


@pg_only
def test_pg_concurrent_rotation_allows_exactly_one_winner(pg_store):
    from pact.core.auth_store import PostgresAuthStore

    user = _user("pg-race@example.com", email_verified_at=datetime.now(timezone.utc))
    pg_store.create_user(user)
    pg_store.create_refresh_token("raw-race", user.id, "fam-race", timedelta(days=1))

    outcomes: list[object] = []
    barrier = threading.Barrier(6)

    def attempt():
        store = PostgresAuthStore(DATABASE_URL)
        barrier.wait(timeout=10)
        outcomes.append(store.rotate_refresh_token("raw-race", timedelta(days=1)))

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    winners = [o for o in outcomes if isinstance(o, tuple)]
    assert len(winners) == 1, f"exactly one rotation may win, got {outcomes}"


@pg_only
def test_pg_account_state_reports_suspension_and_version(pg_store):
    user = _user("pg-susp@example.com", email_verified_at=datetime.now(timezone.utc))
    pg_store.create_user(user)
    pg_store.update_user(user.id, status=STATUS_SUSPENDED, token_version=3)
    assert pg_store.account_state(user.id) == (STATUS_SUSPENDED, 3, True)


@pg_only
def test_pg_account_state_raises_when_unavailable():
    from pact.core.auth_store import UnavailableAuthStore

    store = UnavailableAuthStore(RuntimeError("boom"))
    with pytest.raises(AccountStateError):
        store.account_state("usr_x")


@pg_only
def test_pg_outbox_claim_is_exclusive_and_recovers_an_expired_lease(pg_store):
    import psycopg

    msg = OutboxMessage(id="pg-m1", kind="verify_email", user_id="u1", to_email="a@b.co",
                        token_hash="h1", idempotency_key="k-pg-1", attempts=0, token_raw="raw")
    assert pg_store.enqueue_email(msg) is True
    # A duplicate key is refused by the unique index.
    assert pg_store.enqueue_email(
        OutboxMessage(id="pg-m2", kind="verify_email", user_id="u1", to_email="a@b.co",
                      token_hash="h2", idempotency_key="k-pg-1", attempts=0)
    ) is False

    claimed = pg_store.claim_outbox(10)
    assert len(claimed) == 1 and claimed[0].id == "pg-m1" and claimed[0].token_raw == "raw"

    # While the lease is live, another dispatcher cannot reclaim it.
    assert pg_store.claim_outbox(10) == []

    # Force the lease into the past, as a crashed dispatcher would leave it.
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE email_outbox SET lease_expires_at = now() - interval '1 hour' WHERE id = 'pg-m1'"
        )
    recovered = pg_store.claim_outbox(10)
    assert len(recovered) == 1, "an abandoned lease must be reclaimed"


@pg_only
def test_pg_concurrent_dispatchers_never_claim_the_same_row(pg_store):
    from pact.core.auth_store import PostgresAuthStore

    for i in range(8):
        pg_store.enqueue_email(
            OutboxMessage(id=f"pg-c{i}", kind="verify_email", user_id="u", to_email="a@b.co",
                          token_hash=f"h{i}", idempotency_key=f"k-pg-c{i}", attempts=0)
        )
    claimed_ids: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def drain():
        store = PostgresAuthStore(DATABASE_URL)
        barrier.wait(timeout=10)
        while True:
            batch = store.claim_outbox(1)
            if not batch:
                break
            with lock:
                claimed_ids.extend(m.id for m in batch)

    threads = [threading.Thread(target=drain) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(claimed_ids) == len(set(claimed_ids)), f"a row was claimed twice: {claimed_ids}"
    assert set(claimed_ids) == {f"pg-c{i}" for i in range(8)}


@pg_only
def test_pg_prune_is_bounded_and_removes_only_terminal_rows(pg_store):
    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        for i in range(25):
            cur.execute(
                "INSERT INTO email_outbox (id, kind, user_id, to_email, idempotency_key, status, completed_at) "
                "VALUES (%s, 'verify_email', 'u', 'a@b.co', %s, 'sent', now() - interval '30 days')",
                (f"old-{i}", f"oldkey-{i}"),
            )
        # A pending row must never be pruned.
        cur.execute(
            "INSERT INTO email_outbox (id, kind, user_id, to_email, idempotency_key, status) "
            "VALUES ('pending-1', 'verify_email', 'u', 'a@b.co', 'pendkey', 'pending')"
        )
    removed = pg_store.prune("email_outbox", limit=10)
    assert removed == 10, f"prune must honour the batch limit, got {removed}"
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM email_outbox WHERE id = 'pending-1'")
        assert cur.fetchone()[0] == 1, "a pending message must survive cleanup"


@pg_only
def test_pg_email_verification_consume_is_atomic(pg_store):
    pg_store.create_email_verification("vtok-pg", "usr_1", "v@example.com", timedelta(hours=1))
    assert pg_store.consume_email_verification("vtok-pg") == "usr_1"
    assert pg_store.consume_email_verification("vtok-pg") is None


@pg_only
def test_pg_normalize_and_hash_never_store_plaintext_email():
    assert normalize_email("  Mixed@Example.COM ") == "mixed@example.com"
    # The rate-limit identifier is a hash, so a dump does not enumerate addresses.
    from pact.core.auth_store import hash_identifier

    assert hash_identifier("a@example.com") != "a@example.com"
    assert token_hash("raw") == token_hash("raw")
