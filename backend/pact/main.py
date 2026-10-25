"""FastAPI application for Pact."""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.routes import router
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

app = FastAPI(
    title="Pact",
    version=__version__,
    description="Settlement infrastructure for the agent economy, built on PayPal escrow.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=r"https://.*\.prod-runtime\.all-hands\.dev",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


def _static_dir() -> Path | None:
    """Locate a built frontend, if one was shipped alongside the API."""
    for candidate in (os.environ.get("PACT_STATIC_DIR"), "/app/static", "static"):
        if candidate and (path := Path(candidate)).is_dir() and (path / "index.html").exists():
            return path
    return None


_static = _static_dir()

if _static is None:
    # Development: the frontend runs on its own dev server, so describe the API here.

    @app.get("/")
    def root() -> dict[str, object]:
        return {
            "name": "Pact",
            "version": __version__,
            "description": "Agent-to-agent settlement on PayPal escrow.",
            "docs": "/docs",
            "health": "/api/health",
        }

else:
    # Single-container deployment: the API routes above are matched first, and
    # this mount serves the SPA for everything else, including client-side routes.
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")


@app.on_event("startup")
async def announce_configuration() -> None:
    logging.getLogger("pact").info(
        "PayPal: %s (%s) | LLM: %s | currency: %s | UI: %s",
        "configured" if settings.paypal.configured else "mock escrow",
        settings.paypal.environment,
        "configured" if settings.llm.configured else "deterministic mock",
        settings.currency,
        _static or "served separately",
    )


def _json_safe(value: Any) -> Any:
    """Make a validation-error payload JSON-serialisable.

    ``jsonable_encoder`` first, because ``errors()`` can hold ``Decimal`` values
    that the encoder normalises. Then non-finite floats are stringified: Pydantic
    echoes the offending input back, so for ``{"budget_max": 1e999}`` the payload
    contains ``inf``, and Starlette renders JSON with ``allow_nan=False``. Building
    the 422 would itself raise ``ValueError`` and surface as a 500 — a rejected
    input must stay a rejection.
    """
    value = jsonable_encoder(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": _json_safe(exc.errors())})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
    """Never leak internals to a client, but log them server-side."""
    logging.getLogger("pact").exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal error"})
