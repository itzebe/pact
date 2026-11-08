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
