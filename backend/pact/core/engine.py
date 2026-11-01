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
import threading
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterator

from ..config import settings
from ..agents.seller import SellerAgent
from ..paypal import EscrowBackend, PayPalError, build_escrow_client
from .llm import LLM, build_llm
from .models import EscrowState, Job, JobStatus
from .negotiation import NegotiationEngine, default_seller_ids
from .settlement import SettlementEngine, Verifier
from .store import JobStore, ReputationLedger, job_store, reputation

log = logging.getLogger("pact.engine")


class JobAlreadyRunning(RuntimeError):
    """Raised when a job that is already mid-flight is asked to run again."""


class ServerBusy(RuntimeError):
    """Raised when too many runs are already in flight in this process."""


class RunDeadlineExceeded(RuntimeError):
    """Raised when a run exceeds its internal deadline before the platform kills it."""


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
        self.settlement = SettlementEngine(self.escrow, self.reputation, store=self.store)
        # Bound how many runs execute at once. Without this, every arriving request
        # starts an LLM loop and the process melts under load rather than shedding
        # it. The semaphore is released in a finally, so a failed run never leaks a
        # slot.
        self._run_slots = threading.BoundedSemaphore(settings.max_concurrent_runs)

    @contextmanager
    def _run_slot(self) -> Iterator[None]:
        """Acquire a run slot, or refuse quickly. Never blocks without bound."""
        acquired = self._run_slots.acquire(timeout=settings.run_queue_timeout_seconds)
        if not acquired:
            raise ServerBusy(
                "The server is at its concurrent-run limit; retry shortly"
            )
        try:
            yield
        finally:
            self._run_slots.release()

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
        # Stamp the escrow record's mode up front from the *actual* client, not the
        # record default. Without this a job created on a mock deployment would
        # report ``settlement_is_real: true`` until the first hold, which is exactly
        # the "simulated settlement presented as real" mistake this field exists to
        # prevent.
        self._stamp_escrow_mode(job)
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

    def _stamp_escrow_mode(self, job: Job) -> None:
        """Record whether this job's escrow will be real, from the live client."""
        mode = self.settlement.mode
        job.escrow.mode = mode
        job.escrow.simulated = mode == "mock"

    # ----------------------------------------------------------- lifecycle

    def run(self, job: Job, seller_ids: list[str] | None = None, *, public_url: str | None = None) -> Job:
        """Drive the job to a terminal state, inside a slot and a hard deadline.

        Two failure modes are handled here rather than by the platform:

        * Under load, more runs arriving than the process can serve are refused
          with ``ServerBusy`` (surfaced as 429) instead of queueing without bound.
        * A run that exceeds ``PACT_RUN_TIMEOUT_SECONDS`` — the platform's own
          function timeout is the backstop — is abandoned *before* the platform
          kills the process, and the job is marked failed with its lease released.
          That is what stops a slow run from leaving a job stuck in ``negotiating``
          forever with no worker and no expiry.
        """
        with self._run_slot():
            return self._with_deadline(self._run_inner, job, seller_ids, public_url=public_url)

    def _with_deadline(self, fn, job: Job, *args, **kwargs) -> Job:
        """Run ``fn(job, ...)`` in a worker thread, bounded by the run deadline."""
        result: dict[str, Any] = {}

        def target() -> None:
            try:
                result["job"] = fn(job, *args, **kwargs)
            except BaseException as exc:  # captured and re-raised on the caller's thread
                result["error"] = exc

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(timeout=settings.run_timeout_seconds)
        if worker.is_alive():
            # The worker is stuck in a blocking provider/LLM call. We cannot safely
            # kill it, but we can stop waiting and make the job's state honest: the
            # lease is expired and the job is failed, so it is not wedged.
            self._abandon(job, "run exceeded its time budget")
            raise RunDeadlineExceeded(
                f"Job {job.id} exceeded the {settings.run_timeout_seconds}s run budget"
            )
        if "error" in result:
            raise result["error"]
        return result["job"]

    def _abandon(self, job: Job, reason: str) -> None:
        """Mark a job failed and release its lease, best-effort."""
        try:
            fresh = self.store.get(job.id)
            if fresh is None:
                return
            fresh.status = JobStatus.FAILED
            fresh.run_lease_expires_at = None
            fresh.append("run_abandoned", "pact", reason)
            self.store.save(fresh)
        except Exception as exc:  # a store failure must not mask the timeout
            log.error("Could not record abandonment of %s: %s", job.id, exc)

    def _run_inner(self, job: Job, seller_ids: list[str] | None = None, *, public_url: str | None = None) -> Job:
        """Drive the job to a terminal state. Never raises on a failed settlement.

        Concurrency: a job that is already mid-flight is refused rather than run
        twice, so two simultaneous settlement requests cannot produce a duplicate
        capture or contradictory terminal states.
        """
        # The atomic claim is the *only* decision about whether this run may
        # proceed — there is deliberately no status pre-check in front of it. A
        # pre-check reads this process's copy and cannot see another worker's
        # claim, and (worse) it rejected the very in-flight states a crashed
        # worker leaves behind, so an expired lease could never be reclaimed
        # through the engine at all. ``claim_run`` allows a job that is runnable
        # or whose lease has expired, and refuses one whose lease is still live,
        # so two live workers cannot both proceed and a hard-killed worker does
        # not brick the job. It returns the freshly claimed record (None if
        # another worker won) so we operate on authoritative state.
        claimed = self.store.claim_run(job, lease_seconds=settings.lease_seconds)
        if claimed is None:
            raise JobAlreadyRunning(
                f"Job {job.id} is already being run (or its lease is still live); "
                f"refusing to run it again"
            )
        # Adopt the authoritative state into the caller's object rather than
        # rebinding, so a run keeps mutating the handle the caller passed in — the
        # contract every other transition relies on. On Postgres the claim returns
        # a freshly loaded row, which is a different object.
        if claimed is not job:
            job.__dict__.update(claimed.__dict__)
        if job.is_deleted:
            raise JobAlreadyRunning(f"Job {job.id} has been deleted; refusing to run it")

        seller_ids = seller_ids or default_seller_ids()
        job.status = JobStatus.NEGOTIATING
        job.append("negotiation_started", "pact", f"Inviting sellers: {', '.join(seller_ids)}")
        self.store.save(job)

        accepted = self.negotiation.negotiate(job, seller_ids)
        score_of = lambda seller_id: self.reputation.get(seller_id)["score"]
        winning = self.negotiation.select_winner(accepted, score_of)
        if winning is None:
            job.status = JobStatus.FAILED
            # The summary must reflect what actually happened. Claiming the budget
            # was too small when every quote fit is what sent the last investigation
            # down the wrong path: "no affordable quote" and "no quote at all" are
            # different conditions and only one of them is about price.
            if not job.offers:
                summary = "No seller produced an offer"
            else:
                cheapest = min(o.price for o in job.offers)
                summary = (
                    f"No seller quoted within budget: cheapest was "
                    f"{job.currency} {cheapest:.2f} against a ceiling of {job.currency} {job.budget_max:.2f}"
                )
            job.append("negotiation_failed", "pact", summary)
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
            self.settlement.hold(job, public_url=public_url)
        except (PayPalError, ValueError) as exc:
            if getattr(exc, "ambiguous", False):
                self.settlement.mark_unresolved(job, str(exc))
            else:
                job.status = JobStatus.FAILED
                job.append("escrow_error", "pact", f"Could not escrow funds: {exc}")
            self.store.save(job)
            return job
        self.store.save(job)

        # Nothing may be delivered unless funds are genuinely held. This covers a
        # real order still awaiting buyer approval, a denied authorization, and an
        # outcome we could not determine — none of which permit work to start.
        if job.escrow.state is not EscrowState.HELD:
            return job

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

        return self.settle_delivery(job)

    def settle_delivery(self, job: Job) -> Job:
        """Verify the stored delivery and settle against it.

        Verification runs against the delivery already attached to this job, so a
        verdict can only ever be about the artifact that was submitted for this
        authorization.
        """
        if job.delivery is None:
            raise ValueError("Cannot settle a job with no delivery")

        # --- verify
        job.status = JobStatus.VERIFYING
        job.append("verification_started", "pact", "Independent verification of the delivery")
        verification = self.verifier.verify(job, job.delivery)
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
            if getattr(exc, "ambiguous", False):
                self.settlement.mark_unresolved(job, str(exc))
            else:
                job.status = JobStatus.FAILED
                job.append("settlement_error", "pact", f"Settlement failed: {exc}")
            self.store.save(job)
            return job

        self.store.save(job)
        return job

    def approve_and_hold(self, job: Job) -> Job:
        """Continue a job whose order the buyer has now approved.

        The approval transition is claimed atomically *before* any provider call,
        so two concurrent approvals cannot both proceed to authorize the buyer's
        funds. A second caller loses the claim and gets a conflict, and a repeated
        request on an already-continued job is likewise refused rather than run
        again.
        """
        if job.escrow.state != EscrowState.AWAITING_APPROVAL:
            raise ValueError(f"Job is not awaiting approval (escrow state {job.escrow.state})")
        if not self.store.claim_approval(job):
            raise ValueError("Buyer approval was already claimed for this job")
        try:
            self.settlement.authorize_hold(job)
        except (PayPalError, ValueError) as exc:
            if not getattr(exc, "ambiguous", False):
                job.status = JobStatus.FAILED
                # Release the claim. PayPal rejected the authorize before the buyer
                # approved, which burns nothing, so the buyer must be able to retry
                # after approving. The claim exists only to stop two approvals
                # racing; once this attempt has definitively failed there is no race
                # to lose.
                job.approval_claimed_at = None
                job.append("escrow_error", "pact", f"Could not escrow funds: {exc}")
            self.store.save(job)
            return job
        self.store.save(job)

        # The buyer has approved and funds are held; produce and settle the work.
        job.status = JobStatus.DELIVERED
        delivery = SellerAgent(self.llm, job.seller_id or "").deliver(job)
        job.delivery = delivery
        job.append(
            "delivery",
            job.seller_id or "seller",
            f"Delivered: {delivery.summary or 'no summary'}",
            chars=len(delivery.artifact),
        )
        self.store.save(job)
        return self.settle_delivery(job)

    def settle_existing(self, job: Job) -> Job:
        """Verify and settle a job that already holds escrow.

        Refuses to touch a job that has already reached a terminal settlement
        state, so a repeat call cannot capture twice.
        """
        if job.escrow.state != EscrowState.HELD or job.delivery is None:
            return job
        return self.settle_delivery(job)

    def reconcile_job(self, job: Job) -> Job:
        """Resolve a job whose settlement outcome was never determined.

        Only reads PayPal to establish what actually happened, so it is safe to
        call repeatedly. The job stays UNRESOLVED if the outcome still cannot be
        established, rather than being guessed either way.
        """
        return self.settlement.reconcile(job)

    def cancel_job(self, job: Job, reason: str = "") -> Job:
        """Unwind a job's financial exposure so it can be safely deleted.

        Delegates to the settlement engine, which voids any outstanding hold (and
        confirms it) rather than discarding it. A job whose money has already
        moved, or whose fate is unknown, is refused — it must be reconciled
        first, never deleted out from under a live authorization.
        """
        return self.settlement.cancel(job, reason=reason)


engine = PactEngine()
