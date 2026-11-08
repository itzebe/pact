"""FastAPI application for Pact."""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from . import __version__
from .api.routes import public_router, router, stream_router
from .api import auth_routes
from .config import settings
from .core.ticket_store import ticket_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

app = FastAPI(
    title="Pact",
    version=__version__,
    description="Settlement infrastructure for the agent economy, built on PayPal escrow.",
)

# Cap the request body before it is parsed. FastAPI/Pydantic validate the shape
# of a payload, but only after the whole body has been read into memory, so a
# multi-megabyte criterion would be buffered before any validator ran.
MAX_REQUEST_BYTES = 64 * 1024


def _too_large_response(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


class LimitRequestBodyMiddleware:
    """Enforce ``MAX_REQUEST_BYTES`` on the bytes actually received.

    A ``Content-Length`` check alone is not enough: a client that sends
    ``Transfer-Encoding: chunked`` (or simply omits the header) declares no size,
    and the body was then read into memory in full before any validator ran — a
    one-request memory-exhaustion DoS. This wraps the ASGI ``receive`` channel and
    counts real bytes, so the cap holds however the body is framed.
    """

    def __init__(self, app: Any, max_bytes: int = MAX_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await _too_large_response(400, "Invalid Content-Length")(scope, receive, send)
                    return
                if declared > self.max_bytes:
                    await _too_large_response(
                        413, f"Request body exceeds {self.max_bytes} bytes"
                    )(scope, receive, send)
                    return

        received = 0

        async def limited_receive() -> Any:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # An HTTPException is re-raised by FastAPI's body reader rather
                    # than being re-wrapped as a generic 400, so the client is told
                    # the truth: the body was too large.
                    raise HTTPException(
                        status_code=413,
                        detail=f"Request body exceeds {self.max_bytes} bytes",
                    )
            return message

        await self.app(scope, limited_receive, send)


#: Hardening headers attached to every response. These are the ones that are safe
#: for both the JSON API and the SPA shell:
#:
#: * ``X-Content-Type-Options`` stops a browser from re-interpreting a JSON or
#:   error body as HTML/script (MIME confusion).
#: * ``X-Frame-Options`` and the ``frame-ancestors`` CSP directive together stop
#:   the app being framed, which would otherwise let a third-party page overlay a
#:   click target over the guest's refund/approve controls.
#: * ``Referrer-Policy`` keeps the guest's full URL (which can carry a job id) out
#:   of the ``Referer`` sent to PayPal on the approval redirect.
#: * ``Permissions-Policy`` turns off browser capabilities the product never uses.
#: * ``Cross-Origin-Opener-Policy`` isolates the document from cross-origin opener
#:   access.
#:
#: This is deliberately *not* a script/style CSP: the Vite bundle relies on inline
#: styles, so a ``script-src``/``style-src`` policy here would break the app. The
#: directives chosen harden framing and capability use without constraining the
#: bundle. HSTS is not set here — the Vercel edge already sends it, and the app must
#: not force it on a plain-HTTP local run. See docs/security.md.
_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (
        b"content-security-policy",
        b"frame-ancestors 'none'; object-src 'none'; base-uri 'self'",
    ),
    (
        b"permissions-policy",
        b"camera=(), microphone=(), geolocation=()",
    ),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"x-permitted-cross-domain-policies", b"none"),
)


class SecurityHeadersMiddleware:
    """Attach the hardening headers to every response, without overriding one.

    A header already set further down the stack (for example by a future route that
    needs a different policy) is left untouched, so this cannot silently clobber a
    more specific decision.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {name.lower() for name, _ in headers}
                for name, value in _SECURITY_HEADERS:
                    if name not in present:
                        headers.append((name, value))
            await send(message)

        await self.app(scope, receive, send_with_headers)


# Middleware runs in reverse order of registration: the last added is outermost.
# Registering the size cap first makes CORS the outer layer, so a 413 (or any
# rejection) still carries CORS headers and a browser can read the reason.
app.add_middleware(LimitRequestBodyMiddleware)
app.add_middleware(
    CORSMiddleware,
    # Exact origins only. A regex over a shared parent domain would trust every
    # other tenant on that host, and with credentials enabled that is a direct
    # path to reading and driving another user's settlement.
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    # X-CSRF-Token must be allowed so a cross-origin caller (a local dev frontend
    # pointed at a deployed API) can complete the double-submit handshake. Same-origin
    # requests never preflight, so this widens nothing for the production app.
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
)

# Added last so it is the outermost layer: every response — including a CORS
# preflight rejection or a 413 from the size cap — carries the hardening headers.
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(public_router)
app.include_router(router)
app.include_router(stream_router)
# Customer authentication. The public auth routes (register/login/refresh/...) are
# reachable before a session exists; /auth/me and /auth/password/change carry their
# own router-level require_principal dependency. See pact/api/auth_routes.py.
app.include_router(auth_routes.public_router)
app.include_router(auth_routes.router)


def _static_dir() -> Path | None:
    """Locate a built frontend, if one was shipped alongside the API."""
    for candidate in (os.environ.get("PACT_STATIC_DIR"), "/app/static", "static"):
        if candidate and (path := Path(candidate)).is_dir() and (path / "index.html").exists():
            return path
    return None


class _SPAStaticFiles(StaticFiles):
    """Serve a single-page app, including client-side routes.

    ``StaticFiles(html=True)`` only falls back to ``index.html`` for a *directory*
    URL, so a deep link such as ``/jobs/abc`` — which the app handles in the
    browser — returns 404. Returning ``index.html`` for any unknown GET path lets
    the client router take over, while real assets (a 404 that ends in a file
    extension) still 404 honestly instead of being answered with HTML.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and "." not in Path(path).name:
                return await super().get_response("index.html", scope)
            raise


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
    app.mount("/", _SPAStaticFiles(directory=str(_static), html=True), name="static")


@app.on_event("startup")
async def announce_configuration() -> None:
    # Refuse to serve a production deployment that would fake a settlement, or
    # that has no way to authenticate a caller. Failing at startup is far cheaper
    # than discovering it from a customer who was told money moved.
    problems = settings.validate_runtime()
    if problems:
        for problem in problems:
            logging.getLogger("pact").error("CONFIGURATION ERROR: %s", problem)
        if settings.is_production:
            raise RuntimeError(
                "Refusing to start in production with an unsafe configuration: "
                + "; ".join(problems)
            )
    # A production deployment whose shared ticket store could not be opened would
    # otherwise serve with a process-local registry, where a ticket consumed by one
    # instance is replayable against another. The store itself already refuses every
    # ticket in that state (fail closed); refusing to boot makes the misconfiguration
    # impossible to miss instead of merely safe. Reaching this point means a
    # DATABASE_URL was configured (its absence is caught above), so only the failure
    # classification — never the connection string — would be reported.
    if settings.is_production and ticket_store.backend == "unavailable":
        raise RuntimeError(
            "Refusing to start in production: the configured PostgreSQL store could "
            "not be opened, so single-use SSE tickets would not be enforced across "
            "instances. Check that DATABASE_URL points at a reachable database."
        )
    logging.getLogger("pact").info(
        "PayPal: %s (%s) | LLM: %s | auth: %s | env: %s | currency: %s | UI: %s",
        "configured" if settings.paypal.configured else "mock escrow",
        settings.paypal.environment,
        "configured" if settings.llm.configured else "deterministic mock",
        "configured" if settings.auth.configured else "NOT CONFIGURED",
        settings.environment,
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
