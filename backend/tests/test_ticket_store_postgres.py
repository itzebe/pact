"""Shared SSE-ticket consumption on real PostgreSQL.

The process-local registry is correct for one process and wrong for a deployment
that runs several: a replay that lands on another instance finds an empty map and
is accepted. These tests prove the shared registry closes that hole. They are
skipped unless ``DATABASE_URL`` points at a reachable Postgres, because the whole
property under test is *cross-instance*, which an in-memory double cannot model.

Run with:
    DATABASE_URL=postgresql://... pytest tests/test_ticket_store_postgres.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pact.api.auth import issue_token
from pact.core.ticket_store import (
    InMemoryTicketStore,
    PostgresTicketStore,
    UnavailableTicketStore,
)

DATABASE_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set; Postgres tests are opt-in"
)

BACKEND = Path(__file__).resolve().parents[1]

BUYER = {"Authorization": f"Bearer {issue_token('buyer', 'buyer')}"}
BODY = {
    "title": "Landing page copy",
    "brief": "Write hero and feature copy for a launch page.",
    "acceptance_criteria": ["Hero under 12 words", "Three feature blocks", "Clear call to action"],
    "budget_max": 300,
}


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    from pact.api import routes
    from pact.core.engine import PactEngine
    from pact.core.llm import MockLLM
    from pact.core.store import JobStore, ReputationLedger
    from pact.main import app
    from pact.paypal import MockPayPalEscrowClient

    engine = PactEngine(
        llm=MockLLM(),
        escrow=MockPayPalEscrowClient(),
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
    )
    monkeypatch.setattr(routes, "engine", engine)
    return TestClient(app)


def _create(client: TestClient) -> str:
    response = client.post("/api/jobs", json=BODY, headers=BUYER)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _truncate() -> None:
    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        # Create the table first so TRUNCATE is valid on a brand-new database.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS consumed_tickets ("
            "jti TEXT PRIMARY KEY, expires_at TIMESTAMPTZ NOT NULL)"
        )
        cur.execute("TRUNCATE consumed_tickets")


@pytest.fixture()
def ticket_store() -> PostgresTicketStore:
    store = PostgresTicketStore(DATABASE_URL)
    _truncate()
    return store


# ------------------------------------------------------------ single redemption


def test_the_first_redemption_succeeds(ticket_store):
    assert ticket_store.consume("jti-first", 60) is True


def test_a_second_redemption_of_the_same_ticket_fails(ticket_store):
    assert ticket_store.consume("jti-once", 60) is True
    assert ticket_store.consume("jti-once", 60) is False, "a consumed ticket must be refused"


def test_a_new_store_instance_also_refuses_a_consumed_ticket(ticket_store):
    """The decision lives in the database, not in the object that made it."""
    assert ticket_store.consume("jti-shared", 60) is True
    # A completely independent store, as a second API instance would build.
    other = PostgresTicketStore(DATABASE_URL)
    assert other.consume("jti-shared", 60) is False


def test_distinct_tickets_do_not_collide(ticket_store):
    assert ticket_store.consume("jti-a", 60) is True
    assert ticket_store.consume("jti-b", 60) is True


# ------------------------------------------------------- cross-instance races


def test_two_independent_store_instances_allow_exactly_one_success(ticket_store):
    """Two API instances race for the same ticket; exactly one may win."""
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        local = PostgresTicketStore(DATABASE_URL)  # a separate instance's store
        barrier.wait(timeout=10)
        results.append(local.consume("jti-two-way", 60))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert results.count(True) == 1, f"exactly one instance must win, got {results}"


def test_many_independent_instances_allow_exactly_one_success(ticket_store):
    """A burst of instances, each with its own store, racing one ticket."""
    workers = 12
    results: list[bool] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(workers)

    def attempt() -> None:
        local = PostgresTicketStore(DATABASE_URL)
        try:
            barrier.wait(timeout=30)
            results.append(local.consume("jti-burst", 60))
        except Exception as exc:  # recorded for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"concurrent consumption failed: {errors}"
    assert results.count(True) == 1, (
        f"exactly one instance must win across {workers} racers, got {results}"
    )


# The multi-process race: separate OS processes cannot share any Python state, so
# this is the only test that models two *real* instances behind a load balancer.
TICKET_RACE_SCRIPT = textwrap.dedent(
    """
    import sys, time

    from pact.core.ticket_store import PostgresTicketStore

    dsn, jti, start_at = sys.argv[1], sys.argv[2], float(sys.argv[3])
    store = PostgresTicketStore(dsn)

    while time.time() < start_at:
        time.sleep(0.002)

    print("WON" if store.consume(jti, 60) else "REFUSED")
    """
)


def test_two_processes_cannot_both_consume_one_ticket(ticket_store, tmp_path):
    """Four OS processes race for one ticket. Exactly one is admitted.

    This is the failure an in-process registry cannot prevent: on a platform that
    runs several instances, a ``threading.Lock`` is not a guard at all. Safety
    comes from the database primary key making the insert-and-consume one atomic
    statement.
    """
    script = tmp_path / "ticket_race.py"
    script.write_text(TICKET_RACE_SCRIPT)

    start_at = time.time() + 2.0
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "DATABASE_URL": DATABASE_URL}
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), DATABASE_URL, "jti-multiproc", str(start_at)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(4)
    ]
    outs = [p.communicate(timeout=120)[0].strip() for p in procs]

    assert outs.count("WON") == 1, (
        f"exactly one process may consume the ticket, got {outs}"
    )
    assert outs.count("REFUSED") == 3, f"the other three must be refused, got {outs}"


# --------------------------------------------------------------- expiry & cleanup


def test_an_expired_record_is_reclaimed_so_the_table_stays_bounded(ticket_store):
    """A consumed id must be discardable once its window closes, or the table grows forever."""
    # ttl 0: the record is expired the instant it is written, so the opportunistic
    # prune that follows the consume already removes it.
    assert ticket_store.consume("jti-old", 0) is True
    time.sleep(0.05)
    assert ticket_store.size() == 0, "an expired record must be reclaimed automatically"

    # And an explicit prune also removes expired rows, in a bounded batch.
    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO consumed_tickets (jti, expires_at) "
            "VALUES ('jti-old-2', now() - interval '1 hour')"
        )
    assert ticket_store.size() == 1
    assert ticket_store.prune() == 1
    assert ticket_store.size() == 0


def test_a_live_record_is_never_pruned(ticket_store):
    """Cleanup must not free a ticket that is still within its lifetime."""
    assert ticket_store.consume("jti-live", 600) is True
    ticket_store.prune()
    assert ticket_store.size() == 1
    assert ticket_store.consume("jti-live", 600) is False, "a live ticket stays consumed"


def test_cleanup_removes_at_most_one_batch(ticket_store):
    """Pruning is bounded per call, so it cannot delete an unbounded number of rows."""
    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO consumed_tickets (jti, expires_at) "
            "VALUES (%s, now() - interval '1 hour')",
            [(f"expired-{i}",) for i in range(50)],
        )
    removed = ticket_store.prune(limit=10)
    assert removed == 10, f"a bounded prune must remove exactly the limit, got {removed}"
    assert ticket_store.size() == 40
    # Repeated calls drain the rest.
    ticket_store.prune(limit=1000)
    assert ticket_store.size() == 0


def test_an_expired_ticket_cannot_open_a_stream(client, monkeypatch):
    """End to end: a ticket past its ``exp`` is refused even with a shared store."""
    import jwt as pyjwt

    from pact.api import auth as auth_module
    from pact.api.auth import STREAM_TICKET_TTL, stream_scope
    from pact.config import settings as live_settings

    monkeypatch.setattr(auth_module, "ticket_store", PostgresTicketStore(DATABASE_URL))
    job_id = _create(client)
    issued = datetime.now(timezone.utc) - timedelta(seconds=120)
    expired = pyjwt.encode(
        {
            "sub": "buyer", "role": "buyer", "scope": stream_scope(job_id),
            "jti": "jti-expired-api", "iat": int(issued.timestamp()),
            "exp": int((issued + STREAM_TICKET_TTL).timestamp()),
        },
        live_settings.auth.secret,
        algorithm="HS256",
    )
    assert client.get(f"/api/jobs/{job_id}/stream?ticket={expired}").status_code == 401


def test_the_shared_store_admits_one_redemption_then_refuses_the_next(client, monkeypatch):
    """The HTTP path, backed by Postgres, is single-use across independent instances."""
    from pact.api import auth as auth_module

    monkeypatch.setattr(auth_module, "ticket_store", PostgresTicketStore(DATABASE_URL))
    job_id = _create(client)
    ticket = client.post(f"/api/jobs/{job_id}/stream-ticket", headers=BUYER).json()["ticket"]

    assert client.get(f"/api/jobs/{job_id}/stream?ticket={ticket}").status_code == 200

    # A second API instance, with its own store, must refuse the same ticket.
    monkeypatch.setattr(auth_module, "ticket_store", PostgresTicketStore(DATABASE_URL))
    assert client.get(f"/api/jobs/{job_id}/stream?ticket={ticket}").status_code == 401


# --------------------------------------------------------------- fail closed


def test_an_unreachable_database_refuses_every_ticket():
    """A configured-but-unreachable shared store must fail closed, never fall back."""
    store = PostgresTicketStore.__new__(PostgresTicketStore)
    store.dsn = "postgresql://nobody:nobody@127.0.0.1:1/absent"
    assert store.consume("jti-any", 60) is False


def test_selecting_a_shared_store_that_cannot_be_opened_fails_closed(monkeypatch):
    """``_select_ticket_store`` must return a refusing store, not a process-local one."""
    import dataclasses

    from pact.core import ticket_store as module

    monkeypatch.setattr(
        module, "settings",
        dataclasses.replace(module.settings, database_url="postgresql://nobody@127.0.0.1:1/absent"),
    )
    selected = module._select_ticket_store()
    assert isinstance(selected, UnavailableTicketStore)
    assert selected.consume("jti-any", 60) is False


def test_without_a_database_url_the_registry_is_process_local(monkeypatch):
    """Single-instance deployments keep the in-memory registry, by design."""
    import dataclasses

    from pact.core import ticket_store as module

    monkeypatch.setattr(
        module, "settings",
        dataclasses.replace(module.settings, database_url=""),
    )
    assert isinstance(module._select_ticket_store(), InMemoryTicketStore)


# ------------------------------------------------------------------- plumbing


def test_the_table_and_index_are_created_on_a_brand_new_database():
    """Opening the store must create what it needs, on any database."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(DATABASE_URL)
    name = f"pact_tickets_{int(time.time() * 1000)}"
    admin = make_conninfo(**{**params, "dbname": "postgres"})
    fresh = make_conninfo(**{**params, "dbname": name})
    with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    try:
        store = PostgresTicketStore(fresh)  # must create the schema itself
        with psycopg.connect(fresh) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'consumed_tickets'"
            )
            indexes = {row[0] for row in cur.fetchall()}
        assert "consumed_tickets_expires_at_idx" in indexes, indexes
        assert store.consume("jti-new-db", 60) is True
    finally:
        with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_a_ticket_value_is_never_written_to_the_log(ticket_store, caplog):
    """A ticket id is a credential for its lifetime; it must not appear in logs."""
    import logging

    with caplog.at_level(logging.DEBUG):
        ticket_store.consume("super-secret-jti-value", 60)
    assert "super-secret-jti-value" not in caplog.text
