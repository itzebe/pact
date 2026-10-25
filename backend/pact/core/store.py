"""Job persistence and agent reputation.

Jobs are written to disk as JSON after every state transition. This sandbox and
most container platforms lose memory on restart, and a settlement ledger that
vanishes is worse than no ledger at all, so durability is not optional here.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from .models import Job

log = logging.getLogger("pact.store")


class JobStore:
    """Thread-safe, disk-backed store of jobs keyed by id."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or settings.data_dir
        self.directory.mkdir(parents=True, exist_ok=True)
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


class ReputationLedger:
    """Evidence-based reputation, derived only from settled outcomes.

    An agent's score is a function of *verified* deliveries, not self-reported
    claims. A seller that takes escrow and then fails verification loses score;
    voiding an authorization is therefore a real economic signal, which is the
    point of putting settlement behind verification.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (settings.data_dir / "reputation.json")
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


job_store = JobStore()
reputation = ReputationLedger()
