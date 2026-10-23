"""HTTP API for Pact.

Exposes the job lifecycle, the audit ledger, reputation, and a server-sent
event stream so the UI can follow a settlement as it happens rather than
polling for a finished result.
"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..config import settings
from ..core.engine import engine
from ..core.models import Job
from ..core.negotiation import default_seller_ids

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
    refunded: int
    failed: int
    volume: float


def _job_or_404(job_id: str) -> Job:
    job = engine.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")
    return job


@router.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "paypal": {
            "configured": settings.paypal.configured,
            "environment": settings.paypal.environment,
            "mode": engine.settlement_mode,
        },
        "llm": {
            "configured": settings.llm.configured,
            "model": engine.llm.name,
            "is_mock": bool(getattr(engine.llm, "is_mock", False)),
        },
        "currency": settings.currency,
        "sellers": default_seller_ids(),
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


@router.post("/jobs/{job_id}/run")
async def run_job(job_id: str, seller_ids: list[str] | None = None) -> dict[str, Any]:
    """Run the full lifecycle. Executed off the event loop so it cannot block it."""
    job = _job_or_404(job_id)
    if job.status.value not in {"open", "failed"}:
        raise HTTPException(status_code=409, detail=f"Job is already {job.status.value}")
    completed = await asyncio.to_thread(engine.run, job, seller_ids or default_seller_ids())
    return _job_full(completed)


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
            if job.status.value in {"settled", "refunded", "failed"} and seen >= len(job.ledger):
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
                "authorization_id": job.escrow.authorization_id,
                "capture_id": job.escrow.capture_id,
                "refund_id": job.escrow.refund_id,
                "amount": float(job.escrow.amount),
                "currency": job.escrow.currency,
            },
            "ledger": [e.model_dump(mode="json") for e in job.ledger],
            "ledger_chain_valid": job.verify_chain(),
        }
    )
    return data
