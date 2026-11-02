"""Session affinity for the settlement lock, and a timeout that survives pooling.

The bug these tests exist for
----------------------------
Production points ``DATABASE_URL`` at Neon's pooled (``-pooler``) endpoint, which
is PgBouncer in *transaction* mode. In that mode the server connection is handed
back to the pooler after every transaction, so:

* a session-scoped ``pg_advisory_lock`` taken through it is released between
  statements — the settlement guard silently stops guarding, and two instances can
  both capture the same authorization;
* a session-level ``SET statement_timeout`` applies to whichever server connection
  the client happened to get, and can be reset or handed to another client.

The fix routes session-scoped work to a dedicated *direct* connection
(``DATABASE_URL_UNPOOLED``) and applies ``statement_timeout`` transaction-locally
(``SET LOCAL``) for pooled work. ``pool.direct()`` additionally refuses an endpoint
that looks or behaves like a pooler, so the protection cannot silently degrade.

What is proven locally
----------------------
The ``direct()``-behaviour tests need only a plain PostgreSQL (``DATABASE_URL``).
The pooling tests need a transaction-mode pooler as well (``PGBOUNCER_URL``) and
are skipped without it; CI runs PgBouncer in transaction mode so they execute
there. ``test_the_pooled_endpoint_cannot_hold_a_session_lock`` is the negative
control: it proves the pooled endpoint really does lose the lock, so the direct
endpoint is load-bearing rather than ceremonial.

Run locally with:
    DATABASE_URL=postgresql://... PGBOUNCER_URL=postgresql://.../pact \\
        pytest tests/test_settlement_pooling.py -v
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import textwrap
import time
from decimal import Decimal
from pathlib import Path

import pytest

DIRECT_URL = os.getenv("DATABASE_URL", "")
POOLED_URL = os.getenv("PGBOUNCER_URL", "")


def _probe_key() -> int:
    """A unique advisory-lock key, so a killed run can never collide with a rerun."""
    return random.getrandbits(62)


pytestmark = pytest.mark.skipif(
    not DIRECT_URL, reason="DATABASE_URL not set; these tests need a PostgreSQL"
)

BACKEND = Path(__file__).resolve().parents[1]
CRITERIA = ["The deliverable states the headline result"]

# Imported at module level so the tests read clearly; the module itself is safe to
# import without a database (the connection is only opened on use).
from pact.core.dbstore import PostgresJobStore, PostgresReputationLedger  # noqa: E402


@pytest.fixture()
def clean_db():
    store = PostgresJobStore(DIRECT_URL)
    ledger = PostgresReputationLedger(DIRECT_URL)
    import psycopg

    with psycopg.connect(DIRECT_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE jobs, reputation")
    return store, ledger


def _pid(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        return cur.fetchone()[0]


# ------------------------------------------------------- direct() session affinity


def test_direct_uses_the_unpooled_dsn_not_the_pooled_one():
    """``direct()`` must connect to the unpooled endpoint when one is configured."""
    from pact.core.pool import DatabasePool

    if not POOLED_URL:
        pytest.skip("PGBOUNCER_URL not set; need a distinct pooled endpoint to prove routing")
    pool = DatabasePool(POOLED_URL, direct_dsn=DIRECT_URL, max_size=2)
    try:
        assert pool.dsn == POOLED_URL
        assert pool.direct_dsn == DIRECT_URL
        # The pooled endpoint is the -pooler host in CI; a direct session must not
        # be taken through it. Taking the lock must succeed on the direct endpoint.
        with pool.direct() as conn:
            assert _pid(conn) > 0
    finally:
        pool.close()


def test_direct_refuses_a_transaction_mode_pooler():
    """A pooler cannot host a session lock; ``direct()`` must fail loudly, not degrade."""
    from pact.core.pool import DatabasePool

    if not POOLED_URL:
        pytest.skip("PGBOUNCER_URL not set; need a transaction-mode pooler")
    pool = DatabasePool(POOLED_URL, direct_dsn=POOLED_URL, max_size=2)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            with pool.direct():
                pass
        assert "session-scoped" in str(excinfo.value).lower()
        # The refusal never echoes the connection string.
        assert "password" not in str(excinfo.value).lower()
    finally:
        pool.close()


def test_a_pooler_host_is_refused_without_opening_a_connection():
    """The ``-pooler`` marker is caught cheaply, before any connection is made."""
    from pact.core.pool import _direct_endpoint_refusal

    reason = _direct_endpoint_refusal(
        "postgresql://u:p@ep-x-pooler.c-1.us-east-2.aws.neon.tech/pact"
    )
    assert reason and "-pooler" in reason
    assert _direct_endpoint_refusal("postgresql://u:p@ep-x.c-1.us-east-2.aws.neon.tech/pact") is None


def test_the_direct_endpoint_is_derived_from_a_neon_style_pooled_host():
    """The direct host is the pooled host with the ``-pooler`` label removed.

    This is the edit Neon documents, and it is what lets production (a Neon pooled
    endpoint with no ``DATABASE_URL_UNPOOLED``) keep its settlement guard without an
    extra variable.
    """
    from pact.core.pool import derive_direct_dsn

    assert derive_direct_dsn(
        "postgresql://user:pw@ep-x-pooler.c-1.us-east-2.aws.neon.tech/pact?sslmode=require"
    ) == "postgresql://user:pw@ep-x.c-1.us-east-2.aws.neon.tech/pact?sslmode=require"
    # A DSN with no pooler label has nothing to derive.
    assert derive_direct_dsn("postgresql://user:pw@db.internal:5432/pact") is None
    # A password containing the label must not be mangled; only the host is edited.
    assert derive_direct_dsn(
        "postgresql://user:p-pooler-w@ep-x-pooler.c-1.us-east-2.aws.neon.tech/pact"
    ) == "postgresql://user:p-pooler-w@ep-x.c-1.us-east-2.aws.neon.tech/pact"


def test_the_session_lock_is_held_across_the_whole_lifecycle():
    """A lock taken on the direct endpoint excludes a second direct connection.

    This is the property transaction-mode pooling breaks: with a real session, a
    second connection cannot take the same lock while the first still holds it.
    """
    from pact.core.pool import DatabasePool

    pool = DatabasePool(DIRECT_URL, direct_dsn=DIRECT_URL, max_size=2)
    key = _probe_key()
    try:
        with pool.direct() as holder:
            with holder.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (key,))
                cur.execute("SELECT pg_backend_pid()")
                holder_pid = cur.fetchone()[0]
            with pool.direct() as other:
                with other.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                    acquired = cur.fetchone()[0]
                assert acquired is False, (
                    "a second session took the lock while the first held it — "
                    "session affinity was not preserved"
                )
            # Same backend before and after: the lock is on one session throughout.
            with holder.cursor() as cur:
                cur.execute("SELECT pg_backend_pid()")
                assert cur.fetchone()[0] == holder_pid
            with holder.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
    finally:
        pool.close()


def test_backend_pid_is_stable_across_the_lock_lifecycle():
    """The protected operation runs on one PostgreSQL backend, start to finish."""
    from pact.core.pool import DatabasePool

    pool = DatabasePool(DIRECT_URL, direct_dsn=DIRECT_URL, max_size=2)
    key = _probe_key()
    try:
        with pool.direct() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_backend_pid()")
                before = cur.fetchone()[0]
                cur.execute("SELECT pg_advisory_lock(%s)", (key,))
                cur.execute("SELECT pg_backend_pid()")
                during = cur.fetchone()[0]
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
                cur.execute("SELECT pg_backend_pid()")
                after = cur.fetchone()[0]
        assert before == during == after, "the session changed mid-lock"
    finally:
        pool.close()


def test_an_exception_inside_the_lock_releases_it():
    """An exception must not leave the lock held (which would wedge the job)."""
    from pact.core.pool import DatabasePool

    pool = DatabasePool(DIRECT_URL, direct_dsn=DIRECT_URL, max_size=2)
    key = _probe_key()
    try:
        with pytest.raises(ValueError):
            with pool.direct() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_lock(%s)", (key,))
                raise ValueError("boom")
        # The connection was closed, so the session lock is gone with it.
        with pool.direct() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                assert cur.fetchone()[0] is True, "lock outlived the failed operation"
    finally:
        pool.close()


def test_a_failed_direct_connect_does_not_strand_a_lock():
    """If the direct endpoint is unreachable the caller fails, and no lock is held."""
    from pact.core.pool import DatabasePool

    pool = DatabasePool(DIRECT_URL, direct_dsn=DIRECT_URL, max_size=2)
    key = _probe_key()
    try:
        # Simulate a connection that dies mid-operation: close it under the context.
        with pytest.raises(Exception):
            with pool.direct() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_lock(%s)", (key,))
                conn.close()  # the session (and its lock) goes away here
                with conn.cursor() as cur:  # any use now fails
                    cur.execute("SELECT 1")
        with pool.direct() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                assert cur.fetchone()[0] is True, "a dead session kept the lock"
    finally:
        pool.close()


# --------------------------------------------------------- statement_timeout


def test_statement_timeout_applies_to_every_transaction_of_a_checkout():
    """The timeout must abort slow statements in *each* unit of work, not just once."""
    import psycopg

    from pact.core.pool import DatabasePool

    pool = DatabasePool(DIRECT_URL, direct_dsn=DIRECT_URL, max_size=1, statement_timeout_ms=1500)
    try:
        for attempt in range(3):
            started = time.time()
            with pytest.raises(psycopg.errors.QueryCanceled):
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("SELECT pg_sleep(3)")
            elapsed = time.time() - started
            assert elapsed < 2.5, f"attempt {attempt} was not aborted: {elapsed:.1f}s"
    finally:
        pool.close()


def test_statement_timeout_does_not_leak_to_other_connections():
    """``SET LOCAL`` must not change the server default for anyone else."""
    import psycopg

    from pact.core.pool import DatabasePool

    pool = DatabasePool(DIRECT_URL, direct_dsn=DIRECT_URL, max_size=1, statement_timeout_ms=1500)
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SHOW statement_timeout")
            assert cur.fetchone()[0] == "1500ms"
        # A brand-new, unpooled connection sees the server default, not 1500ms.
        with psycopg.connect(DIRECT_URL, autocommit=True) as fresh, fresh.cursor() as cur:
            cur.execute("SHOW statement_timeout")
            assert cur.fetchone()[0] in {"0", "0ms"}, "SET LOCAL leaked past its transaction"
    finally:
        pool.close()


def test_a_zero_statement_timeout_still_runs_long_statements():
    """A configured zero must mean "no timeout", as before."""
    from pact.core.pool import DatabasePool

    pool = DatabasePool(DIRECT_URL, direct_dsn=DIRECT_URL, max_size=1, statement_timeout_ms=0)
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_sleep(2)")
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SHOW statement_timeout")
            assert cur.fetchone()[0] == "0"
    finally:
        pool.close()


# ------------------------------------------------- pooled-DSN settlement race

#: The same four-process race as ``test_postgres_store.py``, but the *application*
#: DSN is the pooled endpoint while the session lock must go to the direct one.
POOLED_RACE_SCRIPT = textwrap.dedent(
    """
    import os, sys, time

    from pact.core.dbstore import PostgresJobStore, PostgresReputationLedger
    from pact.core.engine import PactEngine
    from pact.core.llm import MockLLM
    from pact.paypal.escrow import Capture
    from pact.paypal.mock import MockPayPalEscrowClient

    dsn, job_id, log_path, start_at = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])

    class SharedProvider(MockPayPalEscrowClient):
        def capture_authorization(self, authorization_id, **kwargs):
            marker = log_path + ".claimed"
            try:
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
            except FileExistsError:
                with open(log_path, "a") as fh:
                    fh.write("duplicate\\n"); fh.flush(); os.fsync(fh.fileno())
                raise Exception("provider rejected: authorization already captured")
            with open(log_path, "a") as fh:
                fh.write("capture\\n"); fh.flush(); os.fsync(fh.fileno())
            return Capture(id="CAP-SHARED-1", status="COMPLETED",
                           amount="120.00", currency="USD", raw={})

    store = PostgresJobStore(dsn)
    ledger = PostgresReputationLedger(dsn)
    job = store.get(job_id)
    engine = PactEngine(llm=MockLLM(), escrow=SharedProvider(), store=store,
                        reputation_ledger=ledger)
    while time.time() < start_at:
        time.sleep(0.005)
    try:
        engine.settlement.release(job)
        print("RELEASED")
    except Exception as exc:
        print(f"REFUSED: {type(exc).__name__}: {exc}")
    """
)


def _held_job(store, ledger):
    from pact.core.engine import PactEngine
    from pact.core.llm import MockLLM
    from pact.paypal.mock import MockPayPalEscrowClient

    engine = PactEngine(llm=MockLLM(), escrow=MockPayPalEscrowClient(),
                        store=store, reputation_ledger=ledger)
    job = engine.create_job(title="Pooled race", brief="b", criteria=CRITERIA,
                            budget_max=Decimal("100"))
    job.seller_id = "seller_nova"
    job.agreed_price = Decimal("120.00")
    engine.settlement.hold(job)
    store.save(job)
    return job


@pytest.mark.skipif(not POOLED_URL, reason="PGBOUNCER_URL not set; need a transaction-mode pooler")
def test_settlement_race_is_exactly_once_when_the_application_dsn_is_pooled(clean_db, tmp_path):
    """The regression this whole change exists for.

    Four processes race to settle one job. The application DSN is the pooled,
    transaction-mode endpoint, but the settlement lock must be taken on the direct
    endpoint. Exactly one capture must reach the provider.
    """
    from pact.core.models import EscrowState, JobStatus

    store, ledger = clean_db
    job = _held_job(store, ledger)

    log = tmp_path / "provider.log"
    log.write_text("")
    script = tmp_path / "pooled_race.py"
    script.write_text(POOLED_RACE_SCRIPT)

    start_at = time.time() + 2.0
    env = {
        **os.environ,
        "PYTHONPATH": str(BACKEND),
        "DATABASE_URL": POOLED_URL,
        "DATABASE_URL_UNPOOLED": DIRECT_URL,
    }
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), POOLED_URL, job.id, str(log), str(start_at)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(4)
    ]
    outs = [p.communicate(timeout=180)[0].strip() for p in procs]

    lines = log.read_text().splitlines()
    assert lines.count("capture") == 1, (
        f"provider must be captured exactly once with a pooled application DSN, "
        f"got {lines}; output: {outs}"
    )
    assert outs.count("RELEASED") == 1, f"exactly one attempt should succeed; output: {outs}"

    final = store.get(job.id)
    assert final.escrow.state is EscrowState.RELEASED
    assert final.status is JobStatus.SETTLED


@pytest.mark.skipif(not POOLED_URL, reason="PGBOUNCER_URL not set; need a transaction-mode pooler")
def test_the_pooled_endpoint_cannot_hold_a_session_lock():
    """Negative control: prove the pooled endpoint really loses the session lock.

    Without this, the direct endpoint could be ceremonial. Two connections to the
    transaction-mode pooler must *both* be able to take the same session advisory
    lock, which is exactly the loss of exclusion that made the bug reachable.
    """
    import psycopg

    key = _probe_key()
    first = psycopg.connect(POOLED_URL, autocommit=True)
    try:
        with first.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (key,))
        second = psycopg.connect(POOLED_URL, autocommit=True)
        try:
            with second.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                acquired = cur.fetchone()[0]
        finally:
            second.close()
        assert acquired is True, (
            "the pooled endpoint held a session lock across transactions; if this "
            "starts failing, the environment is no longer transaction-mode and this "
            "control no longer proves the direct endpoint is needed"
        )
        with first.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
    finally:
        first.close()
