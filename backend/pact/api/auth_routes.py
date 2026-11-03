"""HTTP routes for customer authentication.

Route placement is a security control, not a convenience. FastAPI attaches a
dependency to the ``APIRouter`` object, so a route is protected **iff** it is
declared on the protected router:

* ``public_router`` (no dependency) carries only the routes that must be
  reachable before a session exists: register, verify-email, login, refresh,
  logout, password/forgot, password/reset.
* ``router`` (router-level ``Depends(require_principal)``) carries the routes that
  require a session: ``/auth/me`` and ``/auth/password/change``.

Because the two routers are distinct objects, a path cannot exist on both and a
protected endpoint cannot be "accidentally" exposed by forgetting a decorator.
Registration is additionally refused whenever self-service is disabled, so the
whole surface is inert until the owner enables it.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..config import settings
from ..core.accounts import AuthError, accounts
from ..core.auth_store import hash_identifier
from .auth import (
    GUEST_COOKIE,
    GUEST_COOKIE_PATH,
    Principal,
    require_principal,
)

log = logging.getLogger("pact.auth.routes")

public_router = APIRouter(prefix="/api/auth")
router = APIRouter(prefix="/api/auth", dependencies=[Depends(require_principal)])

#: Cookie name and scope for the refresh token. Path-scoped to the auth routes so
#: the browser never attaches it to a money route, and host-only (no Domain) so it
#: cannot leak to a sibling subdomain.
REFRESH_COOKIE = "pact_refresh"
REFRESH_COOKIE_PATH = "/api/auth"


def _client_ip_hash(request: Request) -> str:
    """Hash of the client address, for rate limiting only.

    The *rightmost* ``x-forwarded-for`` entry is used: it is the one written by the
    nearest trusted proxy (Vercel's edge, which documents that it overwrites this
    header rather than appending to it). The leftmost entry is client-controllable
    and would let a caller evade the per-address cap by forging a new value on every
    request. Falling back to the socket peer keeps this correct when there is no
    proxy in front. The value is hashed, never stored raw.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    candidate = ""
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            candidate = parts[-1]
    if not candidate:
        candidate = request.client.host if request.client else "unknown"
    return hash_identifier(candidate)


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _set_guest_cookie(response: Response, token: str, max_age: int) -> None:
    """Set the guest session cookie.

    ``HttpOnly`` so frontend JavaScript cannot read it, ``Secure`` so it never
    travels in the clear, ``SameSite=Lax`` so a cross-site page cannot drive a
    money route with it, and ``Path=/api`` so it is not attached to static assets.
    The CSRF token the client needs travels in the JSON body, never in this cookie.
    """
    response.set_cookie(
        key=GUEST_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="lax",
        path=GUEST_COOKIE_PATH,
    )


def _clear_guest_cookie(response: Response) -> None:
    response.delete_cookie(key=GUEST_COOKIE, path=GUEST_COOKIE_PATH)


def _require_guests_enabled() -> None:
    """Refuse the guest surface when it is disabled.

    Returns 404 (not 403) so a disabled feature does not advertise itself, exactly
    as the registration surface does.
    """
    if not settings.auth.guests_enabled:
        raise HTTPException(status_code=404, detail="Not found")


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        max_age=settings.auth.refresh_token_ttl_days * 86400,
        httponly=True,
        secure=True,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)


def _require_self_service() -> None:
    """Refuse the registration surface while self-service is disabled.

    Returns 404 (not 403) so a disabled feature does not advertise itself.
    """
    if not settings.auth.self_service_enabled:
        raise HTTPException(status_code=404, detail="Not found")


def _auth_error(exc: AuthError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=12, max_length=256)
    # Only buyer and seller are accepted; the service refuses anything else, so an
    # administrator cannot be created through this endpoint even by a crafted body.
    role: str = Field(default="buyer", pattern="^(buyer|seller)$")
    display_name: str | None = Field(default=None, max_length=120)


class VerifyRequest(BaseModel):
    token: str = Field(..., min_length=8, max_length=256)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=1, max_length=256)


class ForgotRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)


class ResetRequest(BaseModel):
    token: str = Field(..., min_length=8, max_length=256)
    password: str = Field(..., min_length=12, max_length=256)


class ChangeRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=12, max_length=256)


# --------------------------------------------------------------------------- #
# Public routes
# --------------------------------------------------------------------------- #


@public_router.post("/register", status_code=202)
def register(payload: RegisterRequest, request: Request, response: Response) -> dict:
    _require_self_service()
    try:
        message = accounts.register(
            email=payload.email,
            password=payload.password,
            role=payload.role,
            display_name=payload.display_name,
            ip_hash=_client_ip_hash(request),
        )
    except AuthError as exc:
        raise _auth_error(exc) from exc
    # Uniform response regardless of whether the address already existed.
    return {"detail": message}


@public_router.post("/verify-email")
def verify_email(payload: VerifyRequest) -> dict:
    try:
        return {"detail": accounts.verify_email(payload.token)}
    except AuthError as exc:
        raise _auth_error(exc) from exc


@public_router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response) -> dict:
    try:
        session = accounts.login(
            email=payload.email,
            password=payload.password,
            ip_hash=_client_ip_hash(request),
            user_agent=request.headers.get("user-agent"),
        )
    except AuthError as exc:
        raise _auth_error(exc) from exc
    # The refresh token goes into an HttpOnly cookie, never into the JSON body, so
    # frontend JavaScript can never read it.
    _set_refresh_cookie(response, session["refresh_token"])
    return {"access_token": session["access_token"], "user": session["user"]}


@public_router.post("/refresh")
def refresh(request: Request, response: Response) -> dict:
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise HTTPException(status_code=401, detail="No session to refresh.")
    try:
        session = accounts.refresh(
            raw, user_agent=request.headers.get("user-agent"), ip_hash=_client_ip_hash(request)
        )
    except AuthError as exc:
        _clear_refresh_cookie(response)
        raise _auth_error(exc) from exc
    _set_refresh_cookie(response, session["refresh_token"])
    return {"access_token": session["access_token"], "user": session["user"]}


@public_router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> Response:
    accounts.logout(request.cookies.get(REFRESH_COOKIE))
    # Clear the cookie on the *injected* response and return it; building a new
    # Response would discard the Set-Cookie that expires the refresh token.
    _clear_refresh_cookie(response)
    response.status_code = 204
    return response


# --------------------------------------------------------------------------- #
# Guest (judge) sessions
#
# A judge opening the deployed product must be able to use it with no credential
# to supply. These two public routes are the entire onboarding: ``session``
# provisions a workspace and returns the CSRF token the client must echo; ``me``
# reports who the cookie belongs to. The cookie itself is server-signed and is
# never trusted from a request body.
# --------------------------------------------------------------------------- #


class GuestSessionRequest(BaseModel):
    """An empty body. Present so the endpoint is a POST (a session is created) and
    so a future optional display name has somewhere to live."""

    display_name: str | None = Field(default=None, max_length=120)


def _sandbox_buyer_hint() -> dict | None:
    """A sandbox buyer account a guest can use to approve the PayPal order.

    PayPal will not authorize an order until the buyer approves it, and a public
    judge has no sandbox account of their own, so the escrow flow would otherwise
    stop at "buyer approval required" with nothing to click. When an operator
    configures one, it is handed to *guests only* — never to an unauthenticated
    caller, never to a customer or operator session — and only while the deployment
    is pointed at the sandbox host, so it cannot leak from a deployment that moves
    real money.
    """
    if settings.paypal.is_live:
        return None
    if not (settings.paypal.sandbox_buyer_email and settings.paypal.sandbox_buyer_password):
        return None
    return {
        "email": settings.paypal.sandbox_buyer_email,
        "password": settings.paypal.sandbox_buyer_password,
        "note": "PayPal Sandbox test buyer — no real money is involved.",
    }


def _guest_payload(principal: Principal, *, expires_in: int) -> dict:
    return {
        "type": "guest",
        "subject": principal.subject,
        "role": principal.role,
        "csrf_token": principal.csrf_token,
        "expires_in": expires_in,
        "sandbox_buyer": _sandbox_buyer_hint(),
    }


@public_router.post("/guest", status_code=201)
def create_guest_session(
    request: Request,
    response: Response,
    payload: GuestSessionRequest | None = None,
) -> dict:
    """Provision a guest workspace and start a session. No credential required.

    This is the zero-friction entry point: a first-time visitor gets a real,
    isolated buyer identity and a signed session cookie, so the actual product can
    be used immediately. Two abuse controls apply and neither hinders an ordinary
    evaluation:

    * new workspaces are capped per client IP within a window, so one source cannot
      mint unbounded rows and sandbox orders;
    * each workspace has a job quota (enforced where jobs are created).

    The CSRF token is returned in the body — the client stores it in memory and
    echoes it in ``X-CSRF-Token`` on state-changing requests. It is never placed in
    a cookie, and the session cookie is ``HttpOnly``, so no script can read the
    session material.
    """
    _require_guests_enabled()

    from ..core.guests import guests, issue_guest_session

    ip_hash = _client_ip_hash(request)
    recent = accounts.store.count_recent(
        "guest_new", ip_hash, settings.auth.guest_new_window_seconds
    )
    if recent >= settings.auth.guest_new_per_ip:
        raise HTTPException(
            status_code=429,
            detail="Too many guest sessions from this address. Try again later.",
        )
    # Record the attempt *before* provisioning, so a burst cannot race past the cap.
    accounts.store.record_attempt("guest_new", ip_hash, True)

    try:
        user = guests.provision()
    except Exception as exc:  # store failure: fail closed, do not fake a session
        log.error("Could not provision a guest session (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="Guest sessions are temporarily unavailable.",
        ) from exc

    token, csrf, expires_at = issue_guest_session(user.id, token_version=user.token_version)
    max_age = max(60, int((expires_at - _now()).total_seconds()))
    _set_guest_cookie(response, token, max_age)
    accounts.store.record_event("guest_created", actor_id=user.id, ip_hash=ip_hash)
    principal = Principal(subject=user.id, role="buyer", token_version=user.token_version,
                          via_cookie=True, csrf_token=csrf)
    return _guest_payload(principal, expires_in=max_age)


@public_router.get("/guest")
def current_guest(principal: Annotated[Principal, Depends(require_principal)]) -> dict:
    """Report the current guest session, or 401 if there is none.

    Deliberately only answers for a *guest* cookie: a customer or operator
    credential reaches this path with a different subject, and reporting it here
    would blur the two surfaces. ``/auth/me`` is the general "who am I" route.
    """
    _require_guests_enabled()
    if not principal.is_guest:
        raise HTTPException(status_code=404, detail="Not a guest session")
    return {
        "type": "guest",
        "subject": principal.subject,
        "role": principal.role,
        "csrf_token": principal.csrf_token,
        "sandbox_buyer": _sandbox_buyer_hint(),
    }


@public_router.post("/guest/logout", status_code=204)
def guest_logout(request: Request, response: Response) -> Response:
    """End a guest session by clearing the cookie. Idempotent."""
    _require_guests_enabled()
    _clear_guest_cookie(response)
    response.status_code = 204
    return response


@public_router.post("/password/forgot", status_code=202)
def forgot_password(payload: ForgotRequest, request: Request) -> dict:
    message = accounts.request_password_reset(
        email=payload.email, ip_hash=_client_ip_hash(request)
    )
    return {"detail": message}


@public_router.post("/password/reset")
def reset_password(payload: ResetRequest) -> dict:
    try:
        return {"detail": accounts.reset_password(raw_token=payload.token, new_password=payload.password)}
    except AuthError as exc:
        raise _auth_error(exc) from exc


# --------------------------------------------------------------------------- #
# Protected routes (router-level Depends(require_principal))
# --------------------------------------------------------------------------- #


@router.get("/me")
def me(principal: Annotated[Principal, Depends(require_principal)]) -> dict:
    """The current caller.

    A legacy operator token has no customer row; it is reported honestly as an
    operator credential rather than pretending to be a registered customer.
    """
    user = accounts.store.get_user_by_id(principal.subject)
    if user is None:
        return {
            "type": "operator",
            "subject": principal.subject,
            "role": principal.role,
            "email_verified": True,
        }
    return {
        "type": "customer",
        "subject": user.id,
        "role": user.role,
        "email": user.email,
        "display_name": user.display_name,
        "email_verified": user.email_verified,
        "status": user.status,
    }


@router.post("/password/change")
def change_password(
    payload: ChangeRequest,
    principal: Annotated[Principal, Depends(require_principal)],
    response: Response,
) -> dict:
    user = accounts.store.get_user_by_id(principal.subject)
    if user is None:
        raise HTTPException(
            status_code=403,
            detail="Operator credentials have no customer password to change.",
        )
    try:
        message = accounts.change_password(
            user_id=user.id,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except AuthError as exc:
        raise _auth_error(exc) from exc
    # Changing a password signs every session out, including this one.
    _clear_refresh_cookie(response)
    return {"detail": message}
