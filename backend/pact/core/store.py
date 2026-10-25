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
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..config import settings
from .models import Job, JobStatus

log = logging.getLogger("pact.store")


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
            return self._jobs.get(job_id)

    def list(self, limit: int = 50) -> list[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return jobs[:limit]

    def delete(self, job_id: str) -> bool:
        with self._lock:
            removed = self._jobs.pop(job_id, None)
            path = self._path(job_id)
            if path.exists():
                path.unlink()
            return removed is not None

    # ------------------------------------------------- concurrency guards

    def claim_run(self, job: Job) -> bool:
        """Atomically move a job out of a runnable state. False means someone else won.

        Mirrors the Postgres store. In-process this is enough because one instance
        owns the whole dict; the DB store needs the equivalent SQL-level guard.
        """
        with self._lock:
            current = self._jobs.get(job.id)
            if current is None or current.status not in {JobStatus.OPEN, JobStatus.FAILED}:
                return False
            current.status = JobStatus.NEGOTIATING
            self.save(current)
            return True

    @contextmanager
    def settlement_claim(self, job: Job) -> Iterator[Job | None]:
        """Yield the freshest stored copy of the job for a settlement attempt.

        The in-process lock lives in the settlement engine; this only guarantees the
        caller works from current state rather than a stale object.
        """
        with self._lock:
            yield self._jobs.get(job.id)


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

    return PostgresJobStore(dsn), PostgresReputationLedger(dsn)


job_store, reputation = _select_backend()

# True only when the ledger is safe to lose: an ephemeral or read-only filesystem
# means the deployment must not be presented as durable.
DURABLE = bool(getattr(job_store, "durable", False))
