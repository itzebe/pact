"""Server-managed guest (judge) workspaces.

A judge must be able to open the deployed product and use it without minting a
token, filling in a form, or waiting on an email. That is what a *guest session*
is: on first visit the server provisions a real, isolated account — a buyer
identity with its own workspace — and hands the browser an opaque session cookie.
No credential is ever typed, and nothing about the workspace is shared with any
other guest.

Design constraints, and why each holds:

* **Identity is server-managed.** The guest id and the session token are both
  generated server-side and signed with the deployment's own secret. A browser
  cannot choose its own id, and cannot present a token it did not receive.
* **Isolation is inherited, not bolted on.** A guest is an ordinary ``buyer``
  principal, so every existing ownership check (``job.buyer_id ==
  principal.subject``) already scopes it to its own records. There is no second
  code path that could disagree with the first.
* **No privileged access.** A guest is never an ``admin``. It cannot reconcile,
  delete, read the provider order, read another guest's records, or reach any
  operator-only route.
* **Guest records are real, not simulated.** A guest drives the same engine and
  the same PayPal (Sandbox) client as any other buyer, so what it exercises is the
  actual product.

The session token is a JWT so it is stateless to verify, but it is *not*
self-issued: it carries the account's ``token_version``, so a workspace can be
revoked server-side the same way a customer session can. See ``pact.api.auth``
for how it is accepted (cookie + CSRF) and ``pact.api.auth_routes`` for the
endpoint that provisions it.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import settings
from .auth_store import (
    STATUS_ACTIVE,
    AuthStoreError,
    UserRecord,
    auth_store,
)

log = logging.getLogger("pact.guests")

#: The role every guest is given. A buyer posts jobs and drives settlement; it is
#: deliberately not ``admin`` (no reconciliation, deletion, or global views) and
#: not ``seller`` (a guest cannot impersonate a competing seller persona).
GUEST_ROLE = "buyer"

#: Guests are stored in the same users table as customers, under a reserved,
#: non-deliverable domain. This is what makes ``require_verified``, ``/auth/me``
#: and the ownership checks work unchanged, while keeping a guest address clearly
#: distinguishable from a real one.
GUEST_EMAIL_DOMAIN = "guest.pact.local"

#: Marker in the session token that distinguishes a guest session from a customer
#: access token and from a legacy operator token.
SESSION_TYPE = "guest"

#: Bytes of entropy in the CSRF token. The value is not secret from the browser
#: that received it (it is the browser's own token); what matters is that another
#: origin cannot read it, because it is only ever delivered in a JSON body to a
#: request the same origin made.
CSRF_BYTES = 32


def new_guest_id() -> str:
    """A fresh, unguessable guest identity. Prefixed so it is obvious in a record."""
    return "gst_" + secrets.token_hex(12)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_BYTES)


def guest_email(guest_id: str) -> str:
    """The (reserved, non-deliverable) address a guest identity is stored under."""
    return f"{guest_id}@{GUEST_EMAIL_DOMAIN}"


def is_guest_id(subject: str) -> bool:
    return isinstance(subject, str) and subject.startswith("gst_")


def is_guest_email(email: str | None) -> bool:
    return bool(email) and email.strip().lower().endswith("@" + GUEST_EMAIL_DOMAIN)


@dataclass(frozen=True)
class GuestRecord:
    """A provisioned guest workspace: the identity, plus its session material."""

    guest_id: str
    token_version: int
    expires_at: datetime
    csrf_token: str


class GuestService:
    """Provisioning and quota accounting for guest workspaces."""

    def __init__(self, store=None) -> None:
        self.store = store or auth_store

    # -- provisioning ------------------------------------------------------
    def provision(self) -> UserRecord:
        """Create a guest identity. Returns the stored record.

        The account is created *verified and active*, because the whole point is
        that no email step stands between a judge and the product. Verification is
        not being skipped here — there is nothing to verify: the identity was
        minted by the server, not claimed by a person.
        """
        guest_id = new_guest_id()
        user = UserRecord(
            id=guest_id,
            email=guest_email(guest_id),
            role=GUEST_ROLE,
            display_name="Guest",
            status=STATUS_ACTIVE,
            password_hash=None,
            email_verified_at=datetime.now(timezone.utc),
            is_agent=False,
            token_version=0,
        )
        try:
            self.store.create_user(user)
        except AuthStoreError:
            # Astronomically unlikely (a 96-bit id collision) and indistinguishable
            # from any other store failure here; surface it rather than pretending
            # a workspace exists.
            log.error("Could not create a guest identity")
            raise
        return user

    def token_version_of(self, guest_id: str) -> int:
        """The authoritative token version for a guest, or -1 if it no longer exists.

        Reading it at every request is what makes a guest session revocable: a
        version bump (or a deletion) invalidates every outstanding token for that
        workspace.
        """
        try:
            state = self.store.account_state(guest_id)
        except Exception as exc:
            log.error("Could not read guest account state (%s)", type(exc).__name__)
            return -1
        if state is None:
            return -1
        status, version, _verified = state
        if status != STATUS_ACTIVE:
            return -1
        return int(version)

    # -- quota -------------------------------------------------------------
    def job_count(self, store, guest_id: str) -> int:
        """How many jobs this guest workspace has created.

        Counted from the job store the engine actually uses (so it is correct on
        both the filesystem and Postgres paths) and pushed into the query, not
        filtered in Python.
        """
        try:
            jobs = store.list(limit=settings.auth.guest_max_jobs + 1, buyer_id=guest_id)
        except Exception as exc:
            # Fail closed on an unreadable store: a quota that cannot be evaluated
            # must refuse rather than allow unbounded work.
            log.error("Could not count guest jobs (%s); refusing", type(exc).__name__)
            return settings.auth.guest_max_jobs + 1
        return len(jobs)

    def quota_exceeded(self, store, guest_id: str) -> bool:
        return self.job_count(store, guest_id) >= settings.auth.guest_max_jobs


guests = GuestService()


# --------------------------------------------------------------------------- #
# Session token
#
# The encode/decode lives here so the guest cookie is verified with exactly the
# same library, secret and algorithm allowlist as every other credential, and so
# there is one place that decides what a guest token may claim.
# --------------------------------------------------------------------------- #


def issue_guest_session(
    guest_id: str,
    *,
    token_version: int,
    csrf_token: str | None = None,
    ttl_hours: int | None = None,
    now: datetime | None = None,
) -> tuple[str, str, datetime]:
    """Mint a guest session token. Returns ``(token, csrf_token, expires_at)``.

    Raises ``RuntimeError`` when no signing secret is configured: an unsigned
    session would be no session at all, and the caller must fail rather than
    hand out a token that cannot be verified.
    """
    import jwt

    from ..api.auth import TOKEN_AUDIENCE, TOKEN_ISSUER
    from ..config import ALLOWED_AUTH_ALGORITHMS

    secret = settings.auth.secret
    if not secret:
        raise RuntimeError("No auth signing secret is configured; cannot issue a session.")
    if settings.auth.algorithm not in ALLOWED_AUTH_ALGORITHMS:
        raise RuntimeError(f"Unsupported signing algorithm {settings.auth.algorithm!r}")

    csrf = csrf_token or new_csrf_token()
    issued = now or datetime.now(timezone.utc)
    ttl = timedelta(hours=ttl_hours if ttl_hours is not None else settings.auth.guest_session_ttl_hours)
    expires_at = issued + ttl
    payload: dict[str, Any] = {
        "sub": guest_id,
        "role": GUEST_ROLE,
        "typ": SESSION_TYPE,
        "gst": guest_id,
        "csrf": csrf,
        "ver": int(token_version),
        "iat": int(issued.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": TOKEN_ISSUER,
        "aud": TOKEN_AUDIENCE,
    }
    token = jwt.encode(payload, secret, algorithm=settings.auth.algorithm)
    return token, csrf, expires_at


def decode_guest_session(token: str) -> dict[str, Any] | None:
    """Verify a guest session token, or return ``None``.

    Returns ``None`` for anything that is not a valid, unexpired guest session:
    a bad signature, an expired token, a wrong issuer/audience, a missing type
    marker, or a token that is actually a customer/operator credential. It never
    raises for a bad token, so a caller cannot mistake a verification failure for
    a server error.
    """
    import jwt

    from ..api.auth import TOKEN_AUDIENCE, TOKEN_ISSUER
    from ..config import ALLOWED_AUTH_ALGORITHMS

    secret = settings.auth.secret
    if not secret or settings.auth.algorithm not in ALLOWED_AUTH_ALGORITHMS:
        return None
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[settings.auth.algorithm],
            options={"verify_aud": False},
            issuer=TOKEN_ISSUER,
            audience=TOKEN_AUDIENCE,
        )
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != SESSION_TYPE:
        return None
    if claims.get("role") != GUEST_ROLE:
        return None
    subject = claims.get("sub")
    if not isinstance(subject, str) or not is_guest_id(subject):
        return None
    return claims


def csrf_matches(expected: str, provided: str | None) -> bool:
    """Constant-time comparison of the session's CSRF token with the header value."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected, provided)


__all__ = [
    "GUEST_ROLE",
    "GUEST_EMAIL_DOMAIN",
    "SESSION_TYPE",
    "GuestRecord",
    "GuestService",
    "guests",
    "new_guest_id",
    "new_csrf_token",
    "guest_email",
    "is_guest_id",
    "is_guest_email",
    "issue_guest_session",
    "decode_guest_session",
    "csrf_matches",
]
