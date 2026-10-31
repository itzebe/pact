"""Job persistence and agent reputation.

Jobs are written to disk as JSON after every state transition. This sandbox and
most container platforms lose memory on restart, and a settlement ledger that
vanishes is worse than no ledger at all, so durability is not optional here.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from ..config import settings
from .models import EscrowState, Job, JobStatus

log = logging.getLogger("pact.store")


def _seller_is_party(job: Job, seller_id: str) -> bool:
    """True when ``seller_id`` won the job or was invited to it (has an offer)."""
    if job.seller_id == seller_id:
        return True
    return any(getattr(o, "seller_id", None) == seller_id for o in job.offers)


# Statuses that mean a worker is (or was) mid-run. A job in one of these can be
# reclaimed only once its execution lease has expired.
IN_FLIGHT_STATUSES = frozenset(
    {
        JobStatus.NEGOTIATING,
        JobStatus.AGREED,
        JobStatus.ESCROWED,
        JobStatus.DELIVERED,
        JobStatus.VERIFYING,
        JobStatus.AWAITING_APPROVAL,
    }
)

# Default execution lease. Long enough for a real negotiation plus a PayPal
# round-trip, short enough that a crashed worker is recovered promptly.
DEFAULT_LEASE_SECONDS = 300

# Escrow states from which nothing further can move money. Only these may be
# deleted; anything else might still hold the buyer's funds.
TERMINAL_ESCROW_STATES = frozenset(
    {EscrowState.NONE, EscrowState.VOIDED, EscrowState.REFUNDED}
)


def _writable_dir(preferred: Path) -> Path:
    """Return a directory that can actually be written to.

    Serverless platforms mount the code read-only, so the configured data
    directory may be impossible to create. Falling back keeps the app usable while
    ``durable`` stays False, so nothing claims the ledger will survive a restart.
    """
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / ".write-probe"
        probe.write_text("")
        probe.unlink()
        return preferred
    except OSError as exc:
        fallback = Path(tempfile.mkdtemp(prefix="pact-data-"))
        log.warning(
            "Data directory %s is not writable (%s). Falling back to %s. "
            "Storage is NOT durable — set DATABASE_URL to persist the ledger.",
            preferred, exc, fallback,
        )
        return fallback


class JobStore:
    """Thread-safe, disk-backed store of jobs keyed by id."""

    backend = "filesystem"
    durable = False  # survives restarts only where the directory is on a real disk
    supports_claims = True

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = _writable_dir(directory or settings.data_dir)
        # A read-only platform (serverless) has no durable disk. The store still
        # works, but the deployment must not be described as durable: DATABASE_URL
        # is the fix, and /api/health reports the real backend.
        self.durable = self.directory == (directory or settings.data_dir)
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._load()

    def _path(self, job_id: str) -> Path:
        return self.directory / f"{job_id}.json"

    def _load(self) -> None:
        for path in sorted(self.directory.glob("job_*.json")):
            try:
                job = Job.model_validate_json(path.read_text())
                self._jobs[job.id] = job
            except Exception as exc:  # a corrupt file must not stop startup
                log.warning("Skipping unreadable job file %s: %s", path.name, exc)
        if self._jobs:
            log.info("Loaded %d job(s) from %s", len(self._jobs), self.directory)

    def save(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job
            tmp = self._path(job.id).with_suffix(".tmp")
            tmp.write_text(job.model_dump_json(indent=2))
            tmp.replace(self._path(job.id))

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            # A tombstoned job is gone as far as callers are concerned, but its
            # record is retained on disk for audit and reconciliation.
            return None if (job is not None and job.is_deleted) else job

    def get_including_deleted(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(
        self,
        limit: int = 50,
        *,
        buyer_id: str | None = None,
        seller_id: str | None = None,
    ) -> list[Job]:
        """Newest jobs first, filtered server-side.

        ``seller_id`` matches a job the seller won or was invited to (they have an
        offer). The filter runs *before* the limit is applied: filtering a
        globally-truncated page would silently drop a seller's older jobs once
        there are more jobs than the limit.
        """
        with self._lock:
            jobs = [
                j for j in self._jobs.values()
                if not j.is_deleted
                and (buyer_id is None or j.buyer_id == buyer_id)
                and (seller_id is None or _seller_is_party(j, seller_id))
            ]
            jobs.sort(key=lambda j: j.created_at, reverse=True)
            return jobs[:limit]

    def delete(self, job_id: str) -> bool:
        """Tombstone a job. The record is retained, never removed from disk.

        Returns False if the job does not exist (or was already deleted). The
        caller is responsible for refusing the delete when escrow is not
        terminal; this method only records the tombstone.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_deleted:
                return False
            job.deleted_at = datetime.now(timezone.utc)
            job.append("job_deleted", "pact", "Job tombstoned; financial record retained")
            self.save(job)
            return True

    # ------------------------------------------------- concurrency guards

    def claim_run(self, job: Job, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> bool:
        """Atomically move a job out of a runnable state. False means someone else won.

        Reclaims a job whose execution lease has expired, so a worker that died
        mid-run (a serverless timeout, a crash) does not wedge the job forever.
        Mirrors the Postgres store; in-process this is enough because one instance
        owns the whole dict.
        """
        with self._lock:
            current = self._jobs.get(job.id)
            if current is None or current.is_deleted:
                return False
            runnable = current.status in {JobStatus.OPEN, JobStatus.FAILED}
            recoverable = (
                current.status in IN_FLIGHT_STATUSES and not current.lease_is_live()
            )
            if not (runnable or recoverable):
                return False
            if recoverable:
                current.append(
                    "run_recovered",
                    "pact",
                    "Reclaimed a run whose lease expired; the previous worker did not finish",
                    previous_status=current.status.value,
                )
            current.status = JobStatus.NEGOTIATING
            current.run_lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
            self.save(current)
            return True

    def claim_approval(self, job: Job) -> bool:
        """Atomically claim the buyer-approval transition. False means someone else won.

        This is the transition that authorizes the buyer's funds, so it must be
        decided in one place. Two concurrent approvals would otherwise both read
        ``awaiting_approval`` and both proceed to authorize and settle. The claim
        is keyed off the *escrow* state, not the job status: a first attempt that
        PayPal rejected before approval leaves the job FAILED but the order still
        awaiting approval, and that attempt must remain retryable.
        """
        with self._lock:
            current = self._jobs.get(job.id)
            if current is None or current.is_deleted:
                return False
            if current.escrow.state is not EscrowState.AWAITING_APPROVAL:
                return False
            if current.approval_claimed_at is not None:
                return False
            current.approval_claimed_at = datetime.now(timezone.utc)
            current.append(
                "approval_claimed",
                "pact",
                "Buyer approval claimed; continuing to authorization",
            )
            self.save(current)
            return True

    @contextmanager
    def settlement_claim(self, job: Job) -> Iterator[Job | None]:
        """Yield the freshest stored copy of the job for a settlement attempt.

        The in-process lock lives in the settlement engine; this only guarantees the
        caller works from current state rather than a stale object.
        """
        with self._lock:
            fresh = self._jobs.get(job.id)
            yield None if (fresh is not None and fresh.is_deleted) else fresh


class ReputationLedger:
    """Evidence-based reputation, derived only from settled outcomes.

    An agent's score is a function of *verified* deliveries, not self-reported
    claims. A seller that takes escrow and then fails verification loses score;
    voiding an authorization is therefore a real economic signal, which is the
    point of putting settlement behind verification.
    """

    def __init__(self, path: Path | None = None) -> None:
        target = path or (settings.data_dir / "reputation.json")
        self.durable = True
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            target = _writable_dir(target.parent) / target.name
            self.durable = False
        self.path = target
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._records = json.loads(self.path.read_text())
            except Exception as exc:
                log.warning("Could not read reputation file: %s", exc)
                self._records = {}

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._records, indent=2, default=str))

    def _record_for(self, agent_id: str) -> dict:
        return self._records.setdefault(
            agent_id,
            {
                "agent_id": agent_id,
                "settled": 0,
                "voided": 0,
                "failed": 0,
                "volume": 0.0,
                "last_settled_at": None,
            },
        )

    def record_settled(self, agent_id: str, amount: float) -> dict:
        with self._lock:
            record = self._record_for(agent_id)
            record["settled"] += 1
            record["volume"] = round(record["volume"] + float(amount), 2)
            record["last_settled_at"] = datetime.now(timezone.utc).isoformat()
            self._flush()
            return dict(record)

    def record_voided(self, agent_id: str) -> dict:
        with self._lock:
            record = self._record_for(agent_id)
            record["voided"] += 1
            self._flush()
            return dict(record)

    def record_failed(self, agent_id: str) -> dict:
        with self._lock:
            record = self._record_for(agent_id)
            record["failed"] += 1
            self._flush()
            return dict(record)

    def record_reconciliation_reversed(self, agent_id: str) -> dict:
        """Withdraw the failure charged for an outcome that turned out to be no fault.

        ``mark_unresolved`` records a failure while the outcome is unknown, because
        that is the honest reading at the time. When reconciliation establishes that
        no capture was applied — the hold is intact, or the authorize simply landed
        — the seller did nothing wrong and the failure must be withdrawn.
        """
        with self._lock:
            record = self._record_for(agent_id)
            record["failed"] = max(0, record["failed"] - 1)
            self._flush()
            return dict(record)

    def record_reconciled(self, agent_id: str, amount: float) -> dict:
        """Correct an earlier unresolved outcome once PayPal confirms the payment.

        An ambiguous capture is recorded as a failure while the outcome is unknown,
        because that is the honest reading at the time. When reconciliation later
        establishes that the money did move, that failure was not the seller's and
        the settlement must be credited, or the reputation contradicts the ledger.
        """
        with self._lock:
            record = self._record_for(agent_id)
            # Reverse the failure recorded by mark_unresolved, never below zero.
            record["failed"] = max(0, record["failed"] - 1)
            record["settled"] += 1
            record["volume"] = round(record["volume"] + float(amount), 2)
            record["last_settled_at"] = datetime.now(timezone.utc).isoformat()
            self._flush()
            return dict(record)

    @staticmethod
    def score(record: dict) -> float:
        """Verified delivery rate, smoothed toward 0.5 for thin histories.

        The smoothing matters: an agent with one lucky settlement should not
        outrank one with twenty, and a brand-new agent should not be treated as
        proven. Two pseudo-observations give a defensible prior.
        """
        settled = record.get("settled", 0)
        voided = record.get("voided", 0)
        failures = voided + record.get("failed", 0)
        return round((settled + 1) / (settled + failures + 2), 3)

    def all(self) -> list[dict]:
        with self._lock:
            records = []
            for record in self._records.values():
                entry = dict(record)
                entry["score"] = self.score(record)
                records.append(entry)
            return sorted(records, key=lambda r: (-r["score"], -r["settled"]))

    def get(self, agent_id: str) -> dict:
        with self._lock:
            record = dict(self._record_for(agent_id))
            record["score"] = self.score(record)
            return record


class UnavailableStore:
    """Stands in when the configured durable backend could not be opened.

    Refusing to serve is safer than silently falling back to instance-local files:
    on a serverless host that fallback would look healthy while losing the ledger
    on every cold start, and could settle real money against state no other
    instance can see. Every operation raises, so no settlement can proceed, and
    ``error`` is reported through ``/api/health`` so the cause is visible instead
    of appearing as an unexplained 500 on every route.
    """

    backend = "unavailable"
    durable = False
    supports_claims = False

    def __init__(self, error: Exception) -> None:
        self.error = error

    def _fail(self, *_, **__):
        raise RuntimeError(
            f"Durable storage is unavailable, so settlement is disabled: {self.error}"
        )

    save = get = get_including_deleted = list = delete = claim_run = _fail
    claim_approval = settlement_claim = _fail


def _select_backend():
    """Choose the persistence backend from configuration.

    Defaults to the filesystem store, so local runs, Docker, Render, and the whole
    existing test suite behave exactly as before. Postgres is opt-in and is the only
    correct choice on a platform with no durable disk.
    """
    dsn = settings.database_url
    if not dsn:
        return JobStore(), ReputationLedger()

    from .dbstore import PostgresJobStore, PostgresReputationLedger

    try:
        return PostgresJobStore(dsn), PostgresReputationLedger(dsn)
    except Exception as exc:  # connection refused, bad credentials, missing schema
        log.error(
            "DATABASE_URL is set but the Postgres backend could not be opened (%s). "
            "Settlement is disabled until the database is reachable; the app will NOT "
            "fall back to non-durable local storage.",
            exc,
        )
        return UnavailableStore(exc), ReputationLedger()


job_store, reputation = _select_backend()

# True only when the ledger is safe to lose: an ephemeral or read-only filesystem
# means the deployment must not be presented as durable.
DURABLE = bool(getattr(job_store, "durable", False))
