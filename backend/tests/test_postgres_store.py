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


@pytest.fixture()
def clean_db():
    """A fresh schema per test, so counts never leak between cases."""
    import psycopg

    from pact.core.dbstore import PostgresJobStore, PostgresReputationLedger

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE jobs, reputation")
    return PostgresJobStore(DATABASE_URL), PostgresReputationLedger(DATABASE_URL)


def _engine(store, ledger, escrow=None):
    from pact.core.engine import PactEngine
    from pact.core.llm import MockLLM
    from pact.paypal.mock import MockPayPalEscrowClient

    return PactEngine(llm=MockLLM(), escrow=escrow or MockPayPalEscrowClient(),
                      store=store, reputation_ledger=ledger)


def _held_job(store, ledger):
    """A job sitting in HELD with an authorization, ready to settle."""
    from pact.core.models import EscrowState

    engine = _engine(store, ledger)
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
