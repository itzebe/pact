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


def test_statement_timeout_is_not_a_connection_startup_parameter():
    """Regression: a managed pooler rejects ``options=-c statement_timeout=...``.

    Passing the timeout as a libpq startup parameter fails *every* connection
    behind Neon/pgbouncer with "unsupported startup parameter in options:
    statement_timeout", which took production down. It must be applied with a
    post-connect ``SET`` instead, so no ``options`` startup parameter is sent.
    """
    from pact.core.pool import DatabasePool

    pool = DatabasePool(DATABASE_URL, max_size=1, statement_timeout_ms=1500)
    try:
        assert not pool._pool.kwargs.get("options"), "no options startup parameter"
        # The connection is still usable and the timeout is in effect.
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SHOW statement_timeout")
            assert cur.fetchone()[0] == "1500ms"
    finally:
        pool.close()


def test_a_zero_statement_timeout_leaves_the_server_default():
    from pact.core.pool import DatabasePool

    pool = DatabasePool(DATABASE_URL, max_size=1, statement_timeout_ms=0)
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SHOW statement_timeout")
            assert cur.fetchone()[0] == "0"
    finally:
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


def test_a_pooled_connection_never_prepares_statements_server_side():
    """Regression: the pool disables psycopg3's automatic statement preparation.

    psycopg3 prepares a statement after 5 executions and names it ``_pg3_0``.
    Behind a transaction-mode pooler that name outlives the client that created
    it: the pooler hands the same *server* connection to the next client, whose
    fresh session re-issues ``PREPARE _pg3_0`` and raises
    ``DuplicatePreparedStatement`` (reproduced locally with a size-1 pooler — see
    the module docstring of ``test_settlement_pooling``). The pool therefore
    disables preparation, so no ``_pg3_`` statement is ever created.

    A direct endpoint is used here so the assertion sees only this connection's
    statements, not a neighbour's leftovers on a shared pooler connection.
    """
    from pact.core.pool import DatabasePool

    pool = DatabasePool(DATABASE_URL, max_size=1)
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            for i in range(8):  # more than the driver default threshold of 5
                cur.execute("SELECT %s::int", (i,))
                cur.fetchall()
            cur.execute("SELECT count(*) FROM pg_prepared_statements WHERE name ~ '^_pg3_'")
            assert cur.fetchone()[0] == 0, "a pooled connection prepared a statement"
    finally:
        pool.close()


def test_the_driver_prepares_by_default_so_disabling_it_is_load_bearing():
    """Negative control for the test above.

    Proves psycopg3 *does* prepare by default, so the pool's ``prepare_threshold``
    is doing real work rather than passing because the driver never prepares.
    """
    import psycopg

    conn = psycopg.connect(DATABASE_URL)  # driver default: prepare after 5 executions
    try:
        with conn.cursor() as cur:
            for i in range(8):
                cur.execute("SELECT %s::int", (i,))
                cur.fetchall()
            cur.execute("SELECT count(*) FROM pg_prepared_statements WHERE name ~ '^_pg3_'")
            assert cur.fetchone()[0] >= 1, "the driver did not prepare; the control is void"
    finally:
        conn.close()


def test_prepare_threshold_config_disables_preparation_by_default(monkeypatch):
    """The env var must map an unset/``none`` value to ``None``, never to ``0``.

    With psycopg3 ``prepare_threshold=0`` means "prepare on the first execution",
    the opposite of disabling preparation, so a naive ``int(os.getenv(...) or 0)``
    would re-introduce the pooler bug.
    """
    from pact.config import _optional_int

    monkeypatch.delenv("PACT_PG_PREPARE_THRESHOLD", raising=False)
    assert _optional_int("PACT_PG_PREPARE_THRESHOLD") is None
    monkeypatch.setenv("PACT_PG_PREPARE_THRESHOLD", "none")
    assert _optional_int("PACT_PG_PREPARE_THRESHOLD") is None
    monkeypatch.setenv("PACT_PG_PREPARE_THRESHOLD", "5")
    assert _optional_int("PACT_PG_PREPARE_THRESHOLD") == 5
