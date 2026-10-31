"""Domain model for a Pact: negotiation -> escrow -> verification -> settlement."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# Sentinel for ``EscrowRecord.capture_id`` meaning "a capture has been claimed but
# its outcome is not yet recorded". It is written *before* the provider call so a
# second instance cannot capture the same authorization, so it must be
# distinguishable both from a real capture id and from no capture at all.
IN_FLIGHT_CAPTURE = "pending"


class JobStatus(str, Enum):
    OPEN = "open"
    NEGOTIATING = "negotiating"
    AGREED = "agreed"
    AWAITING_APPROVAL = "awaiting_approval"  # order created, buyer has not approved yet
    ESCROWED = "escrowed"
    DELIVERED = "delivered"
    VERIFYING = "verifying"
    SETTLED = "settled"
    VOIDED = "voided"        # hold released, buyer charged nothing (no money moved)
    REFUNDED = "refunded"    # a capture was reversed after the fact
    UNRESOLVED = "unresolved"  # outcome unknown; requires reconciliation, never auto-completed
    FAILED = "failed"


class EscrowState(str, Enum):
    NONE = "none"
    AWAITING_APPROVAL = "awaiting_approval"
    HELD = "held"
    RELEASED = "released"
    VOIDED = "voided"
    REFUNDED = "refunded"
    UNRESOLVED = "unresolved"


class Message(BaseModel):
    """One utterance in a negotiation, from either side."""

    round: int
    role: str  # "buyer" | "seller"
    agent_id: str
    text: str
    offer: Decimal | None = None
    rationale: str = ""
    at: datetime = Field(default_factory=now)


class Offer(BaseModel):
    seller_id: str
    price: Decimal
    terms: str = ""
    delivery_estimate: str = ""
    confidence: float = 0.0


class Delivery(BaseModel):
    seller_id: str
    summary: str
    artifact: str
    delivered_at: datetime = Field(default_factory=now)


class CriterionResult(BaseModel):
    criterion: str
    passed: bool
    evidence: str


class Verification(BaseModel):
    passed: bool
    score: float
    criteria: list[CriterionResult] = Field(default_factory=list)
    summary: str = ""


class EscrowRecord(BaseModel):
    provider: str = "paypal"
    mode: str = "sandbox"  # sandbox | live | mock
    # True when this record came from the in-memory double rather than a real
    # provider. Set at hold time and carried through settlement, so a simulated
    # capture can never be reported as a real payment.
    simulated: bool = False
    order_id: str = ""
    approval_url: str = ""
    authorization_id: str = ""
    authorization_status: str = ""
    capture_id: str = ""
    capture_status: str = ""
    refund_id: str = ""
    amount: Decimal = Decimal("0")
    currency: str = "USD"
    state: EscrowState = EscrowState.NONE
    updated_at: datetime = Field(default_factory=now)


class LedgerEntry(BaseModel):
    """An append-only audit record. ``hash`` chains to the previous entry."""

    seq: int
    at: datetime = Field(default_factory=now)
    kind: str
    actor: str
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str = ""
    hash: str = ""

    def compute_hash(self) -> str:
        payload = json.dumps(
            {
                "seq": self.seq,
                "at": self.at.isoformat(),
                "kind": self.kind,
                "actor": self.actor,
                "summary": self.summary,
                "detail": self.detail,
                "prev_hash": self.prev_hash,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class Job(BaseModel):
    id: str = Field(default_factory=lambda: new_id("job"))
    title: str
    brief: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    budget_max: Decimal
    currency: str = "USD"
    buyer_id: str = "buyer"
    status: JobStatus = JobStatus.OPEN
    messages: list[Message] = Field(default_factory=list)
    offers: list[Offer] = Field(default_factory=list)
    agreed_price: Decimal | None = None
    agreed_terms: str = ""
    seller_id: str | None = None
    delivery: Delivery | None = None
    verification: Verification | None = None
    escrow: EscrowRecord = Field(default_factory=EscrowRecord)
    ledger: list[LedgerEntry] = Field(default_factory=list)
    # A soft-delete tombstone. A deleted job is hidden from listings and returns
    # 404, but its record is retained so a live PayPal authorization can never be
    # silently discarded. ``deleted_at`` is set only once the escrow is terminal.
    deleted_at: datetime | None = None
    # Execution lease. A run claims the job for a bounded window; if the worker
    # dies (timeout, crash) the lease expires and the job becomes claimable
    # again, so an interrupted run can never wedge a job forever.
    run_lease_expires_at: datetime | None = None
    # Set exactly once when a buyer approval is claimed, so two concurrent
    # approvals cannot both proceed to authorize the buyer's funds.
    approval_claimed_at: datetime | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)

    def lease_is_live(self, at: datetime | None = None) -> bool:
        if self.run_lease_expires_at is None:
            return False
        return (at or now()) < self.run_lease_expires_at

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def settlement_is_real(self) -> bool:
        """True only when a real provider moved (or is holding) real money.

        A mock capture must never be presented as a payment, so this is False
        whenever the escrow record is flagged simulated.
        """
        return not self.escrow.simulated and self.escrow.mode not in {"mock", ""}

    # ------------------------------------------------------------- ledger

    def append(self, kind: str, actor: str, summary: str, **detail: Any) -> LedgerEntry:
        prev = self.ledger[-1].hash if self.ledger else ""
        entry = LedgerEntry(
            seq=len(self.ledger),
            kind=kind,
            actor=actor,
            summary=summary,
            detail=detail,
            prev_hash=prev,
        )
        entry.hash = entry.compute_hash()
        self.ledger.append(entry)
        self.updated_at = now()
        return entry

    def verify_chain(self) -> bool:
        """Recompute the hash chain; any edit to history breaks it."""
        prev = ""
        for index, entry in enumerate(self.ledger):
            if entry.seq != index or entry.prev_hash != prev:
                return False
            if entry.hash != entry.compute_hash():
                return False
            prev = entry.hash
        return True
