"""Customer account lifecycle: registration, verification, sessions, recovery.

This module is the service layer behind the ``/api/auth/*`` routes. It owns the
decisions that must not be re-implemented per route:

* **registration** creates only buyer/seller accounts, never an admin, and never
  reveals whether an email is already registered;
* **login** is rate-limited and returns one generic failure for every reason a
  credential can be rejected, so it is not an enumeration oracle;
* **sessions** are a 15-minute access JWT plus a rotating, single-use refresh
  token whose reuse revokes the whole family;
* **revocation** is a ``token_version`` bump plus a status change, enforced at
  login, at refresh, and at every sensitive operation;
* **email** is queued into a transactional outbox, never sent inline in a way
  that could be lost if the process dies.

Reserved machine identities (the LLM seller personas and any pre-provisioned
legacy identity) cannot be registered by a customer.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..config import settings
from . import passwords
from .auth_store import (
    REGISTERABLE_ROLES,
    STATUS_ACTIVE,
    STATUS_SUSPENDED,
    AccountStateError,
    AuthStoreError,
    OutboxMessage,
    UserRecord,
    auth_store,
    hash_identifier,
    is_valid_email,
    new_opaque_token,
    normalize_email,
    token_hash,
)

log = logging.getLogger("pact.accounts")

#: Verification link lifetime. Short, single-use.
VERIFY_TTL = timedelta(hours=1)
#: Password-reset link lifetime. Short, single-use.
RESET_TTL = timedelta(minutes=30)
#: Login throttle: this many failures for one identifier within the window locks
#: further attempts for the remainder of the window.
LOGIN_FAILURE_LIMIT = 8
LOGIN_FAILURE_WINDOW_SECONDS = 900
#: Minimum seconds between resends to the same address (email-bomb defence).
RESEND_MIN_INTERVAL_SECONDS = 60

#: Uniform messages. Registration and reset never differ by whether the address
#: exists; only the mailbox owner can tell that a message arrived.
REGISTER_MESSAGE = "Check your email to verify your account."
RESET_MESSAGE = "If that account exists, we've sent reset instructions."
GENERIC_LOGIN_ERROR = "Invalid email or password."


@dataclass
class AuthError(Exception):
    """A client-visible failure with a safe, generic message."""

    status_code: int
    detail: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _reserved_identities() -> set[str]:
    """Identities that public registration may never create.

    The LLM seller personas (``PACT_SELLERS``) are machine identities; a customer
    registering as one would inherit its reputation and job access. Any operator
    that wants extra reserved names can add them here via the environment.
    """
    reserved = {s.strip() for s in settings.sellers.split(",") if s.strip()}
    reserved |= {s.strip() for s in os.getenv("PACT_RESERVED_IDENTITIES", "").split(",") if s.strip()}
    return {r.lower() for r in reserved}


def _user_id_for(email: str) -> str:
    """A stable, non-email-derived user id.

    Deliberately not the email: an id that is the address would leak it into every
    job, ledger entry, and URL that carries a user id.
    """
    return "usr_" + secrets.token_hex(8)


class AccountsService:
    def __init__(self, store=None) -> None:
        self.store = store or auth_store

    # -- registration ------------------------------------------------------
    def register(self, *, email: str, password: str, role: str, display_name: str | None = None,
                 ip_hash: str | None = None) -> str:
        """Create a customer account and queue a verification email.

        Returns the uniform message regardless of whether the address already
        existed, so the response is not an existence oracle. A role outside the
        registerable set (notably ``admin``) is refused.
        """
        if role not in REGISTERABLE_ROLES:
            raise AuthError(400, "Registration is only available for buyer and seller accounts.")
        if not is_valid_email(email):
            raise AuthError(400, "Enter a valid email address.")
        _validate_password(password)

        normalized = normalize_email(email)
        if normalized in _reserved_identities():
            raise AuthError(400, "That identity is reserved.")

        existing = self.store.get_user_by_email(normalized)
        if existing is not None:
            # Do not reveal existence. If the account is real and unverified,
            # resend; either way the response is identical.
            if not existing.email_verified and existing.status == STATUS_ACTIVE:
                self._queue_verification(existing)
            else:
                self._queue_reset(existing, ip_hash=ip_hash)
            self.store.record_event("register_duplicate", actor_id=existing.id, ip_hash=ip_hash)
            return REGISTER_MESSAGE

        user = UserRecord(
            id=_user_id_for(normalized),
            email=normalized,
            role=role,
            display_name=display_name,
            password_hash=passwords.hash_password(password),
            status=STATUS_ACTIVE,
        )
        try:
            self.store.create_user(user)
        except AuthStoreError:
            # A racing registration won the unique index; behave like a duplicate.
            return REGISTER_MESSAGE
        self._queue_verification(user)
        self.store.record_event("register", actor_id=user.id, ip_hash=ip_hash)
        return REGISTER_MESSAGE

    def verify_email(self, raw_token: str) -> str:
        user_id = self.store.consume_email_verification(raw_token)
        if user_id is None:
            raise AuthError(400, "That verification link is invalid or has expired.")
        self.store.update_user(user_id, email_verified_at=_now())
        self.store.record_event("email_verified", actor_id=user_id)
        return "Your email address is verified."

    # -- login / sessions --------------------------------------------------
    def login(self, *, email: str, password: str, ip_hash: str | None = None,
              user_agent: str | None = None) -> dict:
        normalized = normalize_email(email)
        identifier = hash_identifier(normalized)

        if self.store.count_recent_failures("login", identifier, LOGIN_FAILURE_WINDOW_SECONDS) >= LOGIN_FAILURE_LIMIT:
            self.store.record_event("login_throttled", ip_hash=ip_hash)
            raise AuthError(429, "Too many attempts. Try again later.")

        user = self.store.get_user_by_email(normalized)
        # Always do password work, so a missing user and a wrong password take the
        # same time. This is what keeps login from being a timing oracle.
        stored = user.password_hash if user and user.password_hash else passwords.hash_password("placeholder")
        ok = passwords.verify_password(password, stored)
        if user is None or not ok:
            self.store.record_attempt("login", identifier, False)
            self.store.record_event("login_failed", ip_hash=ip_hash)
            raise AuthError(401, GENERIC_LOGIN_ERROR)

        if user.status != STATUS_ACTIVE:
            self.store.record_attempt("login", identifier, False)
            self.store.record_event("login_suspended", actor_id=user.id, ip_hash=ip_hash)
            raise AuthError(401, GENERIC_LOGIN_ERROR)

        # A verified account is required to hold a session. An unverified account
        # gets the same generic failure and a fresh verification email, so the
        # caller cannot distinguish it from a wrong password.
        if not user.email_verified:
            self.store.record_attempt("login", identifier, False)
            self._queue_verification(user)
            raise AuthError(401, GENERIC_LOGIN_ERROR)

        self.store.record_attempt("login", identifier, True)
        self.store.update_user(user.id, last_login_at=_now())
        self.store.record_event("login", actor_id=user.id, ip_hash=ip_hash)
        return self._issue_session(user, user_agent=user_agent, ip_hash=ip_hash)

    def refresh(self, raw_refresh: str, *, user_agent: str | None = None, ip_hash: str | None = None) -> dict:
        """Rotate a refresh token, or revoke the family on reuse.

        A token presented twice is treated as theft: the whole family is revoked,
        so a stolen token cannot be used to keep a session alive.
        """
        result = self.store.rotate_refresh_token(raw_refresh, timedelta(days=settings.auth.refresh_token_ttl_days))
        if result is None:
            raise AuthError(401, "Your session has expired. Sign in again.")
        if result == "reuse":
            # Reuse: revoke every token in the family. We do not know the family
            # from a spent token alone, so revoke by user after looking it up.
            family = self._family_of(raw_refresh)
            if family:
                self.store.revoke_family(family)
            self.store.record_event("refresh_reuse_detected", ip_hash=ip_hash)
            raise AuthError(401, "Your session was ended for security reasons. Sign in again.")

        user_id, family_id, new_raw = result
        user = self.store.get_user_by_id(user_id)
        if user is None or user.status != STATUS_ACTIVE:
            self.store.revoke_family(family_id)
            raise AuthError(401, GENERIC_LOGIN_ERROR)
        state = self._account_state(user_id)
        if state is None or not state[2]:
            self.store.revoke_family(family_id)
            raise AuthError(401, GENERIC_LOGIN_ERROR)
        return {
            "access_token": _issue_access_token(user),
            "refresh_token": new_raw,
            "user": _public_user(user),
        }

    def logout(self, raw_refresh: str | None) -> None:
        if not raw_refresh:
            return
        family = self._family_of(raw_refresh)
        if family:
            self.store.revoke_family(family)

    def _family_of(self, raw_refresh: str) -> str | None:
        getter = getattr(self.store, "family_of", None)
        if getter is None:
            return None
        return getter(token_hash(raw_refresh))

    def _issue_session(self, user: UserRecord, *, user_agent: str | None, ip_hash: str | None) -> dict:
        family_id = secrets.token_hex(16)
        raw_refresh = new_opaque_token()
        self.store.create_refresh_token(
            raw_refresh, user.id, family_id, timedelta(days=settings.auth.refresh_token_ttl_days)
        )
        return {
            "access_token": _issue_access_token(user),
            "refresh_token": raw_refresh,
            "user": _public_user(user),
        }

    # -- password reset / change ------------------------------------------
    def request_password_reset(self, *, email: str, ip_hash: str | None = None) -> str:
        normalized = normalize_email(email)
        user = self.store.get_user_by_email(normalized)
        if user is not None and user.status == STATUS_ACTIVE:
            self._queue_reset(user, ip_hash=ip_hash)
        self.store.record_event("password_reset_requested", ip_hash=ip_hash)
        return RESET_MESSAGE

    def reset_password(self, *, raw_token: str, new_password: str) -> str:
        _validate_password(new_password)
        user_id = self.store.consume_password_reset(raw_token)
        if user_id is None:
            raise AuthError(400, "That reset link is invalid or has expired.")
        user = self.store.get_user_by_id(user_id)
        if user is None:
            raise AuthError(400, "That reset link is invalid or has expired.")
        self._set_password_and_revoke(user_id, new_password)
        self.store.record_event("password_reset", actor_id=user_id)
        return "Your password has been reset. Sign in with the new password."

    def change_password(self, *, user_id: str, current_password: str, new_password: str) -> str:
        user = self.store.get_user_by_id(user_id)
        if user is None or not user.password_hash:
            raise AuthError(400, "This account has no password to change.")
        if not passwords.verify_password(current_password, user.password_hash):
            raise AuthError(401, "Current password is incorrect.")
        _validate_password(new_password)
        self._set_password_and_revoke(user_id, new_password)
        self.store.record_event("password_changed", actor_id=user_id)
        return "Your password has been changed. Other sessions have been signed out."

    def _set_password_and_revoke(self, user_id: str, new_password: str) -> None:
        """Set a new password, bump the token version, and revoke every session."""
        self.store.update_user(
            user_id,
            password_hash=passwords.hash_password(new_password),
            token_version=self._bump_token_version(user_id),
        )
        self.store.revoke_all_for_user(user_id)

    def _bump_token_version(self, user_id: str) -> int:
        state = self._account_state(user_id)
        current = state[1] if state else 0
        return current + 1

    # -- account state -----------------------------------------------------
    def _account_state(self, user_id: str) -> tuple[str, int, bool] | None:
        try:
            return self.store.account_state(user_id)
        except AccountStateError as exc:
            # Fail closed: a sensitive path must not proceed on unknown state.
            raise AuthError(503, "Account state is temporarily unavailable.") from exc

    def is_verified_and_active(self, user_id: str) -> bool:
        """Used by the sensitive-operation gate. Fails closed on any error."""
        state = self._account_state(user_id)
        if state is None:
            return False
        status, _version, verified = state
        return status == STATUS_ACTIVE and verified

    def suspend(self, user_id: str) -> None:
        """Suspend an account and revoke its sessions immediately."""
        self.store.update_user(user_id, status=STATUS_SUSPENDED, token_version=self._bump_token_version(user_id))
        self.store.revoke_all_for_user(user_id)
        self.store.record_event("account_suspended", actor_id=user_id)

    def reinstate(self, user_id: str) -> None:
        self.store.update_user(user_id, status=STATUS_ACTIVE, token_version=self._bump_token_version(user_id))
        self.store.record_event("account_reinstated", actor_id=user_id)

    def revoke_tokens(self, user_id: str) -> None:
        """Coarse revocation: bump the version and drop every refresh family."""
        self.store.update_user(user_id, token_version=self._bump_token_version(user_id))
        self.store.revoke_all_for_user(user_id)
        self.store.record_event("tokens_revoked", actor_id=user_id)

    # -- email queueing ----------------------------------------------------
    def _public_link(self, kind: str, raw_token: str) -> str | None:
        base = settings.public_url
        if not base:
            return None
        path = "/verify-email" if kind == "verify" else "/reset-password"
        return f"{base}{path}?token={raw_token}"

    def _queue_verification(self, user: UserRecord) -> None:
        raw = new_opaque_token()
        th = self.store.create_email_verification(raw, user.id, user.email, VERIFY_TTL)
        # A fresh token per send, so the idempotency key is unique per delivery
        # event — a legitimate future resend is never blocked by an old key.
        event_key = f"verify:{user.id}:{th[:16]}"
        self.store.enqueue_email(
            OutboxMessage(
                id=secrets.token_hex(8), kind="verify_email", user_id=user.id,
                to_email=user.email, token_hash=th, idempotency_key=event_key, attempts=0,
                token_raw=raw,
            )
        )

    def _queue_reset(self, user: UserRecord, *, ip_hash: str | None) -> None:
        raw = new_opaque_token()
        th = self.store.create_password_reset(raw, user.id, RESET_TTL, ip_hash)
        event_key = f"reset:{user.id}:{th[:16]}"
        self.store.enqueue_email(
            OutboxMessage(
                id=secrets.token_hex(8), kind="password_reset", user_id=user.id,
                to_email=user.email, token_hash=th, idempotency_key=event_key, attempts=0,
                token_raw=raw,
            )
        )


def _validate_password(password: str) -> None:
    if not password or len(password) < 12:
        raise AuthError(400, "Choose a password of at least 12 characters.")


def _issue_access_token(user: UserRecord) -> str:
    """Mint a short-lived HS256 access token carrying the account's token version."""
    import jwt

    from ..api.auth import TOKEN_AUDIENCE, TOKEN_ISSUER

    secret = settings.auth.secret
    if not secret:
        raise AuthError(503, "Authentication is not configured on this server.")
    issued = _now()
    payload = {
        "sub": user.id,
        "role": user.role,
        "ver": user.token_version,
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(minutes=settings.auth.access_token_ttl_minutes)).timestamp()),
        "iss": TOKEN_ISSUER,
        "aud": TOKEN_AUDIENCE,
    }
    return jwt.encode(payload, secret, algorithm=settings.auth.algorithm)


def _public_user(user: UserRecord) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "display_name": user.display_name,
        "email_verified": user.email_verified,
        "status": user.status,
    }


accounts = AccountsService()
