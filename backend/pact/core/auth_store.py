"""Durable store for customer accounts, sessions, and the email outbox.

This is the server-side half of the customer-authentication design. It holds the
identity records that a JWT's ``sub`` refers to, the rotating refresh-token
family, the per-account token version used for coarse revocation, the login
rate-limit ledger, and the transactional email outbox.

Two implementations exist and share one interface:

* :class:`PostgresAuthStore` — the production path, and the only one that gives
  cross-instance correctness (a suspension seen by one instance is seen by all).
* :class:`InMemoryAuthStore` — local development and the tests that do not need a
  database. It is honest about being process-local: it is correct only where the
  deployment is single-instance by construction.

Selection mirrors the other stores: ``DATABASE_URL`` set means Postgres; unset
means the deployment is single-instance, so a process-local store is honest.

Security properties that live in the database, not in the process:

* a refresh token is single-use — rotation is one conditional ``UPDATE`` and the
  loser of a race sees it as a *reuse* (see :meth:`rotate_refresh_token`);
* the account status and ``token_version`` are read at login, at refresh, and at
  every sensitive operation, so a suspension or a revocation is enforced across
  every instance (see :meth:`get_account_state`);
* the email outbox claim is ``FOR UPDATE SKIP LOCKED``, so concurrent dispatchers
  never claim the same row and a crashed dispatcher's lease is reclaimable.

No secret, password, raw token, or raw email address is ever logged here.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import settings

log = logging.getLogger("pact.auth_store")

#: How long an email-outbox lease is valid. A dispatcher that claims a row and
#: then crashes leaves it in ``sending`` with this lease; after it expires another
#: dispatcher reclaims the row. Must exceed the provider's own request timeout.
OUTBOX_LEASE_SECONDS = 120
#: Attempts before a message is declared permanently failed (``dead``).
OUTBOX_MAX_ATTEMPTS = 6
#: Terminal outbox rows are kept this long for audit, then pruned.
OUTBOX_RETENTION_DAYS = 7
#: How many rows one cleanup call may remove. Bounds every prune.
CLEANUP_BATCH = 500

ROLE_BUYER = "buyer"
ROLE_SELLER = "seller"
ROLE_ADMIN = "admin"
#: Roles a *public* registration may ever create. Admin is deliberately absent:
#: there is no request shape that produces an administrator.
REGISTERABLE_ROLES = frozenset({ROLE_BUYER, ROLE_SELLER})

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                TEXT PRIMARY KEY,
    email             TEXT NOT NULL,
    email_verified_at TIMESTAMPTZ,
    role              TEXT NOT NULL,
    display_name      TEXT,
    password_hash     TEXT,
    status            TEXT NOT NULL DEFAULT 'active',
    is_agent          BOOLEAN NOT NULL DEFAULT false,
    token_version     INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS email_verifications (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    email       TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token_hash      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    consumed_at     TIMESTAMPTZ,
    requested_ip_hash TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    family_id  TEXT NOT NULL,
    issued_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    rotated_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    user_agent TEXT,
    ip_hash    TEXT
);

CREATE TABLE IF NOT EXISTS login_attempts (
    scope           TEXT NOT NULL,
    identifier_hash TEXT NOT NULL,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok              BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS email_outbox (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    to_email        TEXT NOT NULL,
    token_hash      TEXT,
    token_raw       TEXT,
    idempotency_key TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ,
    last_error      TEXT,
    provider_id     TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'queued',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS auth_events (
    id       TEXT PRIMARY KEY,
    at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id TEXT,
    action   TEXT NOT NULL,
    target   TEXT,
    ip_hash  TEXT,
    detail   TEXT
);
"""

# Additive, idempotent. Indexes the cleanup and lookup paths depend on; without
# them a "bounded" prune degrades into a full table scan.
MIGRATIONS = (
    "CREATE UNIQUE INDEX IF NOT EXISTS users_email_uidx ON users (email)",
    "CREATE INDEX IF NOT EXISTS users_status_idx ON users (status)",
    "CREATE INDEX IF NOT EXISTS email_verifications_expires_idx ON email_verifications (expires_at)",
    "CREATE INDEX IF NOT EXISTS email_verifications_user_idx ON email_verifications (user_id)",
    "CREATE INDEX IF NOT EXISTS password_reset_expires_idx ON password_reset_tokens (expires_at)",
    "CREATE INDEX IF NOT EXISTS password_reset_user_idx ON password_reset_tokens (user_id)",
    "CREATE INDEX IF NOT EXISTS refresh_tokens_expires_idx ON refresh_tokens (expires_at)",
    "CREATE INDEX IF NOT EXISTS refresh_tokens_user_idx ON refresh_tokens (user_id)",
    "CREATE INDEX IF NOT EXISTS refresh_tokens_family_idx ON refresh_tokens (family_id)",
    "CREATE INDEX IF NOT EXISTS login_attempts_lookup_idx ON login_attempts (scope, identifier_hash, at)",
    "CREATE INDEX IF NOT EXISTS email_outbox_due_idx ON email_outbox (status, next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS email_outbox_lease_idx ON email_outbox (lease_expires_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS email_outbox_idem_uidx ON email_outbox (idempotency_key)",
    "CREATE INDEX IF NOT EXISTS auth_events_at_idx ON auth_events (at)",
)

# ---- SQL ---------------------------------------------------------------------

# Claim due work, including rows whose *lease* has expired so a crashed
# dispatcher's message is recovered rather than stranded forever.
_CLAIM_OUTBOX_SQL = """
WITH due AS (
    SELECT id FROM email_outbox
    WHERE (status = 'pending' AND next_attempt_at <= now())
       OR (status = 'sending' AND lease_expires_at IS NOT NULL AND lease_expires_at < now())
    ORDER BY next_attempt_at
    LIMIT %s
    FOR UPDATE SKIP LOCKED
)
UPDATE email_outbox o
SET status = 'sending',
    attempts = o.attempts + 1,
    lease_expires_at = now() + make_interval(secs => %s)
FROM due
WHERE o.id = due.id
RETURNING o.id, o.kind, o.user_id, o.to_email, o.token_raw, o.idempotency_key, o.attempts;
"""

# Rotate a refresh token exactly once. A row is only rotated while it is neither
# already rotated nor revoked and still within its lifetime; RETURNING yields no
# row for the loser of a race, who then reads it and treats it as reuse.
_ROTATE_SQL = """
UPDATE refresh_tokens
SET rotated_at = now()
WHERE token_hash = %s
  AND rotated_at IS NULL
  AND revoked_at IS NULL
  AND expires_at > now()
RETURNING user_id, family_id
"""

# Bounded prune using a CTE and DELETE ... USING. A bare DELETE ... LIMIT is not
# valid PostgreSQL, and FOR UPDATE SKIP LOCKED lets concurrent pruners partition
# the work instead of blocking each other.
_PRUNE_SQL = {
    "email_verifications": """
        WITH expired AS (
            SELECT token_hash FROM email_verifications
            WHERE expires_at <= now() OR consumed_at IS NOT NULL
            ORDER BY expires_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        DELETE FROM email_verifications t
        USING expired e
        WHERE t.token_hash = e.token_hash
    """,
    "password_reset_tokens": """
        WITH expired AS (
            SELECT token_hash FROM password_reset_tokens
            WHERE expires_at <= now() OR consumed_at IS NOT NULL
            ORDER BY expires_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        DELETE FROM password_reset_tokens t
        USING expired e
        WHERE t.token_hash = e.token_hash
    """,
    "refresh_tokens": """
        WITH expired AS (
            SELECT token_hash FROM refresh_tokens
            WHERE expires_at <= now()
            ORDER BY expires_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        DELETE FROM refresh_tokens t
        USING expired e
        WHERE t.token_hash = e.token_hash
    """,
    "email_outbox": """
        WITH done AS (
            SELECT id FROM email_outbox
            WHERE status IN ('sent', 'dead', 'failed')
              AND completed_at IS NOT NULL
              AND completed_at <= now() - make_interval(days => %s)
            ORDER BY completed_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        DELETE FROM email_outbox o
        USING done d
        WHERE o.id = d.id
    """,
    "login_attempts": """
        WITH old AS (
            SELECT ctid FROM login_attempts
            WHERE at <= now() - make_interval(days => %s)
            ORDER BY at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        DELETE FROM login_attempts l
        USING old o
        WHERE l.ctid = o.ctid
    """,
}


def token_hash(raw: str) -> str:
    """Hash an opaque token for storage. The raw value is never persisted."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_opaque_token() -> str:
    """A high-entropy opaque token, safe to put in a URL or a cookie."""
    return secrets.token_urlsafe(32)


def hash_identifier(value: str) -> str:
    """A stable, non-reversible key for rate limiting and audit.

    The plaintext email is never stored in the rate-limit or audit tables, so a
    database dump does not enumerate which addresses exist or tried to log in.
    """
    salted = (settings.auth.secret or "pact") + value.strip().lower()
    return hashlib.sha256(salted.encode("utf-8")).hexdigest()[:32]


def normalize_email(email: str) -> str:
    """Canonical form used for storage and lookup."""
    return email.strip().lower()


def is_valid_email(email: str) -> bool:
    """A deliberately permissive shape check.

    Full RFC 5322 validation is not the goal — deliverability is decided by the
    verification email, not by a regex. This rejects the obviously malformed
    (no ``@``, no dot in the domain, whitespace, over-long) without pretending to
    be authoritative.
    """
    candidate = email.strip()
    if not candidate or len(candidate) > 254 or any(c.isspace() for c in candidate):
        return False
    if candidate.count("@") != 1:
        return False
    local, _, domain = candidate.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        return False
    return True


@dataclass
class UserRecord:
    id: str
    email: str
    role: str
    status: str = STATUS_ACTIVE
    password_hash: str | None = None
    email_verified_at: datetime | None = None
    is_agent: bool = False
    token_version: int = 0
    display_name: str | None = None
    last_login_at: datetime | None = None

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE


@dataclass
class OutboxMessage:
    id: str
    kind: str
    user_id: str
    to_email: str
    token_hash: str | None
    idempotency_key: str
    attempts: int
    status: str = "pending"
    delivery_status: str = "queued"
    last_error: str | None = None
    provider_id: str | None = None
    created_at: datetime | None = None
    #: The raw verification/reset token, carried so the dispatcher can build the
    #: link. It lives only until the message is sent; the persisted verification
    #: row holds only the hash.
    token_raw: str | None = None


class AccountStateError(RuntimeError):
    """Raised when the authoritative account state could not be read.

    Callers on a sensitive path must treat this as a refusal (fail closed), never
    as "the account is fine".
    """


class AuthStoreError(RuntimeError):
    """A database problem in the auth store. Callers fail closed."""


# --------------------------------------------------------------------------- #
# In-memory implementation (single-instance deployments and PG-free tests)
# --------------------------------------------------------------------------- #


class InMemoryAuthStore:
    backend = "memory"
    shared = False

    def __init__(self) -> None:
        self._users: dict[str, UserRecord] = {}
        self._by_email: dict[str, str] = {}
        self._verifications: dict[str, tuple[str, datetime]] = {}
        self._resets: dict[str, tuple[str, datetime]] = {}
        self._refresh: dict[str, dict[str, Any]] = {}
        self._attempts: list[tuple[str, str, float, bool]] = []
        self._outbox: dict[str, OutboxMessage] = {}
        self._events: list[dict[str, Any]] = []
        self._guard = threading.Lock()

    # -- users -------------------------------------------------------------
    def create_user(self, user: UserRecord) -> None:
        with self._guard:
            if user.id in self._users or user.email in self._by_email:
                raise AuthStoreError("user already exists")
            self._users[user.id] = user
            self._by_email[user.email] = user.id

    def get_user_by_id(self, user_id: str) -> UserRecord | None:
        with self._guard:
            return self._users.get(user_id)

    def get_user_by_email(self, email: str) -> UserRecord | None:
        with self._guard:
            uid = self._by_email.get(normalize_email(email))
            return self._users.get(uid) if uid else None

    def update_user(self, user_id: str, **fields: Any) -> None:
        with self._guard:
            user = self._users.get(user_id)
            if user is None:
                raise AuthStoreError("no such user")
            for key, value in fields.items():
                setattr(user, key, value)

    def account_state(self, user_id: str) -> tuple[str, int, bool] | None:
        with self._guard:
            user = self._users.get(user_id)
            if user is None:
                return None
            return user.status, user.token_version, user.email_verified

    # -- email verification / password reset -------------------------------
    def create_email_verification(self, raw_token: str, user_id: str, email: str, ttl: timedelta) -> str:
        th = token_hash(raw_token)
        with self._guard:
            self._verifications[th] = (user_id, datetime.now(timezone.utc) + ttl)
        return th

    def consume_email_verification(self, raw_token: str) -> str | None:
        th = token_hash(raw_token)
        with self._guard:
            entry = self._verifications.get(th)
            if entry is None or entry[1] <= datetime.now(timezone.utc):
                return None
            del self._verifications[th]
            return entry[0]

    def create_password_reset(self, raw_token: str, user_id: str, ttl: timedelta, ip_hash: str | None) -> str:
        th = token_hash(raw_token)
        with self._guard:
            self._resets[th] = (user_id, datetime.now(timezone.utc) + ttl)
        return th

    def consume_password_reset(self, raw_token: str) -> str | None:
        th = token_hash(raw_token)
        with self._guard:
            entry = self._resets.get(th)
            if entry is None or entry[1] <= datetime.now(timezone.utc):
                return None
            del self._resets[th]
            return entry[0]

    # -- refresh tokens ----------------------------------------------------
    def create_refresh_token(self, raw: str, user_id: str, family_id: str, ttl: timedelta) -> None:
        with self._guard:
            self._refresh[token_hash(raw)] = {
                "user_id": user_id,
                "family_id": family_id,
                "expires_at": datetime.now(timezone.utc) + ttl,
                "rotated_at": None,
                "revoked_at": None,
            }

    def rotate_refresh_token(self, raw: str, ttl: timedelta) -> tuple[str, str, str] | str | None:
        th = token_hash(raw)
        with self._guard:
            entry = self._refresh.get(th)
            if entry is None:
                return None
            now = datetime.now(timezone.utc)
            if entry["revoked_at"] is not None or entry["expires_at"] <= now:
                return None
            if entry["rotated_at"] is not None:
                return "reuse"
            entry["rotated_at"] = now
            new_raw = new_opaque_token()
            self._refresh[token_hash(new_raw)] = {
                "user_id": entry["user_id"],
                "family_id": entry["family_id"],
                "expires_at": now + ttl,
                "rotated_at": None,
                "revoked_at": None,
            }
            return entry["user_id"], entry["family_id"], new_raw

    def revoke_family(self, family_id: str) -> int:
        with self._guard:
            count = 0
            for entry in self._refresh.values():
                if entry["family_id"] == family_id and entry["revoked_at"] is None:
                    entry["revoked_at"] = datetime.now(timezone.utc)
                    count += 1
            return count

    def revoke_all_for_user(self, user_id: str) -> int:
        with self._guard:
            count = 0
            for entry in self._refresh.values():
                if entry["user_id"] == user_id and entry["revoked_at"] is None:
                    entry["revoked_at"] = datetime.now(timezone.utc)
                    count += 1
            return count

    def family_of(self, th: str) -> str | None:
        with self._guard:
            entry = self._refresh.get(th)
            return entry["family_id"] if entry else None

    # -- rate limiting -----------------------------------------------------
    def record_attempt(self, scope: str, identifier_hash: str, ok: bool) -> None:
        with self._guard:
            self._attempts.append((scope, identifier_hash, time.time(), ok))

    def count_recent_failures(self, scope: str, identifier_hash: str, window_seconds: int) -> int:
        cutoff = time.time() - window_seconds
        with self._guard:
            return sum(
                1
                for s, i, at, ok in self._attempts
                if s == scope and i == identifier_hash and not ok and at >= cutoff
            )

    # -- outbox ------------------------------------------------------------
    def enqueue_email(self, msg: OutboxMessage) -> bool:
        with self._guard:
            if any(m.idempotency_key == msg.idempotency_key for m in self._outbox.values()):
                return False
            self._outbox[msg.id] = msg
            return True

    def claim_outbox(self, batch: int) -> list[OutboxMessage]:
        now = datetime.now(timezone.utc)
        claimed: list[OutboxMessage] = []
        with self._guard:
            for msg in self._outbox.values():
                if len(claimed) >= batch:
                    break
                due = (msg.status == "pending") or (
                    msg.status == "sending" and msg.lease_expires_at is not None and msg.lease_expires_at < now
                )
                if due:
                    msg.status = "sending"
                    msg.attempts += 1
                    msg.lease_expires_at = now + timedelta(seconds=OUTBOX_LEASE_SECONDS)
                    claimed.append(msg)
        return claimed

    def mark_outbox_sent(self, msg_id: str, provider_id: str | None, delivery_status: str) -> None:
        with self._guard:
            msg = self._outbox.get(msg_id)
            if msg:
                msg.status = "sent"
                msg.provider_id = provider_id
                msg.delivery_status = delivery_status
                # The raw token is no longer needed once the message is sent.
                msg.token_raw = None

    def mark_outbox_failed(self, msg_id: str, error: str, next_attempt_at: datetime, terminal: bool) -> None:
        with self._guard:
            msg = self._outbox.get(msg_id)
            if msg:
                msg.status = "dead" if terminal else "pending"
                msg.last_error = error[:500]
                msg.lease_expires_at = None
                msg.next_attempt_at = next_attempt_at
                if terminal:
                    msg.token_raw = None

    def set_delivery_status(self, provider_id: str, delivery_status: str) -> int:
        with self._guard:
            count = 0
            for msg in self._outbox.values():
                if msg.provider_id == provider_id:
                    msg.delivery_status = delivery_status
                    count += 1
            return count

    # -- audit -------------------------------------------------------------
    def record_event(self, action: str, *, actor_id: str | None = None, target: str | None = None,
                     ip_hash: str | None = None, detail: str | None = None) -> None:
        with self._guard:
            self._events.append(
                {"id": secrets.token_hex(8), "action": action, "actor_id": actor_id,
                 "target": target, "ip_hash": ip_hash, "detail": detail}
            )

    def prune(self, table: str, limit: int | None = None) -> int:
        # The in-memory store is bounded by the process lifetime; nothing to do.
        return 0


# --------------------------------------------------------------------------- #
# PostgreSQL implementation
# --------------------------------------------------------------------------- #


class PostgresAuthStore:
    backend = "postgres"
    shared = True

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        from .pool import get_pool

        self._pool = get_pool(dsn)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(SCHEMA)
            for statement in MIGRATIONS:
                cur.execute(statement)

    # -- users -------------------------------------------------------------
    def create_user(self, user: UserRecord) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, email, email_verified_at, role, display_name,
                                   password_hash, status, is_agent, token_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (user.id, normalize_email(user.email), user.email_verified_at, user.role,
                 user.display_name, user.password_hash, user.status, user.is_agent, user.token_version),
            )

    @staticmethod
    def _row_to_user(row: Any) -> UserRecord:
        return UserRecord(
            id=row[0], email=row[1], email_verified_at=row[2], role=row[3],
            display_name=row[4], password_hash=row[5], status=row[6],
            is_agent=row[7], token_version=row[8], last_login_at=row[9],
        )

    _USER_COLS = (
        "id, email, email_verified_at, role, display_name, password_hash, "
        "status, is_agent, token_version, last_login_at"
    )

    def get_user_by_id(self, user_id: str) -> UserRecord | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._USER_COLS} FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_email(self, email: str) -> UserRecord | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._USER_COLS} FROM users WHERE email = %s",
                (normalize_email(email),),
            )
            row = cur.fetchone()
        return self._row_to_user(row) if row else None

    def update_user(self, user_id: str, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{name} = %s" for name in fields)
        params = list(fields.values()) + [user_id]
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE users SET {columns}, updated_at = now() WHERE id = %s", params)

    def account_state(self, user_id: str) -> tuple[str, int, bool] | None:
        """Authoritative (status, token_version, email_verified) for a user.

        Raises :class:`AccountStateError` on a database problem so a sensitive
        caller fails closed rather than assuming the account is healthy.
        """
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT status, token_version, (email_verified_at IS NOT NULL) "
                    "FROM users WHERE id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
        except Exception as exc:
            raise AccountStateError(type(exc).__name__) from exc
        return (row[0], int(row[1]), bool(row[2])) if row else None

    # -- email verification / password reset -------------------------------
    def create_email_verification(self, raw_token: str, user_id: str, email: str, ttl: timedelta) -> str:
        th = token_hash(raw_token)
        expires = datetime.now(timezone.utc) + ttl
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO email_verifications (token_hash, user_id, email, expires_at) "
                "VALUES (%s, %s, %s, %s)",
                (th, user_id, normalize_email(email), expires),
            )
        return th

    def consume_email_verification(self, raw_token: str) -> str | None:
        th = token_hash(raw_token)
        # Single statement: consume only if unconsumed and unexpired. RETURNING
        # makes it atomic, so two concurrent uses cannot both succeed.
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE email_verifications
                SET consumed_at = now()
                WHERE token_hash = %s AND consumed_at IS NULL AND expires_at > now()
                RETURNING user_id
                """,
                (th,),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def create_password_reset(self, raw_token: str, user_id: str, ttl: timedelta, ip_hash: str | None) -> str:
        th = token_hash(raw_token)
        expires = datetime.now(timezone.utc) + ttl
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO password_reset_tokens (token_hash, user_id, expires_at, requested_ip_hash) "
                "VALUES (%s, %s, %s, %s)",
                (th, user_id, expires, ip_hash),
            )
        return th

    def consume_password_reset(self, raw_token: str) -> str | None:
        th = token_hash(raw_token)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE password_reset_tokens
                SET consumed_at = now()
                WHERE token_hash = %s AND consumed_at IS NULL AND expires_at > now()
                RETURNING user_id
                """,
                (th,),
            )
            row = cur.fetchone()
        return row[0] if row else None

    # -- refresh tokens ----------------------------------------------------
    def create_refresh_token(self, raw: str, user_id: str, family_id: str, ttl: timedelta) -> None:
        expires = datetime.now(timezone.utc) + ttl
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO refresh_tokens (token_hash, user_id, family_id, expires_at) "
                "VALUES (%s, %s, %s, %s)",
                (token_hash(raw), user_id, family_id, expires),
            )

    def rotate_refresh_token(self, raw: str, ttl: timedelta) -> tuple[str, str, str] | str | None:
        """Rotate exactly once, or report reuse.

        Returns ``(user_id, family_id, new_raw_token)`` on success, the string
        ``"reuse"`` when a previously-rotated token is presented again, or ``None``
        when the token is unknown/expired/revoked. The single conditional UPDATE is
        what makes two concurrent uses resolve to exactly one winner.
        """
        th = token_hash(raw)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_ROTATE_SQL, (th,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT rotated_at, revoked_at, expires_at FROM refresh_tokens WHERE token_hash = %s",
                    (th,),
                )
                existing = cur.fetchone()
                if existing and existing[0] is not None:
                    return "reuse"
                return None
            user_id, family_id = row[0], row[1]
            new_raw = new_opaque_token()
            expires = datetime.now(timezone.utc) + ttl
            cur.execute(
                "INSERT INTO refresh_tokens (token_hash, user_id, family_id, expires_at) "
                "VALUES (%s, %s, %s, %s)",
                (token_hash(new_raw), user_id, family_id, expires),
            )
        return user_id, family_id, new_raw

    def revoke_family(self, family_id: str) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE refresh_tokens SET revoked_at = now() "
                "WHERE family_id = %s AND revoked_at IS NULL",
                (family_id,),
            )
            return cur.rowcount

    def revoke_all_for_user(self, user_id: str) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE refresh_tokens SET revoked_at = now() "
                "WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            )
            return cur.rowcount

    def family_of(self, th: str) -> str | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT family_id FROM refresh_tokens WHERE token_hash = %s", (th,))
            row = cur.fetchone()
        return row[0] if row else None

    # -- rate limiting -----------------------------------------------------
    def record_attempt(self, scope: str, identifier_hash: str, ok: bool) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO login_attempts (scope, identifier_hash, ok) VALUES (%s, %s, %s)",
                (scope, identifier_hash, ok),
            )

    def count_recent_failures(self, scope: str, identifier_hash: str, window_seconds: int) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM login_attempts "
                "WHERE scope = %s AND identifier_hash = %s AND ok = false "
                "AND at > now() - make_interval(secs => %s)",
                (scope, identifier_hash, window_seconds),
            )
            return int(cur.fetchone()[0])

    # -- outbox ------------------------------------------------------------
    def enqueue_email(self, msg: OutboxMessage) -> bool:
        """Insert a pending message, or False if the idempotency key exists.

        The unique index on ``idempotency_key`` is what makes a duplicate enqueue a
        no-op rather than a second send, even under concurrent requests.
        """
        from psycopg.errors import UniqueViolation

        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO email_outbox
                        (id, kind, user_id, to_email, token_hash, token_raw, idempotency_key, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                    """,
                    (msg.id, msg.kind, msg.user_id, normalize_email(msg.to_email),
                     msg.token_hash, msg.token_raw, msg.idempotency_key),
                )
            return True
        except UniqueViolation:
            return False

    def claim_outbox(self, batch: int) -> list[OutboxMessage]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_CLAIM_OUTBOX_SQL, (batch, OUTBOX_LEASE_SECONDS))
            rows = cur.fetchall()
        return [
            OutboxMessage(
                id=r[0], kind=r[1], user_id=r[2], to_email=r[3],
                token_hash=None, idempotency_key=r[5], attempts=r[6],
                status="sending", token_raw=r[4],
            )
            for r in rows
        ]

    def mark_outbox_sent(self, msg_id: str, provider_id: str | None, delivery_status: str) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE email_outbox
                SET status = 'sent', provider_id = %s, delivery_status = %s,
                    completed_at = now(), lease_expires_at = NULL, last_error = NULL,
                    token_raw = NULL
                WHERE id = %s
                """,
                (provider_id, delivery_status, msg_id),
            )

    def mark_outbox_failed(self, msg_id: str, error: str, next_attempt_at: datetime, terminal: bool) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE email_outbox
                SET status = %s, last_error = %s, next_attempt_at = %s,
                    lease_expires_at = NULL,
                    delivery_status = CASE WHEN %s THEN 'permanently_failed' ELSE delivery_status END,
                    completed_at = CASE WHEN %s THEN now() ELSE NULL END,
                    -- A terminal message will never be rendered again, so drop the
                    -- raw token: it must not sit in the outbox after it can no
                    -- longer be used.
                    token_raw = CASE WHEN %s THEN NULL ELSE token_raw END
                WHERE id = %s
                """,
                ("dead" if terminal else "pending", error[:500], next_attempt_at,
                 terminal, terminal, terminal, msg_id),
            )

    def set_delivery_status(self, provider_id: str, delivery_status: str) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE email_outbox SET delivery_status = %s WHERE provider_id = %s",
                (delivery_status, provider_id),
            )
            return cur.rowcount

    # -- audit -------------------------------------------------------------
    def record_event(self, action: str, *, actor_id: str | None = None, target: str | None = None,
                     ip_hash: str | None = None, detail: str | None = None) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auth_events (id, actor_id, action, target, ip_hash, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (secrets.token_hex(8), actor_id, action, target, ip_hash, detail),
            )

    # -- cleanup -----------------------------------------------------------
    def prune(self, table: str, limit: int | None = None) -> int:
        """Bounded cleanup of one table. Returns rows removed."""
        batch = CLEANUP_BATCH if limit is None else limit
        sql = _PRUNE_SQL.get(table)
        if sql is None:
            raise ValueError(f"no prune statement for {table!r}")
        params: tuple[Any, ...]
        if table == "email_outbox":
            params = (OUTBOX_RETENTION_DAYS, batch)
        elif table == "login_attempts":
            params = (7, batch)
        else:
            params = (batch,)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


class UnavailableAuthStore:
    """Stands in when a shared store is configured but could not be opened.

    Every account-state read raises :class:`AccountStateError`, so a sensitive
    operation fails closed (503) rather than assuming an account is fine. Mutating
    calls raise :class:`AuthStoreError`. Nothing here ever succeeds.
    """

    backend = "unavailable"
    shared = True

    def __init__(self, error: Exception) -> None:
        self.error = error

    def _fail(self):
        raise AuthStoreError(
            f"the auth store is unavailable ({type(self.error).__name__})"
        )

    def account_state(self, user_id: str):
        raise AccountStateError(type(self.error).__name__)

    def get_user_by_id(self, user_id: str):
        return None

    def get_user_by_email(self, email: str):
        return None

    def __getattr__(self, name: str):
        # Any other operation (create/update/rotate/enqueue/prune) fails closed.
        def _refuse(*args: Any, **kwargs: Any):
            self._fail()

        return _refuse


def _select_auth_store():
    dsn = settings.database_url
    if not dsn:
        log.warning(
            "No DATABASE_URL: the auth store is process-local. Customer accounts and "
            "sessions are correct only for a single-instance deployment."
        )
        return InMemoryAuthStore()
    try:
        return PostgresAuthStore(dsn)
    except Exception as exc:
        # Do not crash the whole app at import: the settlement and operator paths
        # must keep working. Customer authentication, however, refuses every
        # sensitive operation until the database is reachable.
        log.error(
            "DATABASE_URL is set but the auth store could not be opened (%s). "
            "Customer authentication will fail closed.",
            type(exc).__name__,
        )
        return UnavailableAuthStore(exc)


auth_store = _select_auth_store()
