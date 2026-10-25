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
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..config import settings
from ..core.engine import JobAlreadyRunning, engine
from ..core.models import EscrowState, Job
from ..core.negotiation import default_seller_ids
from ..paypal import MockPayPalEscrowClient, PayPalError, approval_url

log = logging.getLogger("pact.api")

router = APIRouter(prefix="/api")


class JobCreate(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    brief: str = Field(..., min_length=3, max_length=4000)
    acceptance_criteria: list[str] = Field(..., min_length=1)
    budget_max: Decimal = Field(..., gt=0)
    buyer_id: str = "buyer"
    seller_ids: list[str] | None = None


class SellerInfo(BaseModel):
    agent_id: str
    score: float
    settled: int
    voided: int
    failed: int
    volume: float


def _job_or_404(job_id: str) -> Job:
    job = engine.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")
    return job


@router.get("/health")
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

    return {
        "status": "degraded" if storage.get("settlement_disabled") else "ok",
        "paypal": {
            "configured": settings.paypal.configured,
            "environment": settings.paypal.environment,
            "mode": engine.settlement_mode,
            "require_real": settings.require_real_paypal,
        },
        "llm": {
            "configured": settings.llm.configured,
            "model": engine.llm.name,
            "is_mock": bool(getattr(engine.llm, "is_mock", False)),
        },
        "currency": settings.currency,
        "sellers": default_seller_ids(),
        # Which persistence is actually in use, so a deployment can never be
        # mistaken for durable when it is not.
        "storage": storage,
    }


@router.get("/jobs")
def list_jobs(limit: int = 50) -> dict[str, Any]:
    jobs = engine.store.list(limit=limit)
    return {"count": len(jobs), "jobs": [_job_summary(j) for j in jobs]}


@router.post("/jobs", status_code=201)
async def create_job(payload: JobCreate) -> dict[str, Any]:
    job = engine.create_job(
        title=payload.title,
        brief=payload.brief,
        criteria=payload.acceptance_criteria,
        budget_max=payload.budget_max,
        buyer_id=payload.buyer_id,
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

    ``PACT_PUBLIC_URL`` wins when set. Otherwise the request's own host is used,
    which is correct behind Vercel's proxy (``X-Forwarded-Host``/``Proto``). This
    keeps the return target real on any deployment without extra configuration,
    rather than baking in a placeholder host that would strand the buyer.
    """
    if settings.public_url:
        return settings.public_url
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not forwarded_host:
        return ""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{forwarded_host}"


@router.post("/jobs/{job_id}/run")
async def run_job(
    job_id: str, request: Request, seller_ids: list[str] | None = Query(default=None)
) -> dict[str, Any]:
    """Run the full lifecycle. Executed off the event loop so it cannot block it.

    A job that is already mid-flight is refused with 409 rather than run twice, so
    two concurrent requests cannot produce a duplicate capture.
    """
    job = _job_or_404(job_id)
    if job.status.value not in {"open", "failed"}:
        raise HTTPException(status_code=409, detail=f"Job is already {job.status.value}")

    requested = _parse_seller_ids(seller_ids) or default_seller_ids()
    known = set(default_seller_ids())
    unknown = [s for s in requested if s not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown seller(s): {', '.join(unknown)}")

    try:
        completed = await asyncio.to_thread(engine.run, job, requested, public_url=_public_base(request))
    except JobAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _job_full(completed)


@router.post("/jobs/{job_id}/approve")
async def approve_job(job_id: str) -> dict[str, Any]:
    """Continue a job after the buyer has approved the PayPal order.

    Only meaningful in real PayPal mode, where an order cannot be authorized until
    the buyer visits the approval URL. In simulated mode the order is authorized
    immediately and this returns 409.
    """
    job = _job_or_404(job_id)
    if job.escrow.state is not EscrowState.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"Job is not awaiting buyer approval (escrow state {job.escrow.state.value})",
        )
    completed = await asyncio.to_thread(engine.approve_and_hold, job)
    return _job_full(completed)


@router.post("/jobs/{job_id}/reconcile")
async def reconcile_job(job_id: str) -> dict[str, Any]:
    """Resolve a job whose settlement outcome was never determined.

    Only reads PayPal to establish what actually happened, so it is safe to call
    repeatedly. Returns 409 for a job that is not in the unresolved state.
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
async def get_paypal_order(job_id: str) -> dict[str, Any]:
    """Read the live PayPal order behind a job. Read-only; moves no money.

    Exists so a stuck buyer approval can be diagnosed from the provider's own
    record (status, intent, amount, payee) instead of inferring it from the
    approval link. Identity fields are redacted.
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
def get_job(job_id: str) -> dict[str, Any]:
    return _job_full(_job_or_404(job_id))


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: str) -> None:
    if not engine.store.delete(job_id):
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")


@router.get("/jobs/{job_id}/ledger")
def get_ledger(job_id: str) -> dict[str, Any]:
    job = _job_or_404(job_id)
    return {
        "job_id": job.id,
        "entries": [e.model_dump(mode="json") for e in job.ledger],
        "chain_valid": job.verify_chain(),
        "entry_count": len(job.ledger),
    }


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str, request: Request) -> EventSourceResponse:
    """Emit ledger entries as they appear, so the UI can follow along live."""
    _job_or_404(job_id)

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
            if idle_ticks > 300:  # ~60s without progress; stop holding the connection
                break
            await asyncio.sleep(0.2)

    return EventSourceResponse(event_source())


@router.get("/reputation")
def get_reputation() -> dict[str, Any]:
    return {"agents": engine.reputation.all()}


@router.get("/ledger")
def global_ledger(limit: int = 200) -> dict[str, Any]:
    """Every settlement decision across every job, newest first."""
    rows: list[dict[str, Any]] = []
    for job in engine.store.list(limit=100):
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
