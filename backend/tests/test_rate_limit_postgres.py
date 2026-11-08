"""Shared money-route rate limiting on real PostgreSQL.

The process-local counter is correct for one process and wrong for a deployment
that runs several: a caller could spend its whole budget once per instance. These
tests prove the shared counter closes that hole — the decision lives in the
database, not in the object that made it. They are skipped unless ``DATABASE_URL``
points at a reachable Postgres, because the property under test is *cross-instance*,
which an in-memory double cannot model.

Run with:
    DATABASE_URL=postgresql://... pytest tests/test_rate_limit_postgres.py -v
"""

from __future__ import annotations

import os
import threading

import pytest

from pact.core.rate_limit import PostgresRateLimiter, RateLimitRule

DATABASE_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set; Postgres tests are opt-in"
)


def _truncate() -> None:
    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS rate_limit_events ("
            "id BIGSERIAL PRIMARY KEY, scope TEXT NOT NULL, key TEXT NOT NULL, "
            "at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur.execute("TRUNCATE rate_limit_events")


@pytest.fixture()
def limiter() -> PostgresRateLimiter:
    store = PostgresRateLimiter(DATABASE_URL)
    _truncate()
    return store


def test_the_first_requests_are_allowed_then_refused(limiter):
    rule = RateLimitRule(limit=3, window_seconds=300)
    for i in range(3):
        assert limiter.check("s", "k", rule).allowed is True, f"request {i + 1}"
    assert limiter.check("s", "k", rule).allowed is False


def test_a_second_instance_sees_the_same_count(limiter):
    """The decision lives in the database, not in the object that made it."""
    rule = RateLimitRule(limit=2, window_seconds=300)
    assert limiter.check("s", "shared", rule).allowed is True
    # A completely independent limiter, as a second API instance would build.
    other = PostgresRateLimiter(DATABASE_URL)
    assert other.check("s", "shared", rule).allowed is True  # second of two
    # Either instance now refuses the third.
    assert limiter.check("s", "shared", rule).allowed is False
    assert other.check("s", "shared", rule).allowed is False


def test_two_instances_racing_allow_exactly_the_limit(limiter):
    """Under a race, the total allowed must not exceed the configured limit."""
    rule = RateLimitRule(limit=5, window_seconds=300)
    # Build every instance up front: constructing one runs CREATE TABLE IF NOT
    # EXISTS, and doing that concurrently is itself racy. The race under test is in
    # ``check``, not in construction.
    instances = [PostgresRateLimiter(DATABASE_URL) for _ in range(8)]
    outcomes: list[bool] = []
    barrier = threading.Barrier(8)

    def attempt(local: PostgresRateLimiter) -> None:
        barrier.wait(timeout=10)
        outcomes.append(local.check("s", "race", rule).allowed)

    threads = [threading.Thread(target=attempt, args=(inst,)) for inst in instances]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert outcomes.count(True) == 5, f"exactly the limit must win, got {outcomes}"


def test_an_unreachable_store_refuses(monkeypatch):
    """A shared limiter that cannot reach its store must refuse, not allow."""
    store = PostgresRateLimiter(DATABASE_URL)

    class _Boom:
        def connection(self, *a, **k):
            raise RuntimeError("database down")

    monkeypatch.setattr(store, "_pool", _Boom())
    decision = store.check("s", "k", RateLimitRule(limit=10, window_seconds=300))
    assert decision.allowed is False
    assert decision.scope == "unavailable"


def test_record_usage_counts_without_gating(limiter):
    """``record`` increments the window for a request that was already served."""
    rule = RateLimitRule(limit=1, window_seconds=300)
    limiter.record("s", "rec")  # one usage, no decision
    assert limiter.check("s", "rec", rule).allowed is False


def _insert(scope: str, key: str, age_seconds: int) -> None:
    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rate_limit_events (scope, key, at) "
            "VALUES (%s, %s, now() - make_interval(secs => %s))",
            (scope, key, age_seconds),
        )


def test_prune_removes_expired_rows_and_keeps_fresh_ones(limiter):
    """Cleanup bounds the table without touching rows still inside a window."""
    from pact.core.rate_limit import retention_seconds

    _insert("s", "old", retention_seconds() + 600)
    _insert("s", "fresh", 5)
    removed = limiter.prune()
    assert removed == 1
    # The fresh row survives, so a live count is unchanged.
    assert limiter.check("s", "fresh", RateLimitRule(limit=1, window_seconds=300)).allowed is False


def test_prune_is_bounded_by_the_batch(limiter):
    from pact.core.rate_limit import retention_seconds

    for _ in range(5):
        _insert("s", "old", retention_seconds() + 600)
    assert limiter.prune(limit=2) == 2


def test_a_row_inside_the_configured_window_is_never_pruned(limiter):
    """A window longer than the default floor must not have its active rows deleted.

    The default retention floor is one hour; a row ten minutes old is inside any
    sane window and must survive a prune.
    """
    _insert("s", "recent", 600)
    limiter.prune()
    assert limiter.check("s", "recent", RateLimitRule(limit=1, window_seconds=3600)).allowed is False


# ------------------------------------------- the lock, deterministically


def _advisory_key(scope: str, key: str) -> int:
    """The exact advisory-lock key ``check`` takes, computed by the server.

    Derived with the same expression ``check`` uses, so the test locks what the code
    locks without duplicating the hash in Python.
    """
    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT hashtextextended(%s, 0)", (f"{scope}:{key}",))
        return cur.fetchone()[0]


def test_check_waits_on_the_per_identity_advisory_lock(limiter):
    """Deterministic proof that ``check`` takes the lock the concurrency fix added.

    A concurrent session holds the advisory lock for ``(scope, key)``; ``check`` for
    that same identity must block until it is released — an ordering fact, not a
    timing race. A different identity is not blocked, proving the lock is per identity.
    """
    import time

    import psycopg

    store = PostgresRateLimiter(DATABASE_URL)
    _truncate()
    rule = RateLimitRule(limit=10, window_seconds=300)
    key_int = _advisory_key("s", "locked-identity")

    holder = psycopg.connect(DATABASE_URL)
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (key_int,))  # held until commit

        blocked: list[float] = []
        done = threading.Event()

        def call() -> None:
            start = time.monotonic()
            store.check("s", "locked-identity", rule)
            blocked.append(time.monotonic() - start)
            done.set()

        t = threading.Thread(target=call)
        t.start()
        assert not done.wait(timeout=1.5), "check must block while the lock is held"

        # A different identity is not serialised behind the held lock.
        other_start = time.monotonic()
        store.check("s", "other-identity", rule)
        assert time.monotonic() - other_start < 1.0, "the lock must be per identity"

        holder.commit()  # release
        assert done.wait(timeout=10), "check must proceed once the lock is released"
        t.join(timeout=5)
        assert blocked and blocked[0] >= 1.0
    finally:
        holder.close()


# --------------------------------------------- through a transaction-mode pooler

POOLED_URL = os.getenv("PGBOUNCER_URL", "")


@pytest.mark.skipif(not POOLED_URL, reason="PGBOUNCER_URL not set; need a transaction-mode pooler")
def test_the_race_is_closed_through_a_transaction_mode_pooler():
    """The advisory lock is transaction-scoped, so it holds through PgBouncer too.

    Production's ``DATABASE_URL`` is a transaction-mode pooler. The count-then-record
    pair must stay atomic there, not only against a direct connection.
    """
    import psycopg

    with psycopg.connect(POOLED_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS rate_limit_events ("
            "id BIGSERIAL PRIMARY KEY, scope TEXT NOT NULL, key TEXT NOT NULL, "
            "at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur.execute("TRUNCATE rate_limit_events")

    rule = RateLimitRule(limit=5, window_seconds=300)
    instances = [PostgresRateLimiter(POOLED_URL) for _ in range(8)]
    outcomes: list[bool] = []
    barrier = threading.Barrier(8)

    def attempt(local: PostgresRateLimiter) -> None:
        barrier.wait(timeout=10)
        outcomes.append(local.check("s", "pooled-race", rule).allowed)

    threads = [threading.Thread(target=attempt, args=(inst,)) for inst in instances]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert outcomes.count(True) == 5, f"exactly the limit must win, got {outcomes}"
