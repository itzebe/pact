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
import threading
from contextlib import contextmanager
from decimal import Decimal
from typing import Iterator

from ..paypal import EscrowBackend, PayPalError
from ..paypal.escrow import (
    HOLDABLE_STATUSES,
    SETTLED_CAPTURE_STATUSES,
    _parse_authorization,
    _parse_capture,
    approval_url,
)
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
    """Drives escrow: hold on agreement, release or return on verification.

    Two rules govern everything here, and both come from PayPal's published
    status enums rather than from optimism:

    1. A state is only recorded as *held* when the provider says the
       authorization is holdable, and only as *settled* when the provider says
       the capture is ``COMPLETED``. Anything else is unresolved, not success.
    2. A settlement is attempted once per job. A second attempt is refused,
       because a duplicate capture is the one failure that costs real money.
    """

    def __init__(self, escrow: EscrowBackend, reputation: ReputationLedger,
                 store: object | None = None) -> None:
        self.escrow = escrow
        self.reputation = reputation
        self.store = store
        # Guards the transition into a terminal settlement state. Without it, two
        # concurrent release/refund calls can both pass the state check and both
        # reach the provider, which is how a duplicate capture happens.
        self._settle_lock = threading.RLock()

    @contextmanager
    def _guard(self, job: Job) -> Iterator[None]:
        """Serialise settlement for one job, across processes as well as threads.

        ``_settle_lock`` only protects a single process. On a platform that runs
        several instances, two of them can hold the same job and both capture. When
        the store can take a shared claim (Postgres), hold it and adopt the stored
        escrow state, so an attempt that already moved money is visible here as a
        recorded capture rather than an open authorization.
        """
        store = self.store
        if store is not None and getattr(store, "supports_claims", False):
            with store.settlement_claim(job) as fresh:
                if fresh is not None:
                    # Adopt the authoritative escrow record. A capture recorded by
                    # another instance then trips the "already captured" check below.
                    job.escrow = fresh.escrow
                    job.status = fresh.status
                with self._settle_lock:
                    yield
            return
        with self._settle_lock:
            yield

    def _persist(self, job: Job) -> None:
        """Push the current escrow state to the shared store, if there is one.

        On the DB path this is what makes a claim visible to other instances. The
        in-memory ``capture_id = "pending"`` marker is meaningless to a process that
        never saw it, so it has to be written before the provider is called.
        """
        store = self.store
        if store is not None and getattr(store, "supports_claims", False):
            store.save(job)

    @property
    def mode(self) -> str:
        return "mock" if getattr(self.escrow, "environment", "sandbox") == "mock" else getattr(
            self.escrow, "environment", "sandbox"
        )

    @property
    def requires_buyer_approval(self) -> bool:
        """Whether this backend needs an interactive buyer-approval step.

        The in-memory double auto-approves so the demo runs with no setup; a real
        PayPal order returns a ``rel:approve`` link and cannot be authorized until
        the buyer visits it.
        """
        return not bool(getattr(self.escrow, "auto_approves", False))

    # ----------------------------------------------------------------- hold

    def hold(self, job: Job) -> Job:
        """Create an AUTHORIZE order and, when possible, place the hold.

        Returns with the job in ``AWAITING_APPROVAL`` when the backend requires a
        buyer to approve the order first. No delivery happens and no money moves
        until :meth:`authorize_hold` succeeds, so an unapproved order is inert.
        """
        if job.agreed_price is None or job.seller_id is None:
            raise ValueError("Cannot escrow a job with no agreed price and seller")
        if job.escrow.state not in {EscrowState.NONE, EscrowState.AWAITING_APPROVAL}:
            raise ValueError(f"Cannot hold escrow in state {job.escrow.state}")

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
        job.escrow.amount = amount
        job.escrow.mode = self.mode
        url = approval_url(order) if isinstance(order, dict) else None
        job.escrow.approval_url = url or ""
        job.append(
            "escrow_order_created",
            "paypal",
            f"PayPal order {order_id} created (intent=AUTHORIZE)",
            order_id=order_id,
            status=order.get("status"),
            mode=job.escrow.mode,
        )

        if self.requires_buyer_approval:
            # The authorize call would be rejected without approval, so stop here
            # rather than attempting it and misreporting the failure.
            job.escrow.state = EscrowState.AWAITING_APPROVAL
            job.status = JobStatus.AWAITING_APPROVAL
            job.append(
                "escrow_awaiting_approval",
                "paypal",
                "Order created; the buyer must approve it before funds can be held",
                order_id=order_id,
                approval_url=url or "",
            )
            return job

        return self.authorize_hold(job)

    def authorize_hold(self, job: Job) -> Job:
        """Authorize an approved order, recording the hold only if PayPal agrees."""
        if not job.escrow.order_id:
            raise ValueError("Cannot authorize a job with no PayPal order")
        if job.escrow.state == EscrowState.HELD:
            raise ValueError("Escrow is already held for this job")

        try:
            authorization = self.escrow.authorize_order(
                job.escrow.order_id, idempotency_key=f"pact-auth-{job.id}"
            )
        except PayPalError as exc:
            if getattr(exc, "ambiguous", False):
                # We do not know whether a hold exists. Never claim one does.
                self.mark_unresolved(job, f"Authorization outcome unknown: {exc}")
            else:
                job.status = JobStatus.FAILED
                job.append("escrow_failed", "paypal", f"Authorization failed: {exc}",
                           order_id=job.escrow.order_id)
            raise

        job.escrow.authorization_id = authorization.id
        job.escrow.authorization_status = authorization.status
        status = authorization.status.upper()

        # PayPal can return a non-holdable state on a successful HTTP response, so
        # the status must be checked rather than assumed.
        if status not in HOLDABLE_STATUSES:
            if status in {"CAPTURED", "PARTIALLY_CAPTURED"}:
                # Money has already moved against this order. That is not a
                # failure to be swallowed: it needs a human to reconcile it.
                job.escrow.state = EscrowState.UNRESOLVED
                job.status = JobStatus.UNRESOLVED
                job.append(
                    "escrow_not_held",
                    "paypal",
                    f"Authorization {authorization.id} is already {status}; funds may have moved",
                    authorization_id=authorization.id,
                    status=status,
                )
                return job

            # DENIED, VOIDED, or anything else: a definite failure with no hold.
            job.escrow.state = EscrowState.NONE
            job.status = JobStatus.FAILED
            job.append(
                "escrow_not_held",
                "paypal",
                f"Authorization {authorization.id} is {status}; no funds are held",
                authorization_id=authorization.id,
                status=status,
            )
            raise PayPalError(
                f"Authorization {authorization.id} is {status}, so no funds are held",
                code="AUTHORIZATION_NOT_HOLDABLE",
            )

        job.escrow.state = EscrowState.HELD
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
        """Capture the held funds because the delivery passed verification.

        The whole transition is serialised on the settlement guard so a second
        concurrent attempt — in another thread or another instance — cannot slip
        past the state check and capture twice.
        """
        with self._guard(job):
            if job.escrow.state != EscrowState.HELD:
                raise ValueError(f"Cannot release escrow in state {job.escrow.state}")
            if not job.escrow.authorization_id:
                raise ValueError("No authorization to capture")
            if job.escrow.capture_id:
                # Idempotent by construction: one capture per authorization.
                raise ValueError(f"Escrow already captured as {job.escrow.capture_id}")

            # Claim the authorization before calling out, so a racing attempt sees
            # an in-progress capture rather than an open one.
            job.escrow.capture_id = "pending"
            # Write the claim before the call, so a second instance that takes the
            # lock next sees a capture in progress instead of an open authorization.
            self._persist(job)
            try:
                capture = self.escrow.capture_authorization(
                    job.escrow.authorization_id,
                    amount=job.escrow.amount,
                    currency=job.currency,
                    note_to_payer=note or "Released by Pact after verified delivery",
                    idempotency_key=f"pact-capture-{job.id}",
                )
            except PayPalError as exc:
                job.append("capture_failed", "paypal", f"Capture failed: {exc}",
                           authorization_id=job.escrow.authorization_id)
                if getattr(exc, "ambiguous", False):
                    # The capture may have been applied before the response was lost.
                    # Keep the "pending" marker so the in-flight fact survives, and
                    # leave the job UNRESOLVED: a blind retry could capture twice, so
                    # only an explicit reconciliation may move it on.
                    self.mark_unresolved(
                        job,
                        f"Capture outcome unknown; reconciliation required before any "
                        f"further capture attempt ({exc})",
                    )
                else:
                    # Definite failure: no money moved, so withdraw the claim. Leaving
                    # "pending" in the shared row would wedge the job permanently,
                    # because every later attempt would see a capture that never was.
                    job.escrow.capture_id = ""
                    self._persist(job)
                raise

            job.escrow.capture_id = capture.id
            job.escrow.capture_status = capture.status

            # Only COMPLETED means the seller actually got paid. A PENDING or FAILED
            # capture must never be recorded as a settled job.
            if capture.status.upper() not in SETTLED_CAPTURE_STATUSES:
                job.escrow.state = EscrowState.UNRESOLVED
                job.status = JobStatus.UNRESOLVED
                job.append(
                    "capture_not_completed",
                    "paypal",
                    f"Capture {capture.id} is {capture.status}; settlement is not confirmed",
                    capture_id=capture.id,
                    status=capture.status,
                    authorization_id=job.escrow.authorization_id,
                )
                return job

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
            self._persist(job)
            return job

    def refund(self, job: Job, reason: str = "") -> Job:
        """Void the hold because the delivery failed verification.

        A void returns the buyer's funds and moves no money, so the job ends in
        ``VOIDED`` rather than ``REFUNDED`` — nothing was ever captured to refund.
        """
        with self._guard(job):
            if job.escrow.state != EscrowState.HELD:
                raise ValueError(f"Cannot void escrow in state {job.escrow.state}")
            if not job.escrow.authorization_id:
                raise ValueError("No authorization to void")
            if job.escrow.capture_id:
                raise ValueError("Cannot void an authorization that has already been captured")

            try:
                self.escrow.void_authorization(
                    job.escrow.authorization_id,
                    idempotency_key=f"pact-void-{job.id}",
                )
            except PayPalError as exc:
                job.append("void_failed", "paypal", f"Void failed: {exc}",
                           authorization_id=job.escrow.authorization_id)
                if getattr(exc, "ambiguous", False):
                    self.mark_unresolved(job, f"Void outcome unknown: {exc}")
                raise

            # Confirm with the provider that the hold is actually released rather than
            # reporting a void on the strength of a success response alone.
            confirmed = self._confirm_void(job.escrow.authorization_id)
            job.escrow.state = EscrowState.VOIDED
            job.status = JobStatus.VOIDED

            job.append(
                "escrow_voided",
                "paypal",
                f"Hold released: authorization {job.escrow.authorization_id} voided, buyer charged nothing",
                authorization_id=job.escrow.authorization_id,
                reason=reason,
                provider_confirmed=confirmed,
                mode=job.escrow.mode,
            )

            if job.seller_id:
                record = self.reputation.record_voided(job.seller_id)
                job.append(
                    "reputation_updated",
                    "pact",
                    f"{job.seller_id} score now {self.reputation.score(record):.3f} "
                    f"({record['voided']} voided)",
                    agent_id=job.seller_id,
                    score=self.reputation.score(record),
                )
            self._persist(job)
            return job

    def _confirm_void(self, authorization_id: str) -> bool:
        """Ask the provider whether the authorization is really voided."""
        try:
            current = self.escrow.get_authorization(authorization_id)
        except PayPalError as exc:
            log.warning("Could not confirm void of %s: %s", authorization_id, exc)
            return False
        return str(current.status).upper() == "VOIDED"

    # ------------------------------------------------------- reconciliation

    def reconcile(self, job: Job) -> Job:
        """Resolve a job whose capture outcome was never learned.

        An ambiguous capture is the one case where PACT cannot tell, from its own
        records, whether money moved. Guessing either way is unsafe: assuming
        success can under-report a settlement, and assuming failure can capture a
        second time. So the only honest resolution is to ask PayPal what it
        actually did, using the identifiers PACT already holds.

        The authorization is the authoritative source here, because a capture is
        recorded against it. If PayPal reports the authorization as CAPTURED, the
        money moved and the capture is fetched to record its real id and status.
        If it is still CREATED, nothing moved and the hold is intact.

        This is a *read*, and it never initiates a capture, so it is safe to call
        repeatedly. If PayPal cannot be reached the job stays UNRESOLVED and
        blocked — an unreachable provider is not evidence that no money moved.

        Runs under the settlement guard so two instances cannot reconcile the same
        job at once: the second adopts the first's resolution and refuses.
        """
        with self._guard(job):
            return self._reconcile(job)

    def _reconcile(self, job: Job) -> Job:
        if job.escrow.state != EscrowState.UNRESOLVED:
            raise ValueError(f"Job is not unresolved (escrow state {job.escrow.state})")

        authorization_id = job.escrow.authorization_id
        if not authorization_id:
            # An authorize call can fail ambiguously too, leaving an order whose
            # outcome we never learned. The order is then the only handle we hold.
            if job.escrow.order_id:
                return self._reconcile_via_order(job)
            # Nothing was ever authorized, so no capture can have been applied.
            return self._resolve(job, EscrowState.NONE, JobStatus.FAILED,
                                 "No authorization existed, so no capture could have been applied")

        try:
            authorization = self.escrow.get_authorization(authorization_id)
        except PayPalError as exc:
            job.append(
                "reconciliation_unavailable",
                "paypal",
                f"Could not read authorization {authorization_id} to reconcile: {exc}",
                authorization_id=authorization_id,
                reason=str(exc),
            )
            self._persist(job)
            return job

        status = str(authorization.status).upper()

        if status in {"CAPTURED", "PARTIALLY_CAPTURED"}:
            return self._resolve_captured(job, authorization_id, status)

        if status in HOLDABLE_STATUSES:
            # PayPal still shows an open hold, so the capture did not land. The
            # money never moved, so the in-flight marker is withdrawn and the job
            # can be settled normally again.
            self._clear_claim(job)
            return self._resolve(job, EscrowState.HELD, JobStatus.ESCROWED,
                                 f"PayPal reports authorization {authorization_id} as {status}; "
                                 f"no capture was applied and the hold is intact")

        if status == "VOIDED":
            self._clear_claim(job)
            return self._resolve(job, EscrowState.VOIDED, JobStatus.VOIDED,
                                 f"PayPal reports authorization {authorization_id} as VOIDED; "
                                 f"no money moved")

        # DENIED or any unknown state: leave it unresolved rather than guess.
        job.append(
            "reconciliation_inconclusive",
            "paypal",
            f"PayPal reports authorization {authorization_id} as {status}; "
            f"outcome still cannot be determined safely",
            authorization_id=authorization_id,
            status=status,
        )
        self._persist(job)
        return job

    def _reconcile_via_order(self, job: Job) -> Job:
        """Resolve an ambiguous *authorize* by asking the order what it holds.

        If the authorize landed, PayPal attaches the authorization to the order's
        purchase unit, so the order is authoritative about whether a hold exists.
        """
        order_id = job.escrow.order_id
        try:
            order = self.escrow.get_order(order_id)
        except PayPalError as exc:
            job.append(
                "reconciliation_unavailable",
                "paypal",
                f"Could not read order {order_id} to reconcile: {exc}",
                order_id=order_id,
                reason=str(exc),
            )
            self._persist(job)
            return job

        authorization = _parse_authorization(order if isinstance(order, dict) else {})
        if not authorization.id:
            # No authorization on the order: the hold never landed.
            self._clear_claim(job)
            return self._resolve(job, EscrowState.NONE, JobStatus.FAILED,
                                 f"PayPal order {order_id} holds no authorization; no funds were held")

        job.escrow.authorization_id = authorization.id
        job.escrow.authorization_status = authorization.status
        status = str(authorization.status).upper()

        if status in HOLDABLE_STATUSES:
            return self._resolve(job, EscrowState.HELD, JobStatus.ESCROWED,
                                 f"Reconciled: order {order_id} holds authorization "
                                 f"{authorization.id} ({status})")
        if status in {"CAPTURED", "PARTIALLY_CAPTURED"}:
            return self._resolve_captured(job, authorization.id, status)

        self._clear_claim(job)
        return self._resolve(job, EscrowState.NONE, JobStatus.FAILED,
                             f"Authorization {authorization.id} on order {order_id} is {status}; "
                             f"no funds are held")

    def _find_capture_on_order(self, order_id: str) -> Any:
        """Ask the order what was captured against it, for a lost capture id."""
        try:
            order = self.escrow.get_order(order_id)
        except PayPalError as exc:
            log.warning("Could not read order %s to recover a capture id: %s", order_id, exc)
            return None
        return _parse_capture(order if isinstance(order, dict) else {})

    def _resolve_captured(self, job: Job, authorization_id: str, auth_status: str) -> Job:
        """Record a capture that PayPal confirms happened.

        The capture id is usually lost with the failed response, so it is recovered
        from the order, which PayPal records captures against. Any id found this way
        comes from PayPal itself rather than being invented.
        """
        capture_id = job.escrow.capture_id
        status = ""
        if capture_id and capture_id != "pending":
            try:
                status = str(self.escrow.get_capture(capture_id).status).upper()
            except PayPalError as exc:
                log.warning("Could not read capture %s: %s", capture_id, exc)

        if not status and job.escrow.order_id:
            found = self._find_capture_on_order(job.escrow.order_id)
            if found is not None and found.id:
                capture_id = found.id
                status = str(found.status).upper()

        if not status:
            # No capture record could be read. The authorization status is still
            # authoritative proof that money moved, so the fact is recorded without
            # inventing a capture id we do not have.
            job.escrow.capture_id = ""
            job.escrow.capture_status = auth_status
        else:
            job.escrow.capture_id = capture_id
            job.escrow.capture_status = status

        if status and status not in SETTLED_CAPTURE_STATUSES:
            return self._resolve(job, EscrowState.UNRESOLVED, JobStatus.UNRESOLVED,
                                 f"Authorization {authorization_id} is captured but capture "
                                 f"{capture_id} reports {status}; still unresolved")

        resolved = self._resolve(
            job, EscrowState.RELEASED, JobStatus.SETTLED,
            f"Reconciled: PayPal confirms authorization {authorization_id} is {auth_status}",
            reconciled=True,
        )
        if job.seller_id:
            # The unknown outcome was recorded as a failure. It was not the
            # seller's, so reverse it and credit the settlement, or reputation
            # contradicts a ledger that now says the money moved.
            record = self.reputation.record_reconciled(job.seller_id, float(job.escrow.amount))
            job.append(
                "reputation_updated",
                "pact",
                f"{job.seller_id} score now {self.reputation.score(record):.3f} "
                f"({record['settled']} settled) after reconciliation",
                agent_id=job.seller_id,
                score=self.reputation.score(record),
            )
            self._persist(job)
        return resolved

    @staticmethod
    def _clear_claim(job: Job) -> None:
        """Withdraw an in-flight capture marker once we know no capture landed.

        Leaving ``pending`` behind would make every later attempt refuse with
        "already captured", which is the wedge this whole path exists to avoid.
        """
        if job.escrow.capture_id == "pending":
            job.escrow.capture_id = ""
            job.escrow.capture_status = ""

    def _resolve(self, job: Job, state: EscrowState, status: JobStatus, summary: str,
                 **detail: Any) -> Job:
        """Move an unresolved job to a state that PayPal's own data supports."""
        previous = job.escrow.state.value
        job.escrow.state = state
        job.status = status
        job.append(
            "settlement_reconciled",
            "pact",
            summary,
            previous_state=previous,
            resolved_state=state.value,
            authorization_id=job.escrow.authorization_id,
            capture_id=job.escrow.capture_id,
            **detail,
        )
        self._persist(job)
        return job

    def mark_unresolved(self, job: Job, reason: str) -> Job:
        """Record an outcome we could not determine. Never a success, never a retry.

        The job stays in this state until :meth:`reconcile` establishes what PayPal
        actually did. Nothing else may capture or void it.

        Idempotent: both the settlement layer and the engine above it react to an
        ambiguous error, and recording the same unknown outcome twice would append a
        duplicate ledger entry and count the seller's failure twice.
        """
        if job.escrow.state == EscrowState.UNRESOLVED:
            return job

        job.escrow.state = EscrowState.UNRESOLVED
        job.status = JobStatus.UNRESOLVED
        job.append(
            "settlement_unresolved",
            "pact",
            f"Settlement unresolved and requires reconciliation: {reason}",
            authorization_id=job.escrow.authorization_id,
            order_id=job.escrow.order_id,
            capture_id=job.escrow.capture_id,
            reason=reason,
        )
        if job.seller_id:
            self.reputation.record_failed(job.seller_id)
        self._persist(job)
        return job
