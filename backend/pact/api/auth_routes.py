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
from .auth import Principal, require_principal

log = logging.getLogger("pact.auth.routes")

public_router = APIRouter(prefix="/api/auth")
router = APIRouter(prefix="/api/auth", dependencies=[Depends(require_principal)])

#: Cookie name and scope for the refresh token. Path-scoped to the auth routes so
#: the browser never attaches it to a money route, and host-only (no Domain) so it
#: cannot leak to a sibling subdomain.
REFRESH_COOKIE = "pact_refresh"
REFRESH_COOKIE_PATH = "/api/auth"


def _client_ip_hash(request: Request) -> str:
    client = request.client.host if request.client else "unknown"
    return hash_identifier(client)


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
