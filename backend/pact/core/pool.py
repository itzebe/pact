"""A small, bounded per-instance PostgreSQL connection pool.

Why this exists
---------------
Every store in this package used to open a brand-new ``psycopg.connect()`` for
each operation. That is fine for a single container, but a serverless deployment
runs many short-lived function instances, and each cold start that opens a raw
connection per request can exhaust the database's connection budget. A bounded
pool keeps each instance's footprint small and fixed.

What it deliberately is NOT
---------------------------
A pool is **per instance**, so it does not create a global connection limit. The
worst case is ``active function instances x max_size``; the operator must budget
for that against the database plan (see ``docs/AUTH.md``). Nothing here can make
that number smaller, only predictable.

The settlement lock is not pooled
---------------------------------
``PostgresJobStore.settlement_claim`` holds a *session* advisory lock across a
whole protected operation. It must not be returned to a pool mid-operation, so it
uses a dedicated direct connection (see ``_direct_connect``). A transaction-mode
pooler is likewise unusable there: it would release the session lock between
transactions and silently stop protecting the settlement.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger("pact.pool")

#: Conservative default. Small enough that many instances do not exhaust a
#: free-tier database, large enough that one instance is not a bottleneck.
DEFAULT_MAX_SIZE = 5
DEFAULT_MIN_SIZE = 0
#: How long a caller waits for a pooled connection before giving up.
DEFAULT_TIMEOUT_SECONDS = 5.0
#: Recycle a connection idle for longer than this, so a database restart or a
#: serverless compute suspend does not hand back a dead connection.
DEFAULT_MAX_IDLE_SECONDS = 300.0
#: A statement that runs longer than this is aborted by the server.
DEFAULT_STATEMENT_TIMEOUT_MS = 10_000


def _driver_pool():
    try:
        from psycopg_pool import ConnectionPool
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "DATABASE_URL is set but the connection pool is not installed. "
            "Install it with `pip install 'psycopg_pool>=3.2'`."
        ) from exc
    return ConnectionPool


def _statement_timeout_configure(ms: int):
    """Return a pool ``configure`` callback that applies ``statement_timeout``.

    It runs on every new pooled connection, immediately after connect and before
    the connection is handed out. ``SET`` is used rather than a libpq ``options``
    startup parameter: managed poolers reject the latter, and a rejected startup
    parameter fails *every* connection, which would take the deployment down.
    """

    def configure(conn: Any) -> None:
        if ms <= 0:
            return
        with conn.cursor() as cur:
            # SET does not accept a bind parameter for statement_timeout; the value
            # is an int we control (never user input), so a literal is safe here.
            cur.execute(f"SET statement_timeout = {int(ms)}")

    return configure


class DatabasePool:
    """A bounded pool for one DSN, shared by every store that needs it."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = DEFAULT_MIN_SIZE,
        max_size: int = DEFAULT_MAX_SIZE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    ) -> None:
        ConnectionPool = _driver_pool()
        self.dsn = dsn
        self.max_size = max(1, int(max_size))

        # Synchronous probe *before* any pool machinery. This raises the precise
        # driver error — an invalid connection string, bad credentials, or an
        # unreachable host — so callers classify a failure exactly as the previous
        # raw-connect code did, and a misconfigured DSN fails at startup rather
        # than surfacing later as an opaque pool timeout. The probe connection is
        # closed immediately and is not part of the pool's budget.
        import psycopg

        probe = psycopg.connect(dsn, connect_timeout=5)
        probe.close()

        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=max(0, int(min_size)),
            max_size=self.max_size,
            timeout=float(timeout_seconds),
            max_idle=float(max_idle_seconds),
            # A dead connection is discarded and reopened rather than handed out.
            check=ConnectionPool.check_connection,
            # Connections are created in autocommit mode. That lets the configure
            # callback's ``SET`` leave the connection in IDLE state (a non-autocommit
            # connection would open a transaction and the pool would discard it), and
            # every checkout sets ``autocommit`` explicitly anyway.
            kwargs={"autocommit": True},
            # statement_timeout is applied as a plain SET *after* connecting, never
            # as a libpq startup parameter. A managed pooler (Neon, pgbouncer)
            # rejects unknown startup parameters with "unsupported startup parameter
            # in options: statement_timeout", which fails every pooled connection and
            # takes the whole deployment down. A SET statement is accepted everywhere.
            configure=_statement_timeout_configure(int(statement_timeout_ms)),
            # No eager connections: a cold start must not open sockets it may not
            # need. The pool opens on first use.
            open=False,
        )
        self._pool.open()

    @contextmanager
    def connection(self, *, autocommit: bool = True) -> Iterator[Any]:
        """Check out a connection, returning it to the pool on exit."""
        with self._pool.connection() as conn:
            conn.autocommit = autocommit
            yield conn

    @contextmanager
    def direct(self, *, autocommit: bool = True) -> Iterator[Any]:
        """A dedicated connection that is *closed* on exit, never pooled.

        Used only where a session-scoped advisory lock must be held for the whole
        protected operation (settlement). Returning such a connection to the pool
        with the lock still held would leak the lock into an unrelated request.

        ``autocommit`` defaults to True to match the original settlement-lock
        connection: the lock is session-scoped, and autocommit keeps the
        connection from holding an idle transaction open for the whole operation.
        """
        import psycopg

        conn = psycopg.connect(self.dsn, autocommit=autocommit)
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Could not close a direct connection: %s", type(exc).__name__)

    def close(self) -> None:
        try:
            self._pool.close()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not close the pool: %s", type(exc).__name__)

    def stats(self) -> dict[str, Any]:
        """Pool counters, for diagnostics. Never includes the DSN or credentials."""
        try:
            return {
                "max_size": self.max_size,
                "size": self._pool.get_stats().get("pool_size"),
                "available": self._pool.get_stats().get("pool_available"),
            }
        except Exception:  # pragma: no cover - defensive
            return {"max_size": self.max_size}


_pools: dict[str, DatabasePool] = {}
_pools_guard = threading.Lock()


def get_pool(dsn: str, **kwargs: Any) -> DatabasePool:
    """Return the process-wide pool for ``dsn``, creating it on first use.

    Keyed by DSN so the job store, the ticket store, the webhook store and the
    auth store share one bounded pool rather than one each — otherwise the
    instance's connection footprint would be multiplied by the number of stores.
    """
    with _pools_guard:
        pool = _pools.get(dsn)
        if pool is None:
            pool = DatabasePool(dsn, **kwargs)
            _pools[dsn] = pool
        return pool


def close_all_pools() -> None:
    """Close every pool. Used by tests and by a graceful shutdown."""
    with _pools_guard:
        for pool in _pools.values():
            pool.close()
        _pools.clear()
