"""HTTP API for Pact.

Exposes the job lifecycle, the audit ledger, reputation, and a server-sent
event stream so the UI can follow a settlement as it happens rather than
polling for a finished result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from decimal import Decimal
from typing import Annotated, Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..config import settings
from ..core.engine import JobAlreadyRunning, RunDeadlineExceeded, ServerBusy, engine
from ..core.models import EscrowState, Job
from ..core.negotiation import default_seller_ids
from ..core.store import TERMINAL_ESCROW_STATES
from ..paypal import MockPayPalEscrowClient, PayPalError, approval_url
from .auth import (
    STREAM_TICKET_TTL,
    Principal,
    assert_can_access_job,
    issue_stream_ticket,
    require_admin,
    require_buyer,
    require_principal,
    require_stream_principal,
)

log = logging.getLogger("pact.api")

# Route protection is declared once, at the router level, so a *future* handler
# cannot accidentally ship unauthenticated. Two routers exist because two routes
# genuinely differ:
#
# * ``public_router`` — liveness only. A monitor cannot present a credential, and
#   /health exposes no money data.
# * ``router`` — everything that reads or moves money. The bearer-token
#   requirement is a router-level dependency, so it is inherited, not repeated.
# * ``stream_router`` — the SSE endpoint, which cannot send an Authorization
#   header (EventSource), so it verifies the same token from a query parameter
#   instead. It is deliberately NOT under the header-only dependency, or the
#   query-token path would be dead code.
public_router = APIRouter(prefix="/api")
router = APIRouter(prefix="/api", dependencies=[Depends(require_principal)])
stream_router = APIRouter(prefix="/api")

# Bounds that keep a single request from turning into unbounded work. These are
# enforced at the edge so no oversized payload ever reaches the engine.
MAX_CRITERIA = 20
MAX_CRITERION_CHARS = 300
MAX_BUYER_ID_CHARS = 64
MAX_SELLERS_PER_RUN = 10
MAX_BUDGET = Decimal("1000000")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class JobCreate(BaseModel):
    """Job input, tightly bounded.

    ``buyer_id`` is accepted for backward compatibility but is *overwritten* from
    the authenticated principal, so a caller can never post a job as someone else.
    """

    title: str = Field(..., min_length=3, max_length=200)
    brief: str = Field(..., min_length=3, max_length=4000)
    acceptance_criteria: list[
        Annotated[str, Field(min_length=1, max_length=MAX_CRITERION_CHARS)]
    ] = Field(..., min_length=1, max_length=MAX_CRITERIA)
    budget_max: Decimal = Field(..., gt=0, le=MAX_BUDGET)
    buyer_id: str = Field(default="buyer", max_length=MAX_BUYER_ID_CHARS)
    seller_ids: list[
        Annotated[str, Field(min_length=1, max_length=64)]
    ] | None = Field(default=None, max_length=MAX_SELLERS_PER_RUN)


class SellerInfo(BaseModel):
    agent_id: str
    score: float
    settled: int
    voided: int
    failed: int
    volume: float


def _job_or_404(job_id: str) -> Job:
    """Fetch a job, or 404. A tombstoned job is 404, exactly like a missing one."""
    job = engine.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")
    return job


def _job_for(principal: Principal, job_id: str) -> Job:
    """Fetch a job the principal is allowed to see.

    A caller who is not party to the job gets 404 rather than 403, so the
    endpoint does not confirm the existence of other users' jobs. (A seller with
    a valid credential but no relationship to the job is treated the same way.)
    """
    job = engine.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")
    try:
        assert_can_access_job(principal, job)
    except HTTPException as exc:
        # 403 from the ownership check becomes a 404 here: do not leak existence.
        if exc.status_code == 403:
            raise HTTPException(status_code=404, detail=f"No job with id {job_id}") from exc
        raise
    return job


def _owned_job(principal: Principal, job_id: str) -> Job:
    """Fetch a job the principal owns *as a buyer*.

    Destructive and financial actions (run, approve, delete) require the caller
    to be the owning buyer or an admin. A seller may observe a job they are party
    to, but may not drive its settlement.
    """
    job = engine.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")
    if principal.is_admin:
        return job
    if principal.is_buyer and job.buyer_id == principal.subject:
        return job
    raise HTTPException(status_code=403, detail="You are not authorized to act on this job")


@public_router.get("/health")
def health() -> dict[str, Any]:
    store = engine.store
    storage: dict[str, Any] = {
        "backend": getattr(store, "backend", "unknown"),
        "durable": bool(getattr(store, "durable", False)),
    }
    # When the configured durable backend could not be opened, say why here rather
    # than letting every route fail with an unexplained 500.
    if getattr(store, "backend", "") == "unavailable":
        storage["error"] = str(getattr(store, "error", "unavailable"))
        storage["settlement_disabled"] = True

    settlement_real = engine.settlement_mode not in {"mock", ""}

    return {
        "status": "degraded" if storage.get("settlement_disabled") else "ok",
        "paypal": {
            "configured": settings.paypal.configured,
            "environment": settings.paypal.environment,
            "mode": engine.settlement_mode,
            "require_real": settings.require_real_paypal,
            # Whether this process will move real money. False means the mock is
            # in use, and any settlement it reports is simulated.
            "settlement_is_real": settlement_real,
        },
        "llm": {
            "configured": settings.llm.configured,
            "model": engine.llm.name,
            "is_mock": bool(getattr(engine.llm, "is_mock", False)),
        },
        "environment": settings.environment,
        "is_production": settings.is_production,
        "auth": {
            # Never expose the secret. Only whether auth is configured, so an
            # operator can see at a glance that the API will refuse requests.
            "configured": settings.auth.configured,
        },
        "currency": settings.currency,
        "sellers": default_seller_ids(),
        # Which persistence is actually in use, so a deployment can never be
        # mistaken for durable when it is not.
        "storage": storage,
    }


def _visible_jobs(principal: Principal, limit: int) -> list[Job]:
    """Jobs the principal may see: their own, jobs they are party to, or all for an admin.

    The filter is pushed into the store and applied before the limit, so a
    non-owner's job is never loaded and a seller's older jobs are not silently
    dropped once there are more jobs than the limit.
    """
    if principal.is_admin:
        return engine.store.list(limit=limit)
    if principal.is_buyer:
        return engine.store.list(limit=limit, buyer_id=principal.subject)
    return engine.store.list(limit=limit, seller_id=principal.subject)


@router.get("/jobs")
def list_jobs(
    principal: Annotated[Principal, Depends(require_principal)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """List jobs visible to the caller."""
    jobs = _visible_jobs(principal, limit)
    return {"count": len(jobs), "jobs": [_job_summary(j) for j in jobs]}


@router.post("/jobs", status_code=201)
async def create_job(
    payload: JobCreate,
    principal: Annotated[Principal, Depends(require_buyer)],
) -> dict[str, Any]:
    """Post a job. The buyer identity comes from the token, never the body."""
    job = engine.create_job(
        title=payload.title,
        brief=payload.brief,
        criteria=payload.acceptance_criteria,
        budget_max=payload.budget_max,
        buyer_id=principal.subject,
        currency=settings.currency,
    )
    return _job_full(job)


def _parse_seller_ids(raw: list[str] | None) -> list[str] | None:
    """Flatten a ``seller_ids`` query value that may be comma-separated or repeated.

    ``Query`` is required: a bare ``list[str]`` on a POST route is parsed as a
    *body* field, so the query parameter is dropped silently and every run would
    invite the default sellers regardless of what the caller asked for.
    """
    if not raw:
        return None
    out: list[str] = []
    for chunk in raw:
        for part in str(chunk).split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out or None


def _public_base(request: Request) -> str:
    """The deployment's own base URL, for building PayPal return/cancel links.

    ``PACT_PUBLIC_URL`` is the canonical origin and always wins. It is the only
    source that is trusted for a security-sensitive provider URL.

    The request's own host is used only as a *fallback*, and only when it matches
    the configured ``PACT_ALLOWED_HOSTS`` allowlist. A forged ``Host`` or
    ``X-Forwarded-Host`` therefore cannot redirect the buyer to an attacker's
    domain after approval: an unknown host yields no base URL at all, and the
    PayPal order is created without a return_url rather than with a hostile one.
    """
    if settings.public_url:
        return settings.public_url

    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").strip()
    if not host:
        return ""
    # Strip any port before comparing, and compare case-insensitively.
    hostname = host.split(":", 1)[0].lower()
    if hostname not in settings.allowed_hosts:
        log.warning("Refusing to build a return_url from untrusted host %r", host)
        return ""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if proto not in {"http", "https"}:
        proto = "https"
    return f"{proto}://{host}"


@router.post("/jobs/{job_id}/run")
async def run_job(
    job_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_buyer)],
    seller_ids: list[str] | None = Query(default=None),
) -> dict[str, Any]:
    """Run the full lifecycle. Executed off the event loop so it cannot block it.

    Only the owning buyer (or an admin) may run a job. A job that is already
    mid-flight is refused with 409 rather than run twice, so two concurrent
    requests cannot produce a duplicate capture. A job whose lease has expired is
    reclaimable, so an interrupted run is not wedged forever.
    """
    job = _owned_job(principal, job_id)

    requested = _parse_seller_ids(seller_ids) or default_seller_ids()
    if len(requested) > MAX_SELLERS_PER_RUN:
        raise HTTPException(status_code=422, detail="Too many sellers requested")
    known = set(default_seller_ids())
    unknown = [s for s in requested if s not in known or not IDENTIFIER.match(s)]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown seller(s): {', '.join(unknown)}")

    try:
        completed = await asyncio.to_thread(
            engine.run, job, requested, public_url=_public_base(request)
        )
    except JobAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServerBusy as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "2"},
        ) from exc
    except RunDeadlineExceeded as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    return _job_full(completed)


@router.post("/jobs/{job_id}/approve")
async def approve_job(
    job_id: str,
    principal: Annotated[Principal, Depends(require_buyer)],
) -> dict[str, Any]:
    """Continue a job after the buyer has approved the PayPal order.

    Only meaningful in real PayPal mode, where an order cannot be authorized until
    the buyer visits the approval URL. In simulated mode the order is authorized
    immediately and this returns 409.

    The approval is claimed atomically in the engine, so concurrent requests
    produce exactly one continuation; the losers get 409 rather than a 500.
    """
    job = _owned_job(principal, job_id)
    if job.escrow.state is not EscrowState.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"Job is not awaiting buyer approval (escrow state {job.escrow.state.value})",
        )
    try:
        completed = await asyncio.to_thread(engine.approve_and_hold, job)
    except ValueError as exc:
        # A lost approval claim (or a state change under us) is a conflict, not a
        # server error.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _job_full(completed)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    principal: Annotated[Principal, Depends(require_buyer)],
) -> dict[str, Any]:
    """Unwind a job's financial exposure so it can be safely deleted.

    Voids any outstanding hold (confirming it with the provider) rather than
    discarding it. A job whose money has already moved, or whose fate is unknown,
    is refused with 409 and must be reconciled first. Idempotent.
    """
    job = _owned_job(principal, job_id)
    try:
        cancelled = await asyncio.to_thread(engine.cancel_job, job, "Cancelled by buyer")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PayPalError as exc:
        # The provider could not be reached or refused the void. The job has been
        # left in a safe (unresolved) state; the caller must not read this as a
        # cancellation.
        raise HTTPException(
            status_code=502,
            detail={"provider_error": exc.code or "PAYPAL_ERROR", "message": str(exc)[:300]},
        ) from exc
    return _job_full(cancelled)


@router.post("/jobs/{job_id}/reconcile")
async def reconcile_job(
    job_id: str,
    principal: Annotated[Principal, Depends(require_admin)],
) -> dict[str, Any]:
    """Resolve a job whose settlement outcome was never determined.

    Only reads PayPal to establish what actually happened, so it is safe to call
    repeatedly. Returns 409 for a job that is not in the unresolved state.
    Reconciliation is an administrative act — it can settle real money — so it
    requires an admin credential rather than the buyer's own.
    """
    job = _job_or_404(job_id)
    if job.escrow.state is not EscrowState.UNRESOLVED:
        raise HTTPException(
            status_code=409,
            detail=f"Job is not unresolved (escrow state {job.escrow.state.value})",
        )
    reconciled = await asyncio.to_thread(engine.reconcile_job, job)
    return _job_full(reconciled)


_EMAIL = re.compile(r"([^@\s]{1,64})@([^@\s]+\.[A-Za-z]{2,})")


def _redact(value: Any) -> Any:
    """Mask email local parts anywhere in a provider payload.

    Order details are useful for diagnosing a stuck approval, but the merchant and
    payer identities in them are not ours to republish. Keeping the domain (which
    distinguishes sandbox from live) and dropping the mailbox is enough.
    """
    if isinstance(value, str):
        return _EMAIL.sub(lambda m: f"{m.group(1)[:1]}***@{m.group(2)}", value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


@router.get("/jobs/{job_id}/paypal-order")
async def get_paypal_order(
    job_id: str,
    principal: Annotated[Principal, Depends(require_admin)],
) -> dict[str, Any]:
    """Read the live PayPal order behind a job. Read-only; moves no money.

    Exists so a stuck buyer approval can be diagnosed from the provider's own
    record (status, intent, amount, payee) instead of inferring it from the
    approval link. Identity fields are redacted. Administrative: it reaches the
    provider with the deployment's own credentials.
    """
    job = _job_or_404(job_id)
    if not job.escrow.order_id:
        raise HTTPException(status_code=409, detail="Job has no PayPal order")
    if isinstance(engine.escrow, MockPayPalEscrowClient):
        raise HTTPException(
            status_code=409,
            detail="Order details require a real PayPal client; this deployment is simulated",
        )
    try:
        order = await asyncio.to_thread(engine.escrow.get_order, job.escrow.order_id)
    except PayPalError as exc:
        raise HTTPException(
            status_code=502,
            detail={"provider_error": exc.code or "PAYPAL_ERROR", "message": str(exc)[:300]},
        ) from exc

    order = order if isinstance(order, dict) else {}
    live_url = approval_url(order)
    return {
        "job_id": job.id,
        "order_id": job.escrow.order_id,
        "environment": getattr(engine.escrow, "environment", ""),
        "intent": order.get("intent"),
        "status": order.get("status"),
        "purchase_units": _redact(order.get("purchase_units") or []),
        "approval_url": live_url,
        "approval_url_matches_stored": bool(live_url) and live_url == job.escrow.approval_url,
        "links": [{"rel": link.get("rel")} for link in (order.get("links") or []) if isinstance(link, dict)],
    }


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    principal: Annotated[Principal, Depends(require_principal)],
) -> dict[str, Any]:
    return _job_full(_job_for(principal, job_id))


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(
    job_id: str,
    principal: Annotated[Principal, Depends(require_admin)],
) -> None:
    """Tombstone a job, but only once its financial exposure is fully resolved.

    A job with a live authorization, an in-flight capture, or an unknown outcome
    must never disappear: deleting it would strand the buyer's funds and destroy
    the only record of the authorization. The caller must cancel (or reconcile)
    it first, which the error message says explicitly. Deletion is an
    administrative act, so it needs an admin credential.
    """
    job = _job_or_404(job_id)
    if job.escrow.state not in TERMINAL_ESCROW_STATES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Refusing to delete: escrow is {job.escrow.state.value}. "
                f"Cancel the job (POST /api/jobs/{job_id}/cancel) to void any hold "
                f"first, or reconcile it if the outcome is unknown."
            ),
        )
    if not engine.store.delete(job_id):
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")


@router.get("/jobs/{job_id}/ledger")
def get_ledger(
    job_id: str,
    principal: Annotated[Principal, Depends(require_principal)],
) -> dict[str, Any]:
    job = _job_for(principal, job_id)
    return {
        "job_id": job.id,
        "entries": [e.model_dump(mode="json") for e in job.ledger],
        "chain_valid": job.verify_chain(),
        "entry_count": len(job.ledger),
    }


@router.post("/jobs/{job_id}/stream-ticket")
def create_stream_ticket(
    job_id: str,
    principal: Annotated[Principal, Depends(require_principal)],
) -> dict[str, Any]:
    """Exchange a bearer token for a short-lived, single-job SSE ticket.

    The browser cannot set an ``Authorization`` header on an ``EventSource``, so
    the credential has to travel in the URL. This endpoint lets the client send
    its real token over the header once (a normal request) and receive back a
    ticket that only opens this one job's stream and expires in a minute — so a
    URL captured in a log or a shared link is not a reusable credential.
    """
    _job_for(principal, job_id)
    return {
        "ticket": issue_stream_ticket(principal, job_id),
        "expires_in": int(STREAM_TICKET_TTL.total_seconds()),
    }


@stream_router.get("/jobs/{job_id}/stream")
async def stream_job(
    job_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_stream_principal)],
) -> EventSourceResponse:
    """Emit ledger entries as they appear, so the UI can follow along live."""
    _job_for(principal, job_id)

    async def event_source() -> AsyncIterator[dict[str, str]]:
        seen = 0
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                break
            job = engine.store.get(job_id)
            if job is None:
                break
            while seen < len(job.ledger):
                entry = job.ledger[seen]
                seen += 1
                idle_ticks = 0
                yield {
                    "event": "ledger",
                    "data": json.dumps(
                        {
                            "entry": entry.model_dump(mode="json"),
                            "status": job.status.value,
                            "escrow_state": job.escrow.state.value,
                        },
                        default=str,
                    ),
                }
            if job.status.value in {"settled", "voided", "refunded", "failed", "unresolved"} and seen >= len(job.ledger):
                yield {"event": "done", "data": json.dumps({"status": job.status.value})}
                break
            idle_ticks += 1
            if idle_ticks > 75:  # ~15s without progress; stop holding the connection
                break
            await asyncio.sleep(0.2)

    return EventSourceResponse(event_source())


@router.get("/reputation")
def get_reputation(
    principal: Annotated[Principal, Depends(require_principal)],
) -> dict[str, Any]:
    return {"agents": engine.reputation.all()}


@router.get("/ledger")
def global_ledger(
    principal: Annotated[Principal, Depends(require_principal)],
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    """Settlement decisions visible to the caller, newest first.

    This is an audit view, but it is still scoped to the caller: a buyer sees only
    their own jobs, a seller only jobs they are party to, an admin everything. An
    unscoped feed would let any authenticated principal read every other tenant's
    job titles, agreed prices, and ledger detail.
    """
    visible = _visible_jobs(principal, 100)

    rows: list[dict[str, Any]] = []
    for job in visible:
        for entry in job.ledger:
            rows.append(
                {
                    "job_id": job.id,
                    "job_title": job.title,
                    "seq": entry.seq,
                    "at": entry.at.isoformat(),
                    "kind": entry.kind,
                    "actor": entry.actor,
                    "summary": entry.summary,
                    "hash": entry.hash,
                    "detail": entry.detail,
                }
            )
    rows.sort(key=lambda r: r["at"], reverse=True)
    return {"count": len(rows), "entries": rows[:limit]}


def _job_summary(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "title": job.title,
        "status": job.status.value,
        "seller_id": job.seller_id,
        "agreed_price": float(job.agreed_price) if job.agreed_price is not None else None,
        "currency": job.currency,
        "escrow_state": job.escrow.state.value,
        "escrow_mode": job.escrow.mode,
        # Whether the money behind this job is real. A consumer must be able to
        # tell a simulated settlement from a real one without consulting /health.
        "simulated": job.escrow.simulated,
        "settlement_is_real": job.settlement_is_real,
        "verified": job.verification.passed if job.verification else None,
        "created_at": job.created_at.isoformat(),
        "ledger_entries": len(job.ledger),
    }


def _job_full(job: Job) -> dict[str, Any]:
    data = _job_summary(job)
    data.update(
        {
            "brief": job.brief,
            "acceptance_criteria": job.acceptance_criteria,
            "budget_max": float(job.budget_max),
            "buyer_id": job.buyer_id,
            "agreed_terms": job.agreed_terms,
            "messages": [m.model_dump(mode="json") for m in job.messages],
            "offers": [
                {"seller_id": o.seller_id, "price": float(o.price), "terms": o.terms,
                 "delivery_estimate": o.delivery_estimate, "confidence": o.confidence}
                for o in job.offers
            ],
            "delivery": (
                {"seller_id": job.delivery.seller_id, "summary": job.delivery.summary,
                 "artifact": job.delivery.artifact, "delivered_at": job.delivery.delivered_at.isoformat()}
                if job.delivery else None
            ),
            "verification": job.verification.model_dump(mode="json") if job.verification else None,
            "escrow": {
                "provider": job.escrow.provider,
                "mode": job.escrow.mode,
                "simulated": job.escrow.simulated,
                "settlement_is_real": job.settlement_is_real,
                "state": job.escrow.state.value,
                "order_id": job.escrow.order_id,
                "approval_url": job.escrow.approval_url,
                "authorization_id": job.escrow.authorization_id,
                "authorization_status": job.escrow.authorization_status,
                "capture_id": job.escrow.capture_id,
                "capture_status": job.escrow.capture_status,
                "refund_id": job.escrow.refund_id,
                "amount": float(job.escrow.amount),
                "currency": job.escrow.currency,
            },
            "ledger": [e.model_dump(mode="json") for e in job.ledger],
            "ledger_chain_valid": job.verify_chain(),
        }
    )
    return data
