"""Authentication and authorization for the Pact API.

Every request that can read or move money carries a bearer credential. The
credential is a JSON Web Token signed with a server-side secret (HS256 by
default), so identity is *derived* from a verified signature rather than trusted
from the request body. A caller cannot become a different buyer or seller by
changing a field, because those fields are taken from the token and overwritten
on the way in.

Three roles exist, and they are not interchangeable:

* ``buyer``  — posts jobs, runs them, approves the PayPal order, settles.
* ``seller`` — the agents that compete for work.
* ``admin``  — reconciliation, cancellation, deletion and global inspection.

The design fails closed. If no signing secret is configured the router refuses
every request (503) instead of letting one through, because a missing secret
means "not configured", not "unauthenticated is acceptable".

Credentials are minted with ``scripts/issue_token.py``. See ``docs/AUTH.md``.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Query, Request, status

from ..config import ALLOWED_AUTH_ALGORITHMS, settings
from ..core.auth_store import hash_identifier
from ..core.ticket_store import ticket_store

log = logging.getLogger("pact.auth")


def client_ip_hash(request: Request) -> str:
    """Hash of the client address, for rate limiting and audit only.

    The *rightmost* ``x-forwarded-for`` entry is used: it is the one written by the
    nearest trusted proxy (Vercel's edge, which documents that it overwrites this
    header rather than appending to it). The leftmost entry is client-controllable
    and would let a caller evade a per-address cap by forging a new value on every
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


# Roles. A principal holds exactly one; ``admin`` is deliberately separate from
# ``buyer`` so a compromised buyer token cannot reconcile or delete records.
ROLE_BUYER = "buyer"
ROLE_SELLER = "seller"
ROLE_ADMIN = "admin"
VALID_ROLES = frozenset({ROLE_BUYER, ROLE_SELLER, ROLE_ADMIN})

# A generous but finite lifetime. A token that never expires is a credential that
# can never be revoked; 30 days keeps the demo usable without that being forever.
DEFAULT_TOKEN_TTL = timedelta(days=30)

# Stream tickets travel in a URL, so they are deliberately short-lived: long
# enough for the client to open the EventSource, far too short to be worth
# harvesting from a log or a shared link.
STREAM_TICKET_TTL = timedelta(seconds=60)

# Issuer and audience for customer access tokens. A *legacy operator token* (minted
# by ``issue_token`` for scripts) carries neither claim and is still accepted on the
# strength of its signature; a customer token always carries both and they must
# match. See ``_decode``.
TOKEN_ISSUER = "pact"
TOKEN_AUDIENCE = "pact-api"

# Guest (judge) session cookie. Scoped to ``/api`` so the browser never attaches it
# to a static asset request, and host-only (no ``Domain``) so it cannot leak to a
# sibling subdomain. It is ``HttpOnly`` so frontend JavaScript cannot read it and
# ``SameSite=Lax`` so it is not sent on a cross-site sub-request — which, with the
# synchroniser CSRF token below, is what keeps a third-party page from driving a
# money route on a judge's behalf. See ``pact.core.guests``.
GUEST_COOKIE = "pact_guest"
GUEST_COOKIE_PATH = "/api"
#: Header that must echo the CSRF token carried inside the guest session for any
#: state-changing request. A cross-origin page can make the browser *send* the
#: cookie, but it cannot read the token out of the JSON response, so it cannot set
#: this header.
CSRF_HEADER = "X-CSRF-Token"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def stream_scope(job_id: str) -> str:
    """The capability string a stream ticket must carry for ``job_id``."""
    return f"stream:{job_id}"


# Consumed stream-ticket ids, so a ticket is single-use. A ticket travels in a URL
# (server logs, browser history, a shared link), and the frontend reconnects if a
# stream drops — without this, a captured URL could be replayed for the ticket's
# whole lifetime. The registry lives in the shared ticket store: PostgreSQL when a
# shared database is configured (so a replay landing on another instance is still
# refused), and a process-local map only where the deployment is single-instance by
# construction. See ``pact.core.ticket_store``.
def _consume_ticket(ticket_id: str, ttl_seconds: float) -> bool:
    """Record a ticket id as used for ``ttl_seconds``. False if already consumed.

    Delegates to the shared store, which decides the consumption atomically and
    fails closed when a shared store is configured but unreachable. The id is
    retained only for the ticket's remaining lifetime, so the registry stays
    bounded.
    """
    return ticket_store.consume(ticket_id, ttl_seconds)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, derived only from a verified token."""

    subject: str
    role: str
    # Set only on a capability-scoped token (a stream ticket). Ordinary tokens
    # carry no scope and are refused where a scope is required, and vice versa.
    scope: str | None = None
    # The unique id of a stream ticket, used to consume it exactly once. ``None``
    # for an ordinary token.
    ticket_id: str | None = None
    # The account token version carried by a customer access token (``ver``). ``None``
    # for a legacy operator token, which has no customer row to version.
    token_version: int | None = None
    # True when this principal was authenticated from the guest session *cookie*
    # rather than an ``Authorization`` header. A cookie-authenticated request must
    # also present the matching CSRF token on a mutating method (see
    # ``require_principal``); a header-authenticated request is exempt because a
    # browser cannot set an ``Authorization`` header cross-origin at all.
    via_cookie: bool = False
    # The CSRF token carried inside the guest session. Kept off any response body;
    # it is compared, never echoed from here.
    csrf_token: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_buyer(self) -> bool:
        return self.role == ROLE_BUYER

    @property
    def is_seller(self) -> bool:
        return self.role == ROLE_SELLER

    @property
    def is_guest(self) -> bool:
        from ..core.guests import is_guest_id

        return is_guest_id(self.subject)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def issue_token(
    subject: str,
    role: str,
    *,
    secret: str | None = None,
    ttl: timedelta = DEFAULT_TOKEN_TTL,
    scope: str | None = None,
    now: datetime | None = None,
) -> str:
    """Mint a signed bearer token for ``subject`` in ``role``.

    Kept in the application package (not a script) so tests can mint tokens
    without shelling out. ``secret`` defaults to the configured signing secret.

    ``scope`` narrows a token to a single capability (currently a stream ticket).
    A scoped token is only accepted where that scope is required, so a leaked
    stream ticket cannot be replayed against any other route.
    """
    import jwt  # imported lazily so the app can start without the dependency

    signing_secret = secret if secret is not None else settings.auth.secret
    if not signing_secret:
        raise RuntimeError("No auth signing secret is configured; cannot issue a token.")
    if role not in VALID_ROLES:
        raise ValueError(f"Unknown role {role!r}")
    if not subject or len(subject) > 128:
        raise ValueError("subject must be 1..128 characters")
    if settings.auth.algorithm not in ALLOWED_AUTH_ALGORITHMS:
        raise ValueError(f"Unsupported signing algorithm {settings.auth.algorithm!r}")

    issued = now or datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "iat": int(issued.timestamp()),
        "exp": int((issued + ttl).timestamp()),
    }
    if scope:
        payload["scope"] = scope
    return jwt.encode(payload, signing_secret, algorithm=settings.auth.algorithm)


def _algorithm() -> str:
    """The signing algorithm, checked against the allowlist before it is used.

    A misconfigured ``PACT_AUTH_ALGORITHM`` (``none``, or an asymmetric algorithm
    this server has no public key for) must never reach the verifier.
    """
    algorithm = settings.auth.algorithm
    if algorithm not in ALLOWED_AUTH_ALGORITHMS:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Unsupported PACT_AUTH_ALGORITHM {algorithm!r}",
        )
    return algorithm


def _decode(token: str) -> dict[str, Any]:
    import jwt

    try:
        # ``verify_aud`` is disabled at the library level because a *legacy
        # operator token* carries no ``aud``/``iss`` claim, and PyJWT raises when a
        # token has an audience but the verifier was not told which to expect.
        # Those claims are validated explicitly below instead, so a customer token
        # is still checked and a legacy token is still accepted. The signature and
        # expiry are always verified by the library.
        claims = jwt.decode(
            token,
            settings.auth.secret,
            algorithms=[_algorithm()],
            options={"verify_aud": False},
        )
    except HTTPException:
        raise
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorized("Token has expired") from exc
    except jwt.InvalidKeyError as exc:
        # An unusable signing key (e.g. an empty secret). This is a server
        # misconfiguration, not a bad credential, and it must fail closed rather
        # than surface as an uncaught 500 with a traceback.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured correctly on this server.",
        ) from exc
    except jwt.InvalidTokenError as exc:
        # Covers a bad signature, a malformed token, and an unexpected algorithm.
        raise _unauthorized("Invalid authentication token") from exc

    # When present, the issuer and audience must match this API. A customer access
    # token always carries them; a legacy operator token carries neither and is
    # accepted on the strength of its signature alone.
    issuer = claims.get("iss")
    if issuer is not None and issuer != TOKEN_ISSUER:
        raise _unauthorized("Invalid authentication token")
    audience = claims.get("aud")
    if audience is not None and audience != TOKEN_AUDIENCE:
        raise _unauthorized("Invalid authentication token")
    return claims


def _principal_from_claims(claims: dict[str, Any]) -> Principal:
    subject = claims.get("sub")
    role = claims.get("role")
    if not isinstance(subject, str) or not subject:
        raise _unauthorized("Token is missing a subject")
    if role not in VALID_ROLES:
        raise _unauthorized("Token carries an unknown role")
    # A guest session is deliberately cookie-only. Accepting it as a header token
    # would create a path around the CSRF check, so it is refused on every
    # header/ticket path here — the cookie path builds its principal elsewhere.
    if claims.get("typ") == "guest":
        raise _unauthorized("A guest session is only valid as a session cookie")
    scope = claims.get("scope")
    jti = claims.get("jti")
    ver = claims.get("ver")
    return Principal(
        subject=subject,
        role=role,
        scope=scope if isinstance(scope, str) else None,
        ticket_id=jti if isinstance(jti, str) else None,
        token_version=ver if isinstance(ver, int) else None,
    )


def _assert_not_scoped(principal: Principal) -> None:
    """Refuse a capability-scoped token on an ordinary route.

    A stream ticket is short-lived and single-purpose. Accepting it here would
    widen the blast radius of a leaked URL to every money route.
    """
    if principal.scope:
        raise _unauthorized("A scoped token is not valid for this route")


def require_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
) -> Principal:
    """FastAPI dependency: resolve and verify the caller's credential.

    Two credential forms are accepted, and only two:

    * an ``Authorization: Bearer <token>`` header — an operator token, a customer
      access token, or a test token; or
    * the guest session cookie (``pact_guest``), when guest sessions are enabled.

    Fails closed: with no configured secret every request is refused, so an
    unconfigured deployment is unusable rather than open.

    **CSRF.** A request authenticated from the cookie must, on a state-changing
    method, echo the session's CSRF token in the ``X-CSRF-Token`` header. A
    cross-site page can make the browser *send* the cookie, but it cannot read the
    token (the cookie is ``HttpOnly`` and the token is only ever delivered in a
    JSON body to a same-origin request), so it cannot forge this header. A
    header-authenticated request is exempt: no browser attaches an
    ``Authorization`` header cross-origin, so it is not reachable by CSRF.
    """
    if not settings.auth.configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Authentication is not configured on this server. Set "
                "PACT_AUTH_SECRET before serving requests."
            ),
        )

    # A bearer header, when present, is authoritative and short-circuits the cookie.
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise _unauthorized("Authorization header must be 'Bearer <token>'")
        claims = _decode(token.strip())
        principal = _principal_from_claims(claims)
        _assert_not_scoped(principal)
        return principal

    principal = _principal_from_guest_cookie(request)
    if principal is None:
        raise _unauthorized("Missing authentication token")
    _enforce_csrf(request, principal, x_csrf_token)
    return principal


def _principal_from_guest_cookie(request: Request) -> Principal | None:
    """Resolve a guest principal from the session cookie, or ``None``.

    Returns ``None`` (never raises) when guest sessions are disabled, the cookie is
    absent, or the token does not verify — the caller turns that into a 401.

    The token's ``ver`` is checked against the guest's *authoritative* token
    version, so a revoked or deleted workspace is refused on every route, not only
    on the money routes. This is one indexed read; it is what makes a guest session
    server-managed rather than merely signed.
    """
    from ..core.guests import decode_guest_session, guests

    if not settings.auth.guests_enabled:
        return None
    raw = request.cookies.get(GUEST_COOKIE)
    if not raw:
        return None
    claims = decode_guest_session(raw)
    if claims is None:
        return None
    subject = str(claims.get("sub"))
    version = claims.get("ver")
    authoritative = guests.token_version_of(subject)
    # -1 means the workspace no longer exists or is not active.
    if authoritative < 0 or not isinstance(version, int) or version != authoritative:
        return None
    return Principal(
        subject=subject,
        role=ROLE_BUYER,
        token_version=version,
        via_cookie=True,
        csrf_token=claims.get("csrf") if isinstance(claims.get("csrf"), str) else None,
    )


def _enforce_csrf(request: Request, principal: Principal, provided: str | None) -> None:
    from ..core.guests import csrf_matches

    if request.method.upper() not in MUTATING_METHODS:
        return
    if not csrf_matches(principal.csrf_token or "", provided):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid CSRF token",
        )


def require_buyer(principal: Annotated[Principal, Depends(require_principal)]) -> Principal:
    """A buyer, or an admin acting on the buyer side."""
    if not (principal.is_buyer or principal.is_admin):
        raise _forbidden("This action requires a buyer or admin credential")
    return principal


def require_seller(principal: Annotated[Principal, Depends(require_principal)]) -> Principal:
    """A seller, or an admin."""
    if not (principal.is_seller or principal.is_admin):
        raise _forbidden("This action requires a seller or admin credential")
    return principal


def require_admin(principal: Annotated[Principal, Depends(require_principal)]) -> Principal:
    """Administrative operations only: reconcile, cancel, delete, global views."""
    if not principal.is_admin:
        raise _forbidden("This action requires an admin credential")
    return principal


def require_verified(principal: Annotated[Principal, Depends(require_principal)]) -> Principal:
    """A gate for money-bearing operations, layered *on top of* the role gates.

    This never widens access: it only ever adds a refusal to a route that already
    required a buyer or seller credential. It reads the registered account's
    authoritative status and token version from the shared store and refuses
    (503) if that store cannot be reached — a sensitive operation must fail closed.

    A **legacy operator token** whose subject has no customer row is treated as a
    pre-verified operator credential and passes. This is a deliberate, narrowly
    scoped exception: it is what keeps the existing operator workflow working, and
    it is documented in docs/AUTH.md. It does not bypass any role or ownership
    check, only the email-verification requirement.
    """
    from ..core.auth_store import AccountStateError, auth_store

    try:
        state = auth_store.account_state(principal.subject)
    except AccountStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account state is temporarily unavailable; try again shortly.",
        ) from exc

    if state is None:
        # No customer row: a legacy operator credential. Pre-verified by
        # construction; role and ownership checks still apply elsewhere.
        return principal

    account_status, token_version, email_verified = state
    if account_status != "active":
        raise _forbidden("This account is suspended.")
    if not email_verified:
        raise _forbidden("Verify your email address before this action.")
    # A token version mismatch means the token was revoked (password reset,
    # suspension, or an explicit revocation). Only enforced when the token carries
    # a version; a legacy operator token has none and is handled above.
    if principal.token_version is not None and token_version != principal.token_version:
        raise _forbidden("This credential has been revoked. Sign in again.")
    return principal


def principal_from_token(token: str) -> Principal:
    """Verify a raw token string and return its principal, or raise 401.

    Shared by the header dependency and the SSE query-parameter path so both use
    exactly the same verification. No code path may bypass this.
    """
    if not settings.auth.configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Authentication is not configured on this server. Set "
                "PACT_AUTH_SECRET before serving requests."
            ),
        )
    if not token or not token.strip():
        raise _unauthorized("Missing authentication token")
    claims = _decode(token.strip())
    principal = _principal_from_claims(claims)
    _assert_not_scoped(principal)
    return principal


def issue_stream_ticket(principal: Principal, job_id: str) -> str:
    """Mint a short-lived, single-job ticket for the SSE stream.

    ``EventSource`` cannot set an ``Authorization`` header, so the token must
    travel in the URL — where it lands in server logs, browser history, and any
    ``Referer``. Sending the full 30-day token that way would put a credential
    that can move money into a place built to record text. Instead the client
    exchanges its real token (over the ``Authorization`` header, on a normal
    request) for this ticket, which is scoped to one job and expires in a minute.
    """
    import jwt

    signing_secret = settings.auth.secret
    if not signing_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured on this server.",
        )
    issued = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": principal.subject,
        "role": principal.role,
        "scope": stream_scope(job_id),
        "jti": uuid.uuid4().hex,
        "iat": int(issued.timestamp()),
        "exp": int((issued + STREAM_TICKET_TTL).timestamp()),
    }
    return jwt.encode(payload, signing_secret, algorithm=_algorithm())


def require_stream_principal(
    job_id: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    ticket: Annotated[str | None, Query()] = None,
) -> Principal:
    """Auth for the SSE stream, which the browser cannot send headers on.

    Three paths, all strictly verified:

    * an ``Authorization`` header (a non-browser client, or a test), which must
      carry a full token — never a scoped ticket; or
    * a ``?ticket=`` query parameter, which must carry a *stream ticket scoped to
      this exact job*. A full token in the query is refused, so a leaked URL can
      never expose the caller's real credential; or
    * the guest session cookie, for the browser case where the client is a guest.

    The cookie path is safe here for the same reason it is on every other route:
    the guest session is ``SameSite=Lax``, so a cross-site ``EventSource`` does not
    carry it, and the stream is a read of the caller's own job (ownership is
    checked immediately after).
    """
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            raise _unauthorized("Authorization header must be 'Bearer <token>'")
        return principal_from_token(value)
    if ticket:
        return _principal_from_stream_ticket(job_id, ticket)
    principal = _principal_from_guest_cookie(request)
    if principal is not None:
        return principal
    raise _unauthorized("Missing Authorization header or stream ticket")


def _principal_from_stream_ticket(job_id: str, ticket: str) -> Principal:
    # Fail closed before decoding, exactly like the header paths. Without this an
    # unconfigured deployment would reach the verifier with an empty key.
    if not settings.auth.configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Authentication is not configured on this server. Set "
                "PACT_AUTH_SECRET before serving requests."
            ),
        )
    claims = _decode(ticket.strip())
    principal = _principal_from_claims(claims)
    if principal.scope != stream_scope(job_id):
        raise _forbidden("Stream ticket is not valid for this job")
    # Single-use: a ticket travels in a URL, so a captured one must not be
    # replayable for its whole lifetime. Consume it atomically; a replay is
    # refused. It is retained for the ticket's remaining life so the entry cannot
    # outlive the token it guards.
    if principal.ticket_id is None or not _consume_ticket(
        principal.ticket_id, float(claims.get("exp", 0)) - time.time()
    ):
        raise _unauthorized("Stream ticket has already been used")
    return principal


def assert_can_access_job(principal: Principal, job: object) -> None:
    """Authorize access to a job, by role.

    * admin   — any job.
    * buyer   — only jobs they own (``job.buyer_id == principal.subject``).
    * seller  — only jobs they have been invited to or won.

    A buyer who does not own the job gets 403. A seller who is not party to it
    also gets 403. Neither learns whether the job exists from the status code of
    a route they already reached; the 404/403 split is intentional for buyers
    (see the routes) but the ownership check here is the single source of truth.
    """
    buyer_id = getattr(job, "buyer_id", None)
    seller_id = getattr(job, "seller_id", None)
    invited = {
        getattr(o, "seller_id", None)
        for o in getattr(job, "offers", []) or []
    }
    invited.discard(None)

    if principal.is_admin:
        return
    if principal.is_buyer and buyer_id == principal.subject:
        return
    if principal.is_seller and (seller_id == principal.subject or principal.subject in invited):
        return
    raise _forbidden("You are not authorized to access this job")


__all__ = [
    "Principal",
    "ROLE_BUYER",
    "ROLE_SELLER",
    "ROLE_ADMIN",
    "VALID_ROLES",
    "DEFAULT_TOKEN_TTL",
    "STREAM_TICKET_TTL",
    "TOKEN_ISSUER",
    "TOKEN_AUDIENCE",
    "GUEST_COOKIE",
    "GUEST_COOKIE_PATH",
    "CSRF_HEADER",
    "MUTATING_METHODS",
    "stream_scope",
    "issue_token",
    "issue_stream_ticket",
    "principal_from_token",
    "require_principal",
    "require_buyer",
    "require_seller",
    "require_admin",
    "require_stream_principal",
    "assert_can_access_job",
]
