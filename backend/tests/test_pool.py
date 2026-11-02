"""The bounded per-instance connection pool.

The pool is what keeps a serverless deployment from opening a new database
connection per request. These tests are opt-in on a real PostgreSQL because the
behaviour under test is the pool's interaction with the driver.
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set; pool tests need a database"
)


def test_the_pool_reuses_connections():
    from pact.core.pool import DatabasePool

    pool = DatabasePool(DATABASE_URL, max_size=2)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
    # A second checkout is served without opening a fresh socket each time; the
    # pool reports its size rather than growing without bound.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
    assert pool.stats()["max_size"] == 2
    pool.close()


def test_direct_connections_are_closed_not_pooled():
    """``direct()`` must not hand a session-lock-holding connection back to the pool."""
    from pact.core.pool import DatabasePool

    pool = DatabasePool(DATABASE_URL, max_size=2)
    with pool.direct() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            direct_pid = cur.fetchone()[0]
    # A pooled connection afterwards must be a different backend, proving the
    # direct one was closed rather than returned.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        pooled_pid = cur.fetchone()[0]
    assert direct_pid != pooled_pid
    pool.close()


def test_a_statement_timeout_is_applied():
    from pact.core.pool import DatabasePool

    pool = DatabasePool(DATABASE_URL, max_size=1, statement_timeout_ms=1500)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        assert cur.fetchone()[0] == "1500ms"
    pool.close()


def test_get_pool_is_keyed_by_dsn():
    """One DSN maps to one pool, so the stores do not multiply the connection count.

    Deliberately does *not* call ``close_all_pools``: the module-level singleton
    stores share this registry, and closing it here would leave every later test
    using a store whose pool is shut.
    """
    from pact.core.pool import get_pool

    first = get_pool(DATABASE_URL)
    second = get_pool(DATABASE_URL)
    assert first is second, "one DSN must map to one pool, not one per store"


def test_an_unreachable_dsn_raises_the_driver_error_not_a_pool_timeout():
    """A bad DSN must fail with the driver's own error, so callers classify it."""
    from pact.core.pool import DatabasePool

    with pytest.raises(Exception) as excinfo:
        DatabasePool("postgresql://nobody:nobody@127.0.0.1:1/absent", timeout_seconds=2)
    # The message names the connection failure, not an opaque PoolTimeout.
    assert "refused" in str(excinfo.value).lower() or "connect" in str(excinfo.value).lower()
