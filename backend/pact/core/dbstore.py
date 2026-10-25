"""Postgres-backed persistence for deployments without a durable disk.

The default store writes JSON files. That is correct for a single container with a
mounted volume, but wrong for serverless: a Vercel Function has a read-only
filesystem with ephemeral ``/tmp``, so instance-local files are lost on cold start
and invisible to every other instance. A ledger that silently loses settlement
history is worse than no ledger at all.

This module keeps the same interface as :mod:`pact.core.store` and moves the jobs
and the reputation ledger into Postgres. That also upgrades the settlement guards
from thread-safe to instance-safe: ``settlement_claim`` takes a session advisory
lock and re-reads the authoritative row, so two instances cannot both capture the
same authorization.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .models import Job

log = logging.getLogger("pact.dbstore")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                 TEXT PRIMARY KEY,
    created_at         TIMESTAMPTZ NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL,
    status             TEXT NOT NULL,
    escrow_state       TEXT NOT NULL,
    escrow_capture_id  TEXT NOT NULL DEFAULT '',
    doc                JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_created_at_idx ON jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);

CREATE TABLE IF NOT EXISTS reputation (
    agent_id TEXT PRIMARY KEY,
    doc      JSONB NOT NULL
);
"""


def _lock_key(job_id: str) -> int:
    """A stable 64-bit advisory-lock key for a job id.

    Must not use the builtin ``hash``: it is salted per process, so two instances
    would compute different keys for the same job and the lock would silently do
    nothing. A truncated digest is deterministic everywhere. A collision only
    serialises two unrelated jobs, which costs a little concurrency and nothing else.
    """
    digest = hashlib.sha256(job_id.encode()).digest()
    return int.from_bytes(digest[:8], "big") % (2**63)


class PostgresJobStore:
    """Durable, instance-safe job store. Same surface as ``JobStore``."""

    supports_claims = True
    backend = "postgres"
    durable = True

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(SCHEMA)

    # ------------------------------------------------------------ plumbing

    def _connect(self):
        import psycopg  # imported lazily so the default path needs no driver

        return psycopg.connect(self.dsn, autocommit=True)

    @staticmethod
    def _jsonb(value: dict):
        """psycopg3 will not adapt a bare dict; it must be wrapped explicitly."""
        from psycopg.types.json import Jsonb

        return Jsonb(value)

    @staticmethod
    def _row_to_job(doc: Any) -> Job:
        return Job.model_validate(doc)

    def _load(self, conn, job_id: str) -> Job | None:
        with conn.cursor() as cur:
            cur.execute("SELECT doc FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        return self._row_to_job(row[0]) if row else None

    # ------------------------------------------------------------- surface

    def save(self, job: Job) -> None:
        payload = job.model_dump(mode="json")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (id, created_at, updated_at, status, escrow_state,
                                  escrow_capture_id, doc)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    updated_at        = EXCLUDED.updated_at,
                    status            = EXCLUDED.status,
                    escrow_state      = EXCLUDED.escrow_state,
                    escrow_capture_id = EXCLUDED.escrow_capture_id,
                    doc               = EXCLUDED.doc
                """,
                (
                    job.id,
                    job.created_at,
                    job.updated_at,
                    job.status.value,
                    job.escrow.state.value,
                    job.escrow.capture_id,
                    self._jsonb(payload),
                ),
            )

    def get(self, job_id: str) -> Job | None:
        with self._connect() as conn:
            return self._load(conn, job_id)

    def list(self, limit: int = 50) -> list[Job]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT doc FROM jobs ORDER BY created_at DESC LIMIT %s", (limit,)
            )
            return [self._row_to_job(row[0]) for row in cur.fetchall()]

    def delete(self, job_id: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE id = %s RETURNING id", (job_id,))
            return cur.fetchone() is not None

    # ------------------------------------------------- cross-instance guards

    def claim_run(self, job: Job) -> bool:
        """Atomically move a job out of a runnable state. False means someone else won.

        The route and the engine both check the status in Python, but two instances
        can read ``open`` before either writes. This conditional UPDATE is the only
        place the transition is decided, so it cannot be lost to a race.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs SET status = 'negotiating', updated_at = now()
                WHERE id = %s AND status = ANY(%s)
                RETURNING id
                """,
                (job.id, ["open", "failed"]),
            )
            return cur.fetchone() is not None

    @contextmanager
    def settlement_claim(self, job: Job) -> Iterator[Job | None]:
        """Serialise settlement for one job across every instance.

        Holds a session advisory lock and re-reads the row inside it, so a second
        instance sees the capture the first one recorded rather than its own stale
        copy. Without this, ``_settle_lock`` only protects a single process and a
        duplicate capture is reachable.
        """
        conn = self._connect()
        key = _lock_key(job.id)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (key,))
            yield self._load(conn, job.id)
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            except Exception as exc:  # never mask the caller's error
                log.warning("Could not release settlement lock for %s: %s", job.id, exc)
            finally:
                conn.close()


class PostgresReputationLedger:
    """Durable reputation, same shape and scoring as the filesystem ledger."""

    supports_claims = False
    backend = "postgres"
    durable = True

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(SCHEMA)

    def _connect(self):
        import psycopg

        return psycopg.connect(self.dsn, autocommit=True)

    @staticmethod
    def _jsonb(value: dict):
        from psycopg.types.json import Jsonb

        return Jsonb(value)

    def _read(self, conn) -> dict[str, dict]:
        with conn.cursor() as cur:
            cur.execute("SELECT doc FROM reputation")
            return {row[0]["agent_id"]: row[0] for row in cur.fetchall()}

    def _write(self, conn, record: dict) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reputation (agent_id, doc) VALUES (%s, %s)
                ON CONFLICT (agent_id) DO UPDATE SET doc = EXCLUDED.doc
                """,
                (record["agent_id"], self._jsonb(record)),
            )

    @staticmethod
    def _blank(agent_id: str) -> dict:
        return {
            "agent_id": agent_id,
            "settled": 0,
            "voided": 0,
            "failed": 0,
            "volume": 0.0,
            "last_settled_at": None,
        }

    def _mutate(self, agent_id: str, apply) -> dict:
        """Read-modify-write one agent under a row lock.

        Concurrent settlements for the same agent must not lose an increment, so
        the row is selected FOR UPDATE and the counters are recomputed in Python
        from the locked copy.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT doc FROM reputation WHERE agent_id = %s FOR UPDATE",
                    (agent_id,),
                )
                row = cur.fetchone()
            record = row[0] if row else self._blank(agent_id)
            apply(record)
            self._write(conn, record)
            return dict(record)

    def record_settled(self, agent_id: str, amount: float) -> dict:
        def apply(record: dict) -> None:
            record["settled"] += 1
            record["volume"] = round(record["volume"] + float(amount), 2)
            record["last_settled_at"] = datetime.now(timezone.utc).isoformat()

        return self._mutate(agent_id, apply)

    def record_voided(self, agent_id: str) -> dict:
        return self._mutate(agent_id, lambda r: r.__setitem__("voided", r["voided"] + 1))

    def record_failed(self, agent_id: str) -> dict:
        return self._mutate(agent_id, lambda r: r.__setitem__("failed", r["failed"] + 1))

    @staticmethod
    def score(record: dict) -> float:
        settled = record.get("settled", 0)
        voided = record.get("voided", 0)
        failures = voided + record.get("failed", 0)
        return round((settled + 1) / (settled + failures + 2), 3)

    def all(self) -> list[dict]:
        with self._connect() as conn:
            records = []
            for record in self._read(conn).values():
                entry = dict(record)
                entry["score"] = self.score(record)
                records.append(entry)
            return sorted(records, key=lambda r: (-r["score"], -r["settled"]))

    def get(self, agent_id: str) -> dict:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT doc FROM reputation WHERE agent_id = %s", (agent_id,))
            row = cur.fetchone()
        record = dict(row[0]) if row else self._blank(agent_id)
        record["score"] = self.score(record)
        return record
