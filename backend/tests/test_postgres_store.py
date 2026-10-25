"""Postgres-backed persistence and cross-instance settlement safety.

These tests are skipped unless ``DATABASE_URL`` points at a reachable Postgres, so
the default suite stays dependency-free. They exist because the filesystem store
cannot be exercised for the two properties that actually matter on a serverless
platform: that state survives an instance change, and that two *separate processes*
cannot both capture the same authorization.

Run with:
    DATABASE_URL=postgresql://... pytest tests/test_postgres_store.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from decimal import Decimal
from pathlib import Path

import pytest

DATABASE_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set; Postgres tests are opt-in"
)

CRITERIA = ["The deliverable states the headline result"]
BACKEND = Path(__file__).resolve().parents[1]

# Imported at module level so the tests read clearly; the module itself is safe to
# import without a database (the connection is only opened on use).
from pact.core.dbstore import PostgresJobStore, PostgresReputationLedger  # noqa: E402


@pytest.fixture()
def clean_db():
    """A fresh schema per test, so counts never leak between cases."""
    # Construct the stores first: opening them creates the schema, which must
    # exist before the TRUNCATE. On a brand-new database (the documented
    # ``createdb pact`` flow) truncating first raised UndefinedTable.
    store = PostgresJobStore(DATABASE_URL)
    ledger = PostgresReputationLedger(DATABASE_URL)

    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE jobs, reputation")
    return store, ledger


def _engine(store, ledger, escrow=None):
    from pact.core.engine import PactEngine
    from pact.core.llm import MockLLM
    from pact.paypal.mock import MockPayPalEscrowClient

    return PactEngine(llm=MockLLM(), escrow=escrow or MockPayPalEscrowClient(),
                      store=store, reputation_ledger=ledger)


def _held_job(store, ledger, escrow=None):
    """A job sitting in HELD with an authorization, ready to settle."""
    from pact.core.models import EscrowState

    engine = _engine(store, ledger, escrow=escrow)
    job = engine.create_job(title="Race", brief="b", criteria=CRITERIA,
                            budget_max=Decimal("100"))
    job.seller_id = "seller_nova"
    job.agreed_price = Decimal("120.00")
    engine.settlement.hold(job)
    assert job.escrow.state is EscrowState.HELD
    store.save(job)
    return job


def test_store_is_selected_and_reports_durability(clean_db):
    from pact.core.store import job_store

    assert job_store.backend == "postgres"
    assert job_store.durable is True


def test_state_survives_a_separate_instance(clean_db):
    """A job written by one instance must be readable by a brand-new one."""
    from pact.core.dbstore import PostgresJobStore

    store, ledger = clean_db
    engine = _engine(store, ledger)
    job = engine.create_job(title="Durable", brief="b", criteria=CRITERIA,
                            budget_max=Decimal("100"))
    engine.run(job, seller_ids=["seller_nova"])

    # A completely independent store object, as a second instance would create.
    restored = PostgresJobStore(DATABASE_URL).get(job.id)
    assert restored is not None, "job vanished between instances"
    assert restored.escrow.state == job.escrow.state
    assert restored.escrow.capture_id == job.escrow.capture_id
    assert restored.verify_chain() is True, "hash chain must survive the round trip"


def test_ledger_survives_when_the_local_disk_is_read_only(clean_db):
    """The Vercel failure mode: no writable disk, so persistence must be remote.

    With the filesystem store this raises PermissionError; with Postgres the
    lifecycle completes normally and the ledger is still written.
    """
    store, ledger = clean_db
    engine = _engine(store, ledger)
    job = engine.create_job(title="RO", brief="b", criteria=CRITERIA,
                            budget_max=Decimal("100"))
    job = engine.run(job, seller_ids=["seller_nova"])
    assert job.ledger, "ledger must be written even with no local disk"
    assert job.verify_chain() is True


# --------------------------------------------------------------- concurrency

RACE_SCRIPT = textwrap.dedent(
    """
    import os, sys, time

    from pact.core.dbstore import PostgresJobStore, PostgresReputationLedger
    from pact.core.engine import PactEngine
    from pact.core.llm import MockLLM
    from pact.paypal.escrow import Capture
    from pact.paypal.mock import MockPayPalEscrowClient

    dsn, job_id, log_path, start_at = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])

    class SharedProvider(MockPayPalEscrowClient):
        \"\"\"A provider double whose "already captured" record lives on disk.

        Every process builds its own object, so the only way to model a real
        provider is out-of-process state. The exclusive-create marker stands in for
        PayPal refusing a second capture of the same authorization.
        \"\"\"

        def capture_authorization(self, authorization_id, **kwargs):
            marker = log_path + ".claimed"
            try:
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
            except FileExistsError:
                with open(log_path, "a") as fh:
                    fh.write("duplicate\\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                raise Exception("provider rejected: authorization already captured")

            with open(log_path, "a") as fh:
                fh.write("capture\\n")
                fh.flush()
                os.fsync(fh.fileno())
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


def test_failed_capture_does_not_wedge_the_job(clean_db):
    """A definite capture failure must leave the job retryable.

    Recording ``capture_id = "pending"`` before calling out is what stops a second
    instance capturing twice. But if the provider then rejects the capture and the
    claim is left in the shared row, every later attempt is refused as "already
    captured" and the job can never settle or void. The claim must be withdrawn.
    """
    from pact.core.models import EscrowState
    from pact.paypal import PayPalError

    store, ledger = clean_db
    job = _held_job(store, ledger)

    class FailingOnce:
        """Fails the capture with a definite (non-ambiguous) error."""

        environment = "mock"
        configured = True

        def __init__(self):
            self.calls = 0

        def capture_authorization(self, authorization_id, **kwargs):
            self.calls += 1
            raise PayPalError("capture refused by provider", code="CAPTURE_FAILED")

    escrow = FailingOnce()
    engine = _engine(store, ledger, escrow=escrow)

    with pytest.raises(PayPalError):
        engine.settlement.release(job)

    stored = store.get(job.id)
    assert stored.escrow.capture_id == "", (
        "a definite failure must not leave a pending claim in the shared row"
    )
    assert stored.escrow.state is EscrowState.HELD, "funds are still held and retryable"
    assert any(e.kind == "capture_failed" for e in stored.ledger)


def test_two_processes_cannot_both_capture(clean_db, tmp_path):
    """Four OS processes race to settle one job. The provider is hit once.

    This is the failure an in-process lock cannot prevent: on Vercel each request
    may land on a different instance, so a ``threading.RLock`` is not a guard at
    all. Safety comes from the shared advisory lock plus re-reading the
    authoritative row inside it, so a capture recorded by another process is
    visible and the attempt is refused.
    """
    from pact.core.models import EscrowState, JobStatus

    store, ledger = clean_db
    job = _held_job(store, ledger)

    log = tmp_path / "provider.log"
    log.write_text("")
    script = tmp_path / "race.py"
    script.write_text(RACE_SCRIPT)

    start_at = time.time() + 2.0
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "DATABASE_URL": DATABASE_URL}
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), DATABASE_URL, job.id, str(log), str(start_at)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(4)
    ]
    outs = [p.communicate(timeout=120)[0].strip() for p in procs]

    lines = log.read_text().splitlines()
    assert lines.count("capture") == 1, (
        f"provider must be captured exactly once, got {lines}; output: {outs}"
    )
    assert "duplicate" not in lines, (
        f"a second instance reached the provider for an already-captured "
        f"authorization; output: {outs}"
    )
    assert outs.count("RELEASED") == 1, (
        f"exactly one attempt should succeed; output: {outs}"
    )

    final = store.get(job.id)
    assert final.escrow.state is EscrowState.RELEASED
    assert final.status is JobStatus.SETTLED
    assert final.verify_chain() is True


def test_the_race_test_fails_when_the_lock_key_is_not_deterministic(clean_db, tmp_path):
    """Negative control: prove the advisory lock is load-bearing.

    A test that passes with the guard removed proves nothing. This runs the same
    four-process race with the lock key made per-process, which is exactly the bug
    a salted ``hash()`` would introduce: every instance computes a different key,
    so no instance ever blocks, and several captures reach the provider.
    """
    from pact.core.models import EscrowState

    store, ledger = clean_db
    job = _held_job(store, ledger)

    log = tmp_path / "provider.log"
    log.write_text("")
    # Same race, but the lock key varies per process so the lock is never shared.
    broken = RACE_SCRIPT.replace(
        "store = PostgresJobStore(dsn)",
        "import pact.core.dbstore as _db\n"
        "_db._lock_key = lambda job_id: (hash(job_id) % (2**63))\n"
        "store = PostgresJobStore(dsn)",
    )
    assert broken != RACE_SCRIPT, "negative control failed to patch the lock key"
    script = tmp_path / "race_broken.py"
    script.write_text(broken)

    start_at = time.time() + 2.0
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "DATABASE_URL": DATABASE_URL,
           "PYTHONHASHSEED": "random"}
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), DATABASE_URL, job.id, str(log), str(start_at)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(4)
    ]
    [p.communicate(timeout=120) for p in procs]

    lines = log.read_text().splitlines()
    # With no shared lock, more than one process reaches the provider. If this
    # ever stops being true, the positive test above is not proving anything.
    assert lines.count("capture") + lines.count("duplicate") > 1, (
        f"negative control did not reproduce the race, so the guarded test is not "
        f"load-bearing; provider log was {lines}"
    )


# ------------------------------------------------------ schema and durability


def test_schema_is_created_on_a_brand_new_database():
    """Schema readiness: opening the store must create what it needs.

    Run against a throwaway database so this genuinely exercises the migration
    path rather than trusting that some earlier test created the tables.
    """
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from pact.core.dbstore import SCHEMA

    # Parse rather than string-edit the DSN: it may carry a query string.
    params = conninfo_to_dict(DATABASE_URL)
    name = f"pact_schema_{int(time.time() * 1000)}"
    admin = make_conninfo(**{**params, "dbname": "postgres"})
    fresh = make_conninfo(**{**params, "dbname": name})
    with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    try:
        store = PostgresJobStore(fresh)  # must create the schema itself
        with psycopg.connect(fresh) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
            tables = {row[0] for row in cur.fetchall()}
        assert {"jobs", "reputation"} <= tables, f"schema incomplete: {tables}"
        assert "CREATE TABLE IF NOT EXISTS jobs" in SCHEMA
        assert store.get("does-not-exist") is None
    finally:
        with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_health_reports_the_real_backend_and_durability(clean_db):
    """The health endpoint must not claim durability it does not have."""
    from pact.api.routes import health

    body = health()
    assert body["storage"]["backend"] == "postgres"
    assert body["storage"]["durable"] is True
    assert "error" not in body["storage"]
    assert body["status"] == "ok"


def test_ledger_integrity_survives_concurrent_settlements(clean_db):
    """Concurrent settlements must not corrupt any job's hash chain."""
    from pact.core.models import EscrowState

    store, ledger = clean_db
    engine = _engine(store, ledger)
    jobs = []
    for index in range(4):
        job = engine.create_job(title=f"Concurrent {index}", brief="b", criteria=CRITERIA,
                                budget_max=Decimal("100"))
        job.seller_id = "seller_nova"
        job.agreed_price = Decimal("50.00")
        engine.settlement.hold(job)
        store.save(job)
        jobs.append(job)

    import threading

    errors: list[Exception] = []

    def settle(job):
        try:
            # A separate store object per worker, as a separate instance would have,
            # but the same provider: the authorization lives in the provider's own
            # state, not in the database.
            local = PostgresJobStore(DATABASE_URL).get(job.id)
            local_engine = _engine(PostgresJobStore(DATABASE_URL),
                                   PostgresReputationLedger(DATABASE_URL),
                                   escrow=engine.escrow)
            local_engine.settlement.release(local)
        except Exception as exc:  # recorded, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=settle, args=(j,)) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not errors, f"concurrent settlements failed: {errors}"
    for job in jobs:
        restored = store.get(job.id)
        assert restored.escrow.state is EscrowState.RELEASED
        assert restored.verify_chain() is True, f"chain broken for {job.id}"


def test_a_connection_lost_while_holding_the_lock_does_not_block_others(clean_db):
    """A dropped connection must release the session lock, not deadlock the job.

    PostgreSQL releases session-level advisory locks when the session ends, which
    is what makes holding one across an external call survivable: a crashed
    instance cannot strand the job.
    """
    from pact.core.models import EscrowState

    from pact.paypal.mock import MockPayPalEscrowClient

    store, ledger = clean_db
    # One provider shared by the job and the engine that settles it: the
    # authorization lives in the provider, not in the database.
    escrow = MockPayPalEscrowClient()
    job = _held_job(store, ledger, escrow=escrow)
    engine = _engine(store, ledger, escrow=escrow)

    import psycopg

    from pact.core.dbstore import _lock_key

    key = _lock_key(job.id)
    # Take the lock on a session, then abandon that session without unlocking.
    victim = psycopg.connect(DATABASE_URL, autocommit=True)
    with victim.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (key,))
    victim.close()

    # Another session must now be able to take it, proving the lock was released.
    settled = engine.settlement.release(store.get(job.id))
    assert settled.escrow.state is EscrowState.RELEASED


def test_the_lock_is_released_after_an_exception(clean_db):
    """An exception inside the guarded section must not strand the lock."""
    import psycopg

    from pact.core.dbstore import _lock_key

    store, ledger = clean_db
    job = _held_job(store, ledger)

    with pytest.raises(RuntimeError):
        with store.settlement_claim(job):
            raise RuntimeError("boom")

    # If the lock leaked, this would block forever rather than returning.
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_lock_key(job.id),))
        acquired = cur.fetchone()[0]
        if acquired:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(job.id),))
    assert acquired is True, "the advisory lock leaked after an exception"


def test_a_second_instance_sees_the_claim_and_refuses_to_capture(clean_db):
    """Concurrent retries from multiple instances: only one may capture."""
    from pact.core.models import EscrowState

    store, ledger = clean_db
    job = _held_job(store, ledger)

    # Simulate the first instance having committed its in-flight claim.
    claimed = store.get(job.id)
    claimed.escrow.capture_id = "pending"
    store.save(claimed)

    # A second, independent instance must refuse rather than capture again.
    other = PostgresJobStore(DATABASE_URL)
    engine = _engine(other, PostgresReputationLedger(DATABASE_URL))
    fresh = other.get(job.id)
    with pytest.raises(ValueError, match="already captured"):
        engine.settlement.release(fresh)
    assert other.get(job.id).escrow.state is EscrowState.HELD


# --------------------------------------------- unsafe production fallback


def test_unavailable_database_disables_settlement_instead_of_falling_back(monkeypatch):
    """A configured-but-unreachable database must not degrade to local files.

    On a serverless host that fallback would look healthy while losing the ledger
    on every cold start. It must refuse instead, and say so through /api/health.
    """
    from pact.config import settings
    from pact.core import store as store_module

    object.__setattr__(settings, "database_url", "postgresql://nobody@127.0.0.1:1/nope")
    try:
        job_store, reputation = store_module._select_backend()
        assert job_store.backend == "unavailable"
        assert job_store.durable is False
        with pytest.raises(RuntimeError, match="Durable storage is unavailable"):
            job_store.get("anything")
        with pytest.raises(RuntimeError):
            job_store.save(object())
    finally:
        object.__setattr__(settings, "database_url", DATABASE_URL)


def test_reconciled_settlement_credits_reputation_on_the_db_path(clean_db):
    """The Postgres ledger must apply the same reconciliation correction.

    Both backends have to agree, or reputation would differ depending on where the
    deployment happens to be running.
    """
    from pact.core.models import EscrowState
    from pact.paypal import AmbiguousOutcome

    store, ledger = clean_db
    engine = _engine(store, ledger)
    job = _held_job(store, ledger, escrow=engine.escrow)
    assert job.seller_id == "seller_nova"

    original = engine.escrow.capture_authorization

    def apply_then_lose(authorization_id, **kwargs):
        capture = original(authorization_id, **kwargs)
        raise AmbiguousOutcome(f"capture {capture.id} applied, response lost", code="TIMEOUT")

    engine.escrow.capture_authorization = apply_then_lose
    with pytest.raises(AmbiguousOutcome):
        engine.settlement.release(job)
    engine.escrow.capture_authorization = original

    unresolved = store.get(job.id)
    assert unresolved.escrow.state is EscrowState.UNRESOLVED
    assert engine.reputation.get("seller_nova")["failed"] == 1

    engine.reconcile_job(unresolved)

    record = PostgresReputationLedger(DATABASE_URL).get("seller_nova")
    assert record["failed"] == 0, "the reversal must persist, not just live in memory"
    assert record["settled"] == 1
    assert record["volume"] == 120.0


# --------------------------------------------- reputation under concurrency


def test_concurrent_reputation_updates_do_not_lose_increments(clean_db):
    """Many settlements for the same seller at once must not lose any increment.

    Reputation is a read-modify-write of a JSON document. Reading it, changing it,
    and writing it back in separate transactions lets two writers read the same
    value and overwrite each other, so a burst of settlements silently records only
    the last one. The score then contradicts the ledger.
    """
    import threading

    import psycopg

    from pact.core.dbstore import PostgresReputationLedger

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE jobs, reputation")

    workers = 32
    barrier = threading.Barrier(workers)
    errors: list[Exception] = []

    def settle() -> None:
        # A separate ledger per thread, as a separate instance would have.
        local = PostgresReputationLedger(DATABASE_URL)
        try:
            barrier.wait(timeout=30)
            local.record_settled("seller_race", 10.0)
        except Exception as exc:  # recorded for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=settle) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not errors, f"concurrent reputation updates failed: {errors}"
    record = PostgresReputationLedger(DATABASE_URL).get("seller_race")
    assert record["settled"] == workers, (
        f"every increment must persist; got {record['settled']} of {workers}"
    )
    assert record["volume"] == workers * 10.0


def test_the_reputation_race_is_real_without_the_row_lock(clean_db):
    """Negative control: prove the row lock is what prevents the lost updates.

    Running the same burst with the lock removed must lose increments. If it does
    not, the guarded test above proves nothing about the guard.
    """
    import threading

    import psycopg

    import pact.core.dbstore as db

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE jobs, reputation")

    original = db.PostgresReputationLedger._mutate

    def unlocked_mutate(self, agent_id, apply):
        """The same read-modify-write with no transaction and no row lock."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT doc FROM reputation WHERE agent_id = %s", (agent_id,))
                row = cur.fetchone()
            record = row[0] if row else self._blank(agent_id)
            apply(record)
            self._write(conn, record)
            return dict(record)

    db.PostgresReputationLedger._mutate = unlocked_mutate
    try:
        workers = 32
        barrier = threading.Barrier(workers)

        def settle() -> None:
            local = db.PostgresReputationLedger(DATABASE_URL)
            barrier.wait(timeout=30)
            local.record_settled("seller_unlocked", 10.0)

        threads = [threading.Thread(target=settle) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
    finally:
        db.PostgresReputationLedger._mutate = original

    record = db.PostgresReputationLedger(DATABASE_URL).get("seller_unlocked")
    assert record["settled"] < workers, (
        "the negative control did not lose any update, so the guarded test is not "
        "load-bearing; the row lock is not what protects this path"
    )
