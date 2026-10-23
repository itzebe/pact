"""The Pact orchestrator: one call drives a job from brief to settled payment.

The lifecycle is deliberately linear and fully audited:

    brief -> negotiate -> agree -> hold escrow -> deliver -> verify -> settle

Each transition appends to the job's hash-chained ledger, so the resulting
record is a complete account of what was agreed, what was delivered, why it was
judged sufficient, and which PayPal object moved the money. If verification
fails at the end, the hold is voided and the buyer is charged nothing — that
refund path is the reason escrow is used at all rather than immediate capture.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from ..config import settings
from ..agents.seller import SellerAgent
from ..paypal import EscrowBackend, PayPalError, build_escrow_client
from .llm import LLM, build_llm
from .models import EscrowState, Job, JobStatus
from .negotiation import NegotiationEngine, default_seller_ids
from .settlement import SettlementEngine, Verifier
from .store import JobStore, ReputationLedger, job_store, reputation

log = logging.getLogger("pact.engine")


class PactEngine:
    def __init__(
        self,
        *,
        llm: LLM | None = None,
        escrow: EscrowBackend | None = None,
        store: JobStore | None = None,
        reputation_ledger: ReputationLedger | None = None,
        max_rounds: int = 3,
    ) -> None:
        self.llm = llm or build_llm()
        self.escrow = escrow or build_escrow_client()
        self.store = store or job_store
        self.reputation = reputation_ledger or reputation
        self.negotiation = NegotiationEngine(self.llm, max_rounds=max_rounds)
        self.verifier = Verifier(self.llm)
        self.settlement = SettlementEngine(self.escrow, self.reputation)

    # ------------------------------------------------------------- helpers

    @property
    def settlement_mode(self) -> str:
        return self.settlement.mode

    def create_job(self, *, title: str, brief: str, criteria: list[str], budget_max: Decimal,
                   buyer_id: str = "buyer", currency: str | None = None) -> Job:
        job = Job(
            title=title,
            brief=brief,
            acceptance_criteria=criteria,
            budget_max=budget_max,
            currency=currency or settings.currency,
            buyer_id=buyer_id,
        )
        job.append(
            "job_created",
            buyer_id,
            f"Job posted: {title}",
            budget_max=float(budget_max),
            currency=job.currency,
            criteria=criteria,
        )
        self.store.save(job)
        return job

    # ----------------------------------------------------------- lifecycle

    def run(self, job: Job, seller_ids: list[str] | None = None) -> Job:
        """Drive the job to a terminal state. Never raises on a failed settlement."""
        seller_ids = seller_ids or default_seller_ids()
        job.status = JobStatus.NEGOTIATING
        job.append("negotiation_started", "pact", f"Inviting sellers: {', '.join(seller_ids)}")
        self.store.save(job)

        accepted = self.negotiation.negotiate(job, seller_ids)
        score_of = lambda seller_id: self.reputation.get(seller_id)["score"]
        winning = self.negotiation.select_winner(accepted, score_of)
        if winning is None:
            job.status = JobStatus.FAILED
            job.append("negotiation_failed", "pact", "No seller produced an offer within budget")
            self.store.save(job)
            return job

        if len(accepted) > 1:
            considered = ", ".join(
                f"{o.seller_id}@{o.price:.2f}(score {self.reputation.get(o.seller_id)['score']:.2f})"
                for o in sorted(accepted, key=lambda o: o.price)
            )
            job.append(
                "seller_selected",
                "pact",
                f"Selected {winning.seller_id} on risk-adjusted cost; considered {considered}",
                winner=winning.seller_id,
                winner_price=float(winning.price),
                candidates=[
                    {"seller": o.seller_id, "price": float(o.price), "score": self.reputation.get(o.seller_id)["score"]}
                    for o in accepted
                ],
            )

        job.seller_id = winning.seller_id
        job.agreed_price = winning.price
        job.agreed_terms = winning.terms
        job.status = JobStatus.AGREED
        job.append(
            "agreement",
            "pact",
            f"Agreed with {winning.seller_id} at {job.currency} {winning.price:.2f}",
            seller=winning.seller_id,
            price=float(winning.price),
            terms=winning.terms,
        )
        self.store.save(job)

        # --- hold funds
        try:
            self.settlement.hold(job)
        except (PayPalError, ValueError) as exc:
            job.status = JobStatus.FAILED
            job.append("escrow_error", "pact", f"Could not escrow funds: {exc}")
            self.store.save(job)
            return job
        self.store.save(job)

        # --- deliver
        job.status = JobStatus.DELIVERED
        delivery = SellerAgent(self.llm, winning.seller_id).deliver(job)
        job.delivery = delivery
        job.append(
            "delivery",
            winning.seller_id,
            f"Delivered: {delivery.summary or 'no summary'}",
            chars=len(delivery.artifact),
        )
        self.store.save(job)

        # --- verify
        job.status = JobStatus.VERIFYING
        job.append("verification_started", "pact", "Independent verification of the delivery")
        verification = self.verifier.verify(job, delivery)
        job.verification = verification
        job.append(
            "verification_result",
            "verifier",
            f"Verification {'PASSED' if verification.passed else 'FAILED'} "
            f"(score {verification.score:.2f})",
            passed=verification.passed,
            score=verification.score,
            criteria=[c.model_dump() for c in verification.criteria],
        )
        self.store.save(job)

        # --- settle
        try:
            if verification.passed:
                self.settlement.release(job, note=f"Pact: verified delivery for '{job.title}'")
            else:
                self.settlement.refund(job, reason=verification.summary or "Delivery did not meet the criteria")
        except (PayPalError, ValueError) as exc:
            job.status = JobStatus.FAILED
            job.append("settlement_error", "pact", f"Settlement failed: {exc}")
            self.store.save(job)
            return job

        self.store.save(job)
        return job

    def settle_existing(self, job: Job) -> Job:
        """Verify and settle a job that already holds escrow (used to demo a refund)."""
        if job.escrow.state != EscrowState.HELD or job.delivery is None:
            return job
        verification = self.verifier.verify(job, job.delivery)
        job.verification = verification
        job.append("verification_result", "verifier",
                   f"Verification {'PASSED' if verification.passed else 'FAILED'} (score {verification.score:.2f})",
                   passed=verification.passed, score=verification.score)
        if verification.passed:
            self.settlement.release(job)
        else:
            self.settlement.refund(job, reason=verification.summary)
        self.store.save(job)
        return job


engine = PactEngine()
