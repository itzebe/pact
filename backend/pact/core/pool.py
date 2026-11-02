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
whole protected operation, so it must run on a genuine, dedicated database
session: ``DatabasePool.direct`` connects to the *direct* endpoint
(``DATABASE_URL_UNPOOLED``) and closes the connection on exit. Neither the
application pool nor a transaction-mode pooler may be used there — a
transaction-mode pooler (Neon's ``-pooler`` host, pgbouncer) hands the server
connection back after every transaction, so the lock is released between
statements and a second instance can take it while the first still believes it
holds it. ``direct()`` refuses to run if the endpoint it is given looks or
behaves like a pooler.

statement_timeout is transaction-local
--------------------------------------
A session-level ``SET statement_timeout`` on a *pooled* connection is not
reliable behind a transaction-mode pooler: it applies to whichever server
connection the client happened to receive and can be reset by the pooler or
handed to a different client. ``connection()`` therefore sets it with
``SET LOCAL`` inside one transaction per checkout, where it is enforced for every
statement of that unit of work. ``direct()`` has a genuine session, so a
session-level ``SET`` is correct there.
"""

from __future__ import annotations

import logging
import random
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


def _direct_endpoint_refusal(dsn: str) -> str | None:
    """Return a human-readable reason if ``dsn`` cannot host a session lock.

    A session-scoped advisory lock only protects anything if the client keeps one
    server connection for the whole operation. A transaction-mode pooler (Neon's
    ``-pooler`` host, or pgbouncer with ``pool_mode=transaction``) returns the
    server connection to its pool after every transaction, so the lock is released
    between statements and a second instance can take it while the first still
    believes it holds it. We detect that case instead of silently mis-protecting.

    Detection is two-pronged and cheap:

    * a ``-pooler`` marker in the host, which is Neon's documented pooled endpoint;
    * a live capability probe — take a session lock on one connection and check a
      second connection cannot take it. This catches a self-hosted pooler that the
      hostname does not advertise.

    Returns ``None`` when the endpoint looks like a real direct session, otherwise a
    reason string. Never includes credentials or the full DSN.
    """
    try:
        from urllib.parse import urlparse

        host = urlparse(dsn).hostname or ""
    except Exception:  # pragma: no cover - a malformed DSN fails later anyway
        host = ""
    if "-pooler" in host.lower():
        return (
            "the configured endpoint host contains '-pooler', which is a "
            "transaction-mode pooler; session-scoped settlement locks need the "
            "direct endpoint"
        )
    return None


def _probe_direct_session(dsn: str) -> str | None:
    """Live check that two connections to ``dsn`` are distinct server sessions.

    Confirms session affinity rather than trusting the hostname: a session lock is
    taken on one connection and a second connection must fail to take it. If the
    second connection *can* take it, the endpoint is pooled and unsafe for locks.

    Two details make this safe to call under concurrency:

    * the probe key is unique per call, so two simultaneous probes never contend
      with each other (or with a real settlement lock) and the verdict is about
      affinity, not about who happened to hold a shared key;
    * the lock is taken with ``pg_try_advisory_lock``, which never blocks, so a
      probe cannot hang on a lock someone else is holding.

    Returns ``None`` when the endpoint behaves as a direct session.
    """
    import psycopg

    key = random.getrandbits(62)
    first = second = None
    try:
        first = psycopg.connect(dsn, connect_timeout=5, autocommit=True)
        with first.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            if cur.fetchone()[0] is not True:  # pragma: no cover - unique key
                return "the direct-session capability probe could not take its own probe lock"
        second = psycopg.connect(dsn, connect_timeout=5, autocommit=True)
        with second.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            acquired = cur.fetchone()[0]
        if acquired:
            with second.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            return (
                "a second connection to this endpoint could take the session lock, "
                "so it is a transaction-mode pooler rather than a direct session"
            )
        return None
    except psycopg.Error:
        # A probe failure (network, permissions) must not be mistaken for safety:
        # report it as a refusal so the caller fails closed rather than guessing.
        return "the direct-session capability probe could not be completed"
    finally:
        for conn in (first, second):
            try:
                if conn is not None:
                    conn.close()
            except Exception:  # pragma: no cover - defensive
                pass


#: Cached direct-session verdicts, keyed by DSN. The probe opens two connections and
#: is therefore worth doing once per endpoint per process, not on every settlement.
_direct_verdicts: dict[str, str | None] = {}
_direct_verdicts_guard = threading.Lock()


def _cached_direct_session_verdict(dsn: str) -> str | None:
    """Memoised :func:`_probe_direct_session`; ``None`` means a real direct session."""
    with _direct_verdicts_guard:
        if dsn in _direct_verdicts:
            return _direct_verdicts[dsn]
    verdict = _probe_direct_session(dsn)
    with _direct_verdicts_guard:
        _direct_verdicts[dsn] = verdict
    return verdict


def reset_direct_session_cache() -> None:
    """Forget cached probe verdicts. Used by tests that change endpoints."""
    with _direct_verdicts_guard:
        _direct_verdicts.clear()


class DatabasePool:
    """A bounded pool for one DSN, shared by every store that needs it."""

    def __init__(
        self,
        dsn: str,
        *,
        direct_dsn: str | None = None,
        min_size: int = DEFAULT_MIN_SIZE,
        max_size: int = DEFAULT_MAX_SIZE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    ) -> None:
        ConnectionPool = _driver_pool()
        self.dsn = dsn
        # The direct endpoint for session-scoped work. Defaults to the pooled DSN so
        # a plain PostgreSQL (development, the test suite) needs no second variable;
        # a deployment behind a pooler sets DATABASE_URL_UNPOOLED and gets a real
        # session there.
        self.direct_dsn = (direct_dsn or dsn).strip() or dsn
        self.max_size = max(1, int(max_size))
        self.statement_timeout_ms = max(0, int(statement_timeout_ms))

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
            # Connections are created in autocommit mode; ``connection()`` switches
            # them to a transaction per checkout so ``SET LOCAL`` applies and a
            # multi-statement operation is not split across backends.
            kwargs={"autocommit": True},
            # No statement_timeout here. A session-level SET on a pooled connection is
            # unreliable behind a transaction-mode pooler — it can be reset, or handed
            # to another client — so the timeout is applied transaction-locally in
            # ``connection()`` instead (and session-locally in ``direct()``, which has
            # a genuine session).
            # No eager connections: a cold start must not open sockets it may not
            # need. The pool opens on first use.
            open=False,
        )
        self._pool.open()

    @contextmanager
    def connection(self, *, autocommit: bool = True) -> Iterator[Any]:
        """Check out a connection for one bounded unit of work.

        The work runs inside a single transaction with ``statement_timeout`` set
        transaction-locally. This is what makes the timeout reliable behind a
        transaction-mode pooler: a session-level ``SET`` applies to whichever server
        connection the client happened to get and can be handed to a different
        client afterwards (or reset by the pooler), whereas ``SET LOCAL`` lives
        exactly as long as the transaction and is enforced for every statement in
        it. A transaction also pins one server connection for the block, so a
        multi-statement operation is not split across backends.

        ``autocommit=False`` is for callers that manage their own transaction (the
        reputation ledger commits explicitly); the timeout still applies, and any
        transaction they leave open is rolled back on exit rather than leaking to
        the next checkout.
        """
        with self._pool.connection() as conn:
            conn.autocommit = False
            if self.statement_timeout_ms > 0:
                with conn.cursor() as cur:
                    # SET LOCAL takes no bind parameter for statement_timeout; the
                    # value is an int we control (never user input).
                    cur.execute(
                        f"SET LOCAL statement_timeout = {int(self.statement_timeout_ms)}"
                    )
            try:
                yield conn
                if autocommit:
                    conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                # The caller may have left a transaction open (autocommit=False with
                # no explicit commit); return the connection to the pool clean.
                try:
                    conn.rollback()
                except Exception:  # pragma: no cover - defensive
                    pass

    @contextmanager
    def direct(self, *, autocommit: bool = True) -> Iterator[Any]:
        """A dedicated connection to the *direct* endpoint, closed on exit.

        Used only where a session-scoped advisory lock must be held for the whole
        protected operation (settlement). Two properties are required and both are
        enforced here rather than assumed:

        * **A real session.** The connection uses ``direct_dsn``
          (``DATABASE_URL_UNPOOLED``), not the pooled DSN. A transaction-mode pooler
          returns the server connection to its pool after every transaction, so a
          session lock taken through it is released between statements and a second
          instance can take it while the first still believes it holds it.
        * **Not pooled.** The connection is closed, not returned, so a lock is never
          handed back with the connection.

        If the direct endpoint is missing or behaves like a pooler, this raises
        rather than silently using the pooled DSN — a settlement guard that does not
        guard is worse than a loud failure.
        """
        import psycopg

        dsn = self.direct_dsn
        reason = _direct_endpoint_refusal(dsn) or _cached_direct_session_verdict(dsn)
        if reason:
            raise RuntimeError(
                "Refusing to hold a session-scoped settlement lock: " + reason
            )
        conn = psycopg.connect(dsn, autocommit=autocommit)
        try:
            if self.statement_timeout_ms > 0:
                # A genuine session, so a session-level SET is reliable here.
                with conn.cursor() as cur:
                    cur.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)}")
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


def get_pool(dsn: str, *, direct_dsn: str | None = None, **kwargs: Any) -> DatabasePool:
    """Return the process-wide pool for ``dsn``, creating it on first use.

    Keyed by DSN so the job store, the ticket store, the webhook store and the
    auth store share one bounded pool rather than one each — otherwise the
    instance's connection footprint would be multiplied by the number of stores.

    ``direct_dsn`` is the endpoint session-scoped work (the settlement lock) must
    use; it defaults to ``dsn`` so a plain PostgreSQL needs nothing extra. It is
    read once, when the pool is created, and the pool is keyed by ``dsn`` alone:
    the direct endpoint is process-wide configuration, so a second store asking for
    the same DSN must reuse the pool rather than open a duplicate one.
    """
    with _pools_guard:
        pool = _pools.get(dsn)
        if pool is None:
            pool = DatabasePool(dsn, direct_dsn=direct_dsn, **kwargs)
            _pools[dsn] = pool
        return pool


def close_all_pools() -> None:
    """Close every pool. Used by tests and by a graceful shutdown."""
    with _pools_guard:
        for pool in _pools.values():
            pool.close()
        _pools.clear()
