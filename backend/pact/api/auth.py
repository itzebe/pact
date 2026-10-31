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

from fastapi import Depends, Header, HTTPException, Query, status

from ..config import ALLOWED_AUTH_ALGORITHMS, settings
from ..core.ticket_store import ticket_store

log = logging.getLogger("pact.auth")

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

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_buyer(self) -> bool:
        return self.role == ROLE_BUYER

    @property
    def is_seller(self) -> bool:
        return self.role == ROLE_SELLER


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
        return jwt.decode(token, settings.auth.secret, algorithms=[_algorithm()])
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


def _principal_from_claims(claims: dict[str, Any]) -> Principal:
    subject = claims.get("sub")
    role = claims.get("role")
    if not isinstance(subject, str) or not subject:
        raise _unauthorized("Token is missing a subject")
    if role not in VALID_ROLES:
        raise _unauthorized("Token carries an unknown role")
    scope = claims.get("scope")
    jti = claims.get("jti")
    return Principal(
        subject=subject,
        role=role,
        scope=scope if isinstance(scope, str) else None,
        ticket_id=jti if isinstance(jti, str) else None,
    )


def _assert_not_scoped(principal: Principal) -> None:
    """Refuse a capability-scoped token on an ordinary route.

    A stream ticket is short-lived and single-purpose. Accepting it here would
    widen the blast radius of a leaked URL to every money route.
    """
    if principal.scope:
        raise _unauthorized("A scoped token is not valid for this route")


def require_principal(
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """FastAPI dependency: resolve and verify the caller's bearer token.

    Fails closed: with no configured secret every request is refused, so an
    unconfigured deployment is unusable rather than open.
    """
    if not settings.auth.configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Authentication is not configured on this server. Set "
                "PACT_AUTH_SECRET before serving requests."
            ),
        )

    if not authorization:
        raise _unauthorized("Missing Authorization header")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("Authorization header must be 'Bearer <token>'")

    claims = _decode(token.strip())
    principal = _principal_from_claims(claims)
    _assert_not_scoped(principal)
    return principal


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
    authorization: Annotated[str | None, Header()] = None,
    ticket: Annotated[str | None, Query()] = None,
) -> Principal:
    """Auth for the SSE stream, which the browser cannot send headers on.

    Two paths, both strictly verified:

    * an ``Authorization`` header (a non-browser client, or a test), which must
      carry a full token — never a scoped ticket; or
    * a ``?ticket=`` query parameter, which must carry a *stream ticket scoped to
      this exact job*. A full token in the query is refused, so a leaked URL can
      never expose the caller's real credential.
    """
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            raise _unauthorized("Authorization header must be 'Bearer <token>'")
        return principal_from_token(value)
    if not ticket:
        raise _unauthorized("Missing Authorization header or stream ticket")
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
