"""Delivery verification and the settlement decision that follows it.

The whole premise of Pact is that payment follows *verified* delivery. So the
verifier is the load-bearing component: it decides whether held funds are
released or returned. It combines two things:

1. Deterministic checks that no model can talk its way past — the artifact must
   be non-empty, must not contain known evasion phrases, and must clear a length
   floor. A seller cannot get paid by returning nothing.
2. Model judgement against the job's acceptance criteria, for the parts that
   genuinely need reading comprehension.

The final verdict is a conjunction: every deterministic check must pass, and the
model must also pass. Failing either one voids the authorization and the buyer
gets their money back.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

from ..paypal import EscrowBackend, PayPalError
from .llm import LLM, LLMError
from .models import (
    CriterionResult,
    Delivery,
    EscrowState,
    Job,
    JobStatus,
    Verification,
)
from .store import ReputationLedger

log = logging.getLogger("pact.settlement")

VERIFIER_SYSTEM = """You are the independent verifier in an escrow settlement.

TASK:verify

Your job is to decide whether a seller's delivery satisfies the buyer's acceptance criteria.
You are not on either side. You are paid to be right, and a wrong "pass" costs the buyer real money.

Judge each criterion on the evidence actually present in the delivery artifact.
- A criterion is satisfied only if the artifact demonstrably meets it.
- Vague assurances, restated promises, or claims without substance do not satisfy a criterion.
- If the artifact is empty, off-topic, or evasive, fail it.

Respond with ONLY a JSON object:
{"passed": <true|false>, "score": <0..1>, "criteria": [{"criterion": "<text>", "passed": <bool>, "evidence": "<what in the artifact supports this>"}], "summary": "<one or two sentences>"}"""

# Phrases a seller might use to claim success without producing anything.
EVASION_MARKERS = (
    "as an ai",
    "i cannot",
    "i can't actually",
    "trust me",
    "consider it done",
    "no need to verify",
    "guaranteed",
)

MIN_ARTIFACT_CHARS = 40


class Verifier:
    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def deterministic_checks(self, job: Job, delivery: Delivery) -> list[CriterionResult]:
        artifact = (delivery.artifact or "").strip()
        checks: list[CriterionResult] = []

        checks.append(
            CriterionResult(
                criterion="Delivery artifact is present and non-trivial",
                passed=len(artifact) >= MIN_ARTIFACT_CHARS,
                evidence=f"Artifact length {len(artifact)} characters (minimum {MIN_ARTIFACT_CHARS}).",
            )
        )

        lowered = artifact.lower()
        hit = next((m for m in EVASION_MARKERS if m in lowered), None)
        checks.append(
            CriterionResult(
                criterion="Delivery does not rely on evasion or unverifiable assurance",
                passed=hit is None,
                evidence=f"Flagged phrase: '{hit}'." if hit else "No evasion markers found.",
            )
        )

        summary_present = bool((delivery.summary or "").strip())
        checks.append(
            CriterionResult(
                criterion="Seller supplied a delivery summary",
                passed=summary_present,
                evidence="Summary provided." if summary_present else "No summary provided.",
            )
        )

        return checks

    def verify(self, job: Job, delivery: Delivery) -> Verification:
        deterministic = self.deterministic_checks(job, delivery)

        context = {
            "title": job.title,
            "brief": job.brief,
            "criteria": job.acceptance_criteria,
            "agreed_price": float(job.agreed_price or 0),
            "currency": job.currency,
            "delivery_summary": delivery.summary,
            "artifact": delivery.artifact[:8000],
        }

        try:
            raw = self.llm.complete_json(
                VERIFIER_SYSTEM,
                f"CONTEXT:{json.dumps(context)}\n\nReturn your verdict as a JSON object.",
                max_tokens=1200,
            )
            model_verification = _parse_verification(raw, job.acceptance_criteria)
        except LLMError as exc:
            log.warning("Verifier model failed (%s); relying on deterministic checks only", exc)
            model_verification = Verification(
                passed=False,
                score=0.0,
                criteria=[],
                summary=f"Model verification unavailable: {exc}",
            )

        passed = all(c.passed for c in deterministic) and model_verification.passed
        return Verification(
            passed=passed,
            score=model_verification.score,
            criteria=deterministic + model_verification.criteria,
            summary=model_verification.summary,
        )


def _parse_verification(raw: dict, criteria: list[str]) -> Verification:
    results: list[CriterionResult] = []
    for item in raw.get("criteria") or []:
        if not isinstance(item, dict):
            continue
        results.append(
            CriterionResult(
                criterion=str(item.get("criterion", ""))[:300],
                passed=bool(item.get("passed", False)),
                evidence=str(item.get("evidence", ""))[:600],
            )
        )

    try:
        score = max(0.0, min(1.0, float(raw.get("score", 0.0))))
    except (TypeError, ValueError):
        score = 0.0

    # A pass with no per-criterion evidence is not trustworthy; require coverage.
    if bool(raw.get("passed")) and len(results) < max(1, len(criteria)):
        return Verification(
            passed=False,
            score=score,
            criteria=results,
            summary="Verdict rejected: the verifier did not assess every acceptance criterion.",
        )

    return Verification(
        passed=bool(raw.get("passed", False)),
        score=score,
        criteria=results,
        summary=str(raw.get("summary", ""))[:600],
    )


class SettlementEngine:
    """Drives escrow: hold on agreement, release or return on verification."""

    def __init__(self, escrow: EscrowBackend, reputation: ReputationLedger) -> None:
        self.escrow = escrow
        self.reputation = reputation

    @property
    def mode(self) -> str:
        return "mock" if getattr(self.escrow, "environment", "sandbox") == "mock" else getattr(
            self.escrow, "environment", "sandbox"
        )

    # ----------------------------------------------------------------- hold

    def hold(self, job: Job) -> Job:
        """Create an AUTHORIZE order and place a hold for the agreed price."""
        if job.agreed_price is None or job.seller_id is None:
            raise ValueError("Cannot escrow a job with no agreed price and seller")

        amount = job.agreed_price
        job.append(
            "escrow_requested",
            "pact",
            f"Requesting PayPal authorization for {job.currency} {amount:.2f}",
            amount=float(amount),
            seller=job.seller_id,
        )

        order = self.escrow.create_order(
            amount=amount,
            currency=job.currency,
            description=f"Pact escrow: {job.title}",
            reference_id=job.id,
            custom_id=job.id,
            idempotency_key=f"pact-order-{job.id}",
        )
        order_id = order.get("id", "")
        job.escrow.order_id = order_id
        job.append("escrow_order_created", "paypal", f"PayPal order {order_id} created (intent=AUTHORIZE)",
                   order_id=order_id, status=order.get("status"))

        try:
            authorization = self.escrow.authorize_order(order_id, idempotency_key=f"pact-auth-{job.id}")
        except PayPalError as exc:
            job.status = JobStatus.FAILED
            job.append("escrow_failed", "paypal", f"Authorization failed: {exc}", order_id=order_id)
            raise

        job.escrow.authorization_id = authorization.id
        job.escrow.amount = amount
        job.escrow.state = EscrowState.HELD
        job.escrow.mode = self.mode
        job.status = JobStatus.ESCROWED

        job.append(
            "escrow_held",
            "paypal",
            f"Funds held: authorization {authorization.id} for {authorization.currency} {authorization.amount}",
            authorization_id=authorization.id,
            status=authorization.status,
            expires_at=authorization.expires_at,
            mode=job.escrow.mode,
        )
        return job

    # -------------------------------------------------------------- settle

    def release(self, job: Job, note: str = "") -> Job:
        """Capture the held funds because the delivery passed verification."""
        if job.escrow.state != EscrowState.HELD:
            raise ValueError(f"Cannot release escrow in state {job.escrow.state}")
        if not job.escrow.authorization_id:
            raise ValueError("No authorization to capture")

        try:
            capture = self.escrow.capture_authorization(
                job.escrow.authorization_id,
                amount=job.escrow.amount,
                currency=job.currency,
                note_to_payer=note or "Released by Pact after verified delivery",
                idempotency_key=f"pact-capture-{job.id}",
            )
        except PayPalError as exc:
            job.append("capture_failed", "paypal", f"Capture failed: {exc}", authorization_id=job.escrow.authorization_id)
            raise

        job.escrow.capture_id = capture.id
        job.escrow.state = EscrowState.RELEASED
        job.status = JobStatus.SETTLED

        job.append(
            "escrow_released",
            "paypal",
            f"Funds released: capture {capture.id} for {capture.currency} {capture.amount}",
            capture_id=capture.id,
            status=capture.status,
            amount=float(job.escrow.amount),
            mode=job.escrow.mode,
        )

        if job.seller_id:
            record = self.reputation.record_settled(job.seller_id, float(job.escrow.amount))
            job.append(
                "reputation_updated",
                "pact",
                f"{job.seller_id} score now {self.reputation.score(record):.3f} "
                f"({record['settled']} settled)",
                agent_id=job.seller_id,
                score=self.reputation.score(record),
            )
        return job

    def refund(self, job: Job, reason: str = "") -> Job:
        """Void the hold because the delivery failed verification."""
        if job.escrow.state != EscrowState.HELD:
            raise ValueError(f"Cannot void escrow in state {job.escrow.state}")
        if not job.escrow.authorization_id:
            raise ValueError("No authorization to void")

        try:
            self.escrow.void_authorization(
                job.escrow.authorization_id,
                idempotency_key=f"pact-void-{job.id}",
            )
        except PayPalError as exc:
            job.append("void_failed", "paypal", f"Void failed: {exc}", authorization_id=job.escrow.authorization_id)
            raise

        job.escrow.state = EscrowState.VOIDED
        job.status = JobStatus.REFUNDED

        job.append(
            "escrow_voided",
            "paypal",
            f"Hold released: authorization {job.escrow.authorization_id} voided, buyer charged nothing",
            authorization_id=job.escrow.authorization_id,
            reason=reason,
            mode=job.escrow.mode,
        )

        if job.seller_id:
            record = self.reputation.record_refunded(job.seller_id)
            job.append(
                "reputation_updated",
                "pact",
                f"{job.seller_id} score now {self.reputation.score(record):.3f} "
                f"({record['refunded']} refunded)",
                agent_id=job.seller_id,
                score=self.reputation.score(record),
            )
        return job
