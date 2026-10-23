"""FastAPI application for Pact."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
    """Never leak internals to a client, but log them server-side."""
    logging.getLogger("pact").exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal error"})
