"""Applying verified PayPal webhook events to PACT payment state.

A webhook is a *notification*, not an instruction, and never evidence on its own.
PACT's own reconciliation (which reads PayPal directly) stays authoritative; a
webhook is used to (a) prove a capture actually landed when a response was lost and
(b) surface state changes promptly. This module therefore never moves money: it
reads the provider, then records what the provider says.

The processing rules, in order:

1. **Authenticity.** The caller must have verified the signature through
   ``PayPalEscrowClient.verify_webhook_signature`` before calling here. This module
   takes a ``verified`` flag and refuses to act on an unverified event, so a route
   that forgets the check cannot change state.
2. **Idempotency.** The event id is claimed exactly once in the shared store. A
   duplicate delivery is acknowledged and does nothing.
3. **Relevance.** An event that does not name a job PACT knows is acknowledged and
   ignored — PayPal sends events for an account, not just for this deployment.
4. **Mapping.** Only the event types that bear on escrow are acted on. An unknown
   type is acknowledged and ignored rather than guessed at.
5. **Never a blind retry.** A ``PAYMENT.CAPTURE.COMPLETED`` for a job whose capture
   was ambiguous marks the job captured *from the event's own data plus a
   confirming read*, so no second capture is ever issued.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from ..paypal.escrow import SETTLED_CAPTURE_STATUSES
from .models import IN_FLIGHT_CAPTURE, EscrowState, Job, JobStatus
from .webhook_store import WebhookRecord

log = logging.getLogger("pact.webhooks")

# Event types that bear on a PACT escrow. Anything else is acknowledged and ignored:
# guessing at an unknown event's meaning is how a webhook handler ends up moving
# money on a payload it did not understand.
CAPTURE_COMPLETED = "PAYMENT.CAPTURE.COMPLETED"
CAPTURE_DENIED = "PAYMENT.CAPTURE.DENIED"
CAPTURE_REFUNDED = "PAYMENT.CAPTURE.REFUNDED"
CAPTURE_REVERSED = "PAYMENT.CAPTURE.REVERSED"
AUTHORIZATION_VOIDED = "PAYMENT.AUTHORIZATION.VOIDED"
AUTHORIZATION_CREATED = "PAYMENT.AUTHORIZATION.CREATED"
AUTHORIZATION_EXPIRED = "PAYMENT.AUTHORIZATION.EXPIRED"
ORDER_APPROVED = "CHECKOUT.ORDER.APPROVED"
ORDER_COMPLETED = "CHECKOUT.ORDER.COMPLETED"

HANDLED_EVENT_TYPES = frozenset(
    {
        CAPTURE_COMPLETED,
        CAPTURE_DENIED,
        CAPTURE_REFUNDED,
        CAPTURE_REVERSED,
        AUTHORIZATION_VOIDED,
        AUTHORIZATION_CREATED,
        AUTHORIZATION_EXPIRED,
        ORDER_APPROVED,
        ORDER_COMPLETED,
    }
)


class WebhookOutcome:
    """Result labels, kept as fixed strings so a caller can act without parsing prose."""

    APPLIED = "applied"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    REJECTED = "rejected"
    UNKNOWN_JOB = "unknown_job"
    UNVERIFIED = "unverified"


def extract_event(payload: Any) -> WebhookRecord | None:
    """Read the id, type and (if present) the PACT job id from a webhook body.

    Returns ``None`` when the payload is not shaped like a PayPal event at all, so a
    malformed body is refused before anything else runs. The job id is carried in
    the order's ``custom_id``/``reference_id``, which PACT set to the job id at
    creation, or in ``purchase_units[0].custom_id`` of an order event.
    """
    if not isinstance(payload, dict):
        return None
    event_id = payload.get("id")
    event_type = payload.get("event_type")
    if not isinstance(event_id, str) or not event_id.strip():
        return None
    if not isinstance(event_type, str) or not event_type.strip():
        return None

    resource = payload.get("resource")
    job_id = _job_id_from_resource(resource)
    return WebhookRecord(event_id=event_id.strip(), event_type=event_type.strip(), job_id=job_id)


def _job_id_from_resource(resource: Any) -> str | None:
    """Best-effort extraction of the PACT job id from an event's resource."""
    if not isinstance(resource, dict):
        return None
    # A capture carries custom_id directly; an order carries it on its purchase unit.
    custom = resource.get("custom_id")
    if isinstance(custom, str) and custom:
        return custom
    units = resource.get("purchase_units")
    if isinstance(units, list) and units and isinstance(units[0], dict):
        unit = units[0]
        for key in ("custom_id", "reference_id"):
            value = unit.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def job_id_for_job(job: Job) -> str:
    return job.id


def process_event(
    *,
    payload: Any,
    verified: bool,
    store,
    get_job,
    save_job,
    job_guard=None,
) -> tuple[str, Job | None]:
    """Apply a (possibly unverified) webhook event to payment state.

    ``verified`` must be True only when the signature was confirmed by PayPal. The
    store, the job lookup and the save are injected so this function is testable
    without a running app, and so it holds no global state of its own.

    ``job_guard`` is an optional context manager that serialises work on one job
    across threads and instances (the settlement engine's guard). When supplied, the
    event is applied inside it, so a webhook cannot race a capture the settlement
    engine is mid-way through: the two would otherwise both read ``HELD`` and both
    record a release. When it is not supplied the caller has accepted that
    serialisation is handled elsewhere (or does not apply).

    Returns ``(outcome, job)``. The job is the touched record, or ``None`` when the
    event did not name a job PACT knows (an acknowledgement, not an error).
    """
    record = extract_event(payload)
    if record is None:
        return WebhookOutcome.REJECTED, None

    # Authenticity first: an unverified event must not even be claimed, so a later
    # legitimate delivery of the same id is still processed.
    if not verified:
        log.warning("Refusing an unverified webhook event %s", record.event_id)
        return WebhookOutcome.UNVERIFIED, None

    job = get_job(record.job_id) if record.job_id else None
    if job is None:
        # Acknowledge and ignore: PayPal notifies the account, and this event is not
        # about one of our jobs. Claim it so a redelivery is a cheap no-op.
        store.claim(record)
        store.settle(record.event_id, WebhookOutcome.UNKNOWN_JOB)
        return WebhookOutcome.UNKNOWN_JOB, None

    if record.event_type not in HANDLED_EVENT_TYPES:
        store.claim(record)
        store.settle(record.event_id, WebhookOutcome.IGNORED)
        return WebhookOutcome.IGNORED, job

    # Idempotency: claim exactly once. A duplicate is acknowledged, never re-applied.
    if not store.claim(record):
        return WebhookOutcome.DUPLICATE, job

    def _guarded_apply():
        """Apply the event to the record that will be persisted, and save it here.

        The record that is *mutated* is the record that must be *saved*. On the
        Postgres path the guard hands back the authoritative row, so the event is
        applied to that object; saving it inside the guard is what makes the write
        atomic with respect to a concurrent capture or refund on the same job — a
        save issued after the guard released could clobber a concurrent writer, and
        saving a different object than the one mutated would persist nothing.
        """
        if job_guard is None:
            changed = _apply(job, record, payload)
            if changed:
                save_job(job)
            return job, changed
        with job_guard(job):
            # The guard may hand back the authoritative row (Postgres), which the
            # settlement engine adopts; re-read so the event is applied to the same
            # record a concurrent capture would have written.
            target = get_job(job.id) or job
            changed = _apply(target, record, payload)
            if changed:
                save_job(target)
            return target, changed

    try:
        applied_job, applied = _guarded_apply()
    except Exception as exc:
        # The event is claimed but not applied. Leave the outcome honest; PayPal will
        # redeliver and the claim makes that a no-op, so an operator must reconcile.
        log.error("Failed to apply webhook event %s: %s", record.event_id, type(exc).__name__)
        store.settle(record.event_id, "error")
        raise
    store.settle(record.event_id, WebhookOutcome.APPLIED if applied else WebhookOutcome.IGNORED)
    return (WebhookOutcome.APPLIED if applied else WebhookOutcome.IGNORED), applied_job


def _apply(job: Job, record: WebhookRecord, payload: Any) -> bool:
    """Apply one verified, first-seen event. Returns whether the job changed."""
    event_type = record.event_type

    if event_type == CAPTURE_COMPLETED:
        return _apply_capture_completed(job, payload)
    if event_type in {CAPTURE_DENIED, CAPTURE_REFUNDED, CAPTURE_REVERSED}:
        return _apply_capture_not_completed(job, event_type, payload)
    if event_type == AUTHORIZATION_VOIDED:
        return _apply_authorization_voided(job)
    if event_type == AUTHORIZATION_EXPIRED:
        return _apply_authorization_expired(job)
    if event_type in {ORDER_APPROVED, ORDER_COMPLETED, AUTHORIZATION_CREATED}:
        # Informational: PACT does not authorize on the strength of a webhook. The
        # buyer approval still has to be driven through the approve endpoint, which
        # claims the transition atomically and calls PayPal itself.
        return False
    return False


def _to_amount(value: Any) -> Decimal:
    """Parse a provider amount string to a quantised ``Decimal``; 0.0 on junk.

    A webhook is not a place to raise: an unparseable amount must read as "unknown",
    which the callers treat as "do not settle", never as zero-money-moved success.
    """
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def _capture_from_payload(payload: Any) -> tuple[str, str, str, str]:
    """Read (id, status, value, currency) from a capture event's resource."""
    resource = payload.get("resource") if isinstance(payload, dict) else None
    resource = resource if isinstance(resource, dict) else {}
    amount = resource.get("amount") if isinstance(resource.get("amount"), dict) else {}
    return (
        str(resource.get("id", "")),
        str(resource.get("status", "")),
        str(amount.get("value", "")),
        str(amount.get("currency_code", "")),
    )


def _apply_capture_completed(job: Job, payload: Any) -> bool:
    """Record a capture that PayPal says completed.

    This is the one event that can move a job to SETTLED without an operator, and it
    is safe to do so *only* because the event is verified and the amount and currency
    are checked against the authorization PACT already holds. A mismatch is not
    applied — it is left for reconciliation, because a capture for the wrong amount
    is a signal to investigate, not a settlement to book.
    """
    capture_id, status, value, currency = _capture_from_payload(payload)

    # Already settled by our own path (e.g. the capture call succeeded and the event
    # is just a confirmation). Idempotent: record nothing new.
    if job.escrow.state == EscrowState.RELEASED and job.escrow.capture_id not in {"", capture_id}:
        return False
    if job.escrow.state == EscrowState.RELEASED and job.escrow.capture_id == capture_id:
        return False

    expected_amount = f"{float(job.escrow.amount):.2f}"
    if value and expected_amount != f"{float(value):.2f}":
        job.append(
            "webhook_amount_mismatch",
            "paypal",
            f"Capture event {capture_id} reports {currency} {value} against an "
            f"authorization of {job.currency} {expected_amount}; not applied",
            capture_id=capture_id,
            expected_amount=expected_amount,
            reported_amount=value,
        )
        return True
    if currency and job.currency and currency.upper() != job.currency.upper():
        job.append(
            "webhook_currency_mismatch",
            "paypal",
            f"Capture event {capture_id} reports {currency} against a job in "
            f"{job.currency}; not applied",
            capture_id=capture_id,
            expected_currency=job.currency,
            reported_currency=currency,
        )
        return True

    if status and status.upper() not in SETTLED_CAPTURE_STATUSES and status.upper():
        # A capture event that does not report COMPLETED is not a settlement.
        return False

    # A capture is only meaningful from a state where money could have moved. From
    # NONE or AWAITING_APPROVAL the event is inconsistent with our record; record the
    # observation but do not invent a settlement.
    if job.escrow.state not in {EscrowState.HELD, EscrowState.UNRESOLVED}:
        job.append(
            "webhook_capture_unexpected",
            "paypal",
            f"Capture event {capture_id} arrived while escrow was "
            f"{job.escrow.state.value}; recorded but not settled",
            capture_id=capture_id,
            escrow_state=job.escrow.state.value,
        )
        return True

    # A capture we were mid-way through claiming (the in-flight marker) is *proven*
    # to have landed by this event. That is the one case a lost response could not
    # resolve on its own, and it is safe to settle here because the event is verified
    # and the amount/currency already matched above.
    resolved_in_flight = job.escrow.capture_id == IN_FLIGHT_CAPTURE

    job.escrow.capture_id = capture_id or job.escrow.capture_id
    job.escrow.capture_status = "COMPLETED"
    job.escrow.state = EscrowState.RELEASED
    job.status = JobStatus.SETTLED
    job.append(
        "escrow_released",
        "paypal",
        f"PayPal webhook confirms capture {capture_id} completed for {currency} {value}"
        + (" (resolved an in-flight capture)" if resolved_in_flight else ""),
        capture_id=capture_id,
        status=status or "COMPLETED",
        amount=float(job.escrow.amount),
        mode=job.escrow.mode,
        source="webhook",
        resolved_in_flight=resolved_in_flight,
    )
    return True


def _apply_capture_not_completed(job: Job, event_type: str, payload: Any) -> bool:
    """A capture was denied, refunded or reversed after the fact.

    None of these may silently become a settlement. A refund or reversal of an
    already-released payment moves the job to REFUNDED; a denied capture that never
    landed moves it to UNRESOLVED for an operator, because the safe reading of "the
    money did not move" is to stop, not to settle.
    """
    capture_id, status, value, currency = _capture_from_payload(payload)

    if event_type in {CAPTURE_REFUNDED, CAPTURE_REVERSED}:
        # A refund may be partial. Accumulate the provider-reported amount so a
        # partial refund does not read as a full reversal; the job only becomes
        # REFUNDED once the cumulative refunded total reaches the captured amount.
        # The event is verified, so its amount is trusted here — but it is only ever
        # used to *record* a provider fact, never to move money.
        #
        # For ``PAYMENT.CAPTURE.REFUNDED`` the resource id is the *refund* id. PACT's
        # own Refund API already recorded that refund and counted its amount, and this
        # event is the same money reported again, so a refund id already counted must
        # not be added a second time — otherwise a partial refund reads as a full
        # reversal. A ``REVERSED`` event is a whole-capture reversal, not a refund, so
        # its id is not a refund id and this dedupe does not apply to it.
        refunded = _to_amount(value)
        if event_type == CAPTURE_REFUNDED and capture_id in job.escrow.refund_ids:
            if job.escrow.state != EscrowState.REFUNDED:
                job.append(
                    "webhook_refund_already_recorded",
                    "paypal",
                    f"PayPal webhook reports refund {capture_id}, already recorded by "
                    f"PACT; not counted again",
                    refund_id=capture_id,
                    refunded_total=float(_to_amount(job.escrow.refunded_amount)),
                    source="webhook",
                )
                return True
            return False
        if refunded > 0:
            job.escrow.refunded_amount = _to_amount(job.escrow.refunded_amount) + refunded
        if capture_id:
            # Deliberately not added to ``refund_ids``: that set is the refunds PACT's
            # own API initiated, and only those can be reported a second time by this
            # webhook. A refund seen only through webhooks is counted once per event id
            # (PayPal retries reuse the event id, which the event store already dedupes).
            job.escrow.refund_id = job.escrow.refund_id or capture_id
        job.escrow.refund_status = status or "REFUNDED"

        captured = _to_amount(job.escrow.amount)
        fully_refunded = (
            captured <= 0
            or _to_amount(job.escrow.refunded_amount) >= captured
            or event_type == CAPTURE_REVERSED
        )
        if fully_refunded:
            job.escrow.state = EscrowState.REFUNDED
            job.status = JobStatus.REFUNDED
        job.append(
            "escrow_refunded",
            "paypal",
            f"PayPal webhook reports capture {capture_id} {event_type.split('.')[-1].lower()} "
            f"for {currency} {value}"
            + ("" if fully_refunded else " (partial)"),
            capture_id=capture_id,
            status=status,
            amount=refunded or None,
            refunded_total=float(_to_amount(job.escrow.refunded_amount)),
            partial=not fully_refunded,
            source="webhook",
        )
        return True

    # CAPTURE_DENIED
    if job.escrow.state == EscrowState.RELEASED:
        # A denial after we recorded a release contradicts our record; do not silently
        # flip it. Record the contradiction for reconciliation.
        job.append(
            "webhook_capture_denied_after_release",
            "paypal",
            f"Capture event {capture_id} reports a denial after a recorded release; "
            f"reconciliation required",
            capture_id=capture_id,
            source="webhook",
        )
        return True
    if job.escrow.state == EscrowState.HELD:
        job.escrow.capture_status = "DENIED"
        job.escrow.capture_id = ""
        job.append(
            "capture_failed",
            "paypal",
            f"PayPal webhook reports capture {capture_id} denied; no money moved",
            capture_id=capture_id,
            status=status,
            source="webhook",
        )
        return True
    return False


def _apply_authorization_voided(job: Job) -> bool:
    """A hold was voided at PayPal. If we still think it is held, correct that."""
    if job.escrow.state == EscrowState.HELD:
        job.escrow.state = EscrowState.VOIDED
        job.status = JobStatus.VOIDED
        job.append(
            "escrow_voided",
            "paypal",
            f"PayPal webhook confirms authorization {job.escrow.authorization_id} voided",
            authorization_id=job.escrow.authorization_id,
            source="webhook",
        )
        return True
    return False


def _apply_authorization_expired(job: Job) -> bool:
    """A hold expired uncaptured. It is no longer capturable, so stop pretending it is."""
    if job.escrow.state == EscrowState.HELD:
        job.escrow.state = EscrowState.VOIDED
        job.status = JobStatus.VOIDED
        job.append(
            "escrow_voided",
            "paypal",
            f"PayPal webhook reports authorization {job.escrow.authorization_id} expired "
            f"uncaptured; the hold is gone",
            authorization_id=job.escrow.authorization_id,
            source="webhook",
        )
        return True
    return False
