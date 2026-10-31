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
import json
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

CREATE TABLE IF NOT EXISTS reputation (
    agent_id TEXT PRIMARY KEY,
    doc      JSONB NOT NULL
);
"""

# Columns and indexes added after the first release. ``CREATE TABLE IF NOT EXISTS``
# does not alter an existing table, so an already-deployed database needs these
# explicitly. They run *before* any statement that references the new columns:
# SCHEMA intentionally creates only the original columns, because a table-wide
# index on a column that an older database does not yet have would fail at startup
# before the migration that adds it could run. Each statement is idempotent, so
# this is safe on every boot.
MIGRATIONS = (
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS buyer_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS run_lease_expires_at TIMESTAMPTZ",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS approval_claimed_at TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS jobs_created_at_idx ON jobs (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status)",
    "CREATE INDEX IF NOT EXISTS jobs_buyer_id_idx ON jobs (buyer_id)",
    # Supports the seller filter in ``list``: a containment test over the offers
    # array (jsonb_path_ops) and an equality test on the winning seller. There is no
    # seller_id column — the winner lives in the document.
    "CREATE INDEX IF NOT EXISTS jobs_doc_gin_idx ON jobs USING gin (doc jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS jobs_seller_id_idx ON jobs ((doc ->> 'seller_id'))",
)

# Statuses a run may start from, and the statuses a crashed run may be reclaimed
# from once its lease has expired. Kept in SQL form so the reclaim is decided by
# one conditional UPDATE rather than a read-then-write race.
_RUNNABLE_STATUSES = ["open", "failed"]
_IN_FLIGHT_STATUSES = [
    "negotiating",
    "agreed",
    "escrowed",
    "delivered",
    "verifying",
    "awaiting_approval",
]


def _lock_key(job_id: str) -> int:
    """A stable 64-bit advisory-lock key for a job id.

    Must not use the builtin ``hash``: it is salted per process, so two instances
    would compute different keys for the same job and the lock would silently do
    nothing. A truncated digest is deterministic everywhere. A collision only
    serialises two unrelated jobs, which costs a little concurrency and nothing else.
    """
    digest = hashlib.sha256(job_id.encode()).digest()
    return int.from_bytes(digest[:8], "big") % (2**63)


def _driver():
    """Import psycopg with a message that says what to do about it.

    The import is lazy so the default filesystem path needs no driver installed.
    """
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "DATABASE_URL is set but the Postgres driver is not installed. "
            "Install it with `pip install 'psycopg[binary]'` (or `uv sync`)."
        ) from exc
    return psycopg


class PostgresJobStore:
    """Durable, instance-safe job store. Same surface as ``JobStore``."""

    supports_claims = True
    backend = "postgres"
    durable = True

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(SCHEMA)
            for statement in MIGRATIONS:
                cur.execute(statement)

    # ------------------------------------------------------------ plumbing

    def _connect(self):
        return _driver().connect(self.dsn, autocommit=True)

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
                                  escrow_capture_id, buyer_id, deleted_at,
                                  run_lease_expires_at, approval_claimed_at, doc)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    updated_at           = EXCLUDED.updated_at,
                    status               = EXCLUDED.status,
                    escrow_state         = EXCLUDED.escrow_state,
                    escrow_capture_id    = EXCLUDED.escrow_capture_id,
                    buyer_id             = EXCLUDED.buyer_id,
                    deleted_at           = EXCLUDED.deleted_at,
                    run_lease_expires_at = EXCLUDED.run_lease_expires_at,
                    approval_claimed_at  = EXCLUDED.approval_claimed_at,
                    doc                  = EXCLUDED.doc
                """,
                (
                    job.id,
                    job.created_at,
                    job.updated_at,
                    job.status.value,
                    job.escrow.state.value,
                    job.escrow.capture_id,
                    job.buyer_id,
                    job.deleted_at,
                    job.run_lease_expires_at,
                    job.approval_claimed_at,
                    self._jsonb(payload),
                ),
            )

    def get(self, job_id: str) -> Job | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT doc FROM jobs WHERE id = %s AND deleted_at IS NULL", (job_id,)
            )
            row = cur.fetchone()
        return self._row_to_job(row[0]) if row else None

    def get_including_deleted(self, job_id: str) -> Job | None:
        with self._connect() as conn:
            return self._load(conn, job_id)

    def list(
        self,
        limit: int = 50,
        *,
        buyer_id: str | None = None,
        seller_id: str | None = None,
    ) -> list[Job]:
        # Filters are applied in SQL, before LIMIT, so a seller's older jobs are
        # not dropped just because other buyers posted newer ones. A seller is
        # party to a job they won (doc.seller_id) or were invited to (an entry in
        # the doc.offers array). There is no seller_id column, so the winner is
        # read from the document.
        clauses = ["deleted_at IS NULL"]
        params: list[object] = []
        if buyer_id is not None:
            clauses.append("buyer_id = %s")
            params.append(buyer_id)
        if seller_id is not None:
            clauses.append(
                "(doc ->> 'seller_id' = %s OR doc -> 'offers' @> %s::jsonb)"
            )
            params.append(seller_id)
            params.append(json.dumps([{"seller_id": seller_id}]))
        params.append(limit)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT doc FROM jobs WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC LIMIT %s",
                tuple(params),
            )
            return [self._row_to_job(row[0]) for row in cur.fetchall()]

    def delete(self, job_id: str) -> bool:
        """Tombstone a job. The row is retained so a live authorization is never lost."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET deleted_at = now(), updated_at = now(),
                    doc = jsonb_set(doc, '{deleted_at}', to_jsonb(now()::text))
                WHERE id = %s AND deleted_at IS NULL
                RETURNING id
                """,
                (job_id,),
            )
            return cur.fetchone() is not None

    # ------------------------------------------------- cross-instance guards

    def claim_run(self, job: Job, *, lease_seconds: int = 300) -> bool:
        """Atomically move a job out of a runnable state. False means someone else won.

        Reclaims a job whose execution lease has expired, so a crashed worker does
        not wedge the job. The transition is a single conditional UPDATE, so two
        instances cannot both claim the same job.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'negotiating',
                    run_lease_expires_at = now() + make_interval(secs => %s),
                    updated_at = now()
                WHERE id = %s
                  AND deleted_at IS NULL
                  AND (
                        status = ANY(%s)
                     OR (status = ANY(%s) AND run_lease_expires_at IS NOT NULL
                         AND run_lease_expires_at < now())
                  )
                RETURNING id
                """,
                (lease_seconds, job.id, _RUNNABLE_STATUSES, _IN_FLIGHT_STATUSES),
            )
            return cur.fetchone() is not None

    def claim_approval(self, job: Job) -> bool:
        """Atomically claim the buyer-approval transition. False means someone else won.

        The claim is keyed off the *escrow* state, not the job status: a first
        attempt that PayPal rejected before approval leaves the job FAILED but the
        order still awaiting approval, and that attempt must remain retryable.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET approval_claimed_at = now(), updated_at = now()
                WHERE id = %s AND deleted_at IS NULL
                  AND escrow_state = 'awaiting_approval'
                  AND approval_claimed_at IS NULL
                RETURNING id
                """,
                (job.id,),
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
        """One transaction per connection, so ``SELECT ... FOR UPDATE`` actually locks.

        Autocommit would commit the ``FOR UPDATE`` immediately and release the row
        lock before the write, so concurrent increments for the same agent would
        read the same value and overwrite each other — silently losing settlements.
        """
        return _driver().connect(self.dsn, autocommit=False)

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
        """Read-modify-write one agent under a row lock, in one transaction.

        Concurrent settlements for the same agent must not lose an increment, so the
        row is selected ``FOR UPDATE`` and the counters are recomputed from the
        locked copy inside the same transaction that writes them. A missing row is
        inserted first so the lock is always taken on a real row.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                # Take the lock on a row that certainly exists. A plain FOR UPDATE
                # over nothing locks nothing, which is how concurrent inserts for a
                # brand-new agent would both read the blank record.
                cur.execute(
                    """
                    INSERT INTO reputation (agent_id, doc) VALUES (%s, %s)
                    ON CONFLICT (agent_id) DO NOTHING
                    """,
                    (agent_id, self._jsonb(self._blank(agent_id))),
                )
                cur.execute(
                    "SELECT doc FROM reputation WHERE agent_id = %s FOR UPDATE",
                    (agent_id,),
                )
                row = cur.fetchone()
            record = row[0] if row else self._blank(agent_id)
            apply(record)
            self._write(conn, record)
            conn.commit()
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

    def record_reconciliation_reversed(self, agent_id: str) -> dict:
        """Withdraw the failure charged for an outcome that turned out to be no fault.

        Mirrors the filesystem ledger: reconciliation that finds no capture was
        applied means the seller was never at fault, so the provisional failure is
        reversed.
        """

        def apply(record: dict) -> None:
            record["failed"] = max(0, record["failed"] - 1)

        return self._mutate(agent_id, apply)

    def record_reconciled(self, agent_id: str, amount: float) -> dict:
        """Correct an earlier unresolved outcome once PayPal confirms the payment.

        Mirrors the filesystem ledger: the failure recorded while the outcome was
        unknown is reversed and the settlement credited, so reputation does not
        contradict a ledger that says the money moved.
        """

        def apply(record: dict) -> None:
            record["failed"] = max(0, record["failed"] - 1)
            record["settled"] += 1
            record["volume"] = round(record["volume"] + float(amount), 2)
            record["last_settled_at"] = datetime.now(timezone.utc).isoformat()

        return self._mutate(agent_id, apply)

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
