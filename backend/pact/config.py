"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _optional_int(name: str) -> int | None:
    """An int from the environment, or ``None`` when unset/blank/``none``.

    ``None`` is a meaningful value here (it disables psycopg3's automatic statement
    preparation), so an unset variable must not collapse to ``0`` — with psycopg3
    ``prepare_threshold=0`` means "prepare on the first execution", the opposite of
    what is wanted.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "" or raw.strip().lower() == "none":
        return None
    return int(raw)


def _env_int(name: str, default: int) -> int:
    """An int from the environment, or ``default`` when unset/blank.

    A set-but-unparseable value is a typo the operator must see, so it raises at
    import rather than falling back silently to the default (which would look
    operational while being ignored).
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    """A float from the environment, or ``default`` when unset/blank. See ``_env_int``."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


# PayPal's two API hosts. The sandbox host is the only one this project should ever
# talk to during sandbox work; the live host moves real money.
PAYPAL_SANDBOX_HOST = "api-m.sandbox.paypal.com"
PAYPAL_LIVE_HOST = "api-m.paypal.com"

# The only ``PAYPAL_ENVIRONMENT`` values this client knows how to resolve to a host.
# ``is_live`` treats any other value as live (fail closed), and ``validate_runtime``
# refuses it at startup: without that, a typo such as ``production`` would resolve to
# the sandbox host while being *reported* as a safe sandbox deployment.
PAYPAL_ENVIRONMENTS = ("sandbox", "live")


def _is_local_or_sandbox_host(url: str) -> bool:
    """Whether an API base is the sandbox host or a loopback/private test host.

    Used by the sandbox-only guard. A test double or an on-prem proxy is not
    production money, so it is allowed; anything else — the live PayPal host, or a
    host someone forgot they pointed at — is treated as live and refused when the
    guard is on.
    """
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in {PAYPAL_SANDBOX_HOST, "www.sandbox.paypal.com", "sandbox.paypal.com"}:
        return True
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return True
    # RFC1918 / link-local / CGNAT: a private network host is a test or an internal
    # mock, never PayPal's production endpoint.
    if host.startswith(("10.", "192.168.", "169.254.")):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second <= 31
    return False


@dataclass(frozen=True)
class PayPalSettings:
    client_id: str = field(default_factory=lambda: os.getenv("PAYPAL_CLIENT_ID", ""))
    client_secret: str = field(default_factory=lambda: os.getenv("PAYPAL_CLIENT_SECRET", ""))
    environment: str = field(default_factory=lambda: os.getenv("PAYPAL_ENVIRONMENT", "sandbox").lower())
    # Webhook id of the endpoint registered in the PayPal developer dashboard. It is
    # what PayPal signs against, so signature verification cannot proceed without it.
    # Not a secret: it identifies the endpoint, it does not authorise anything.
    webhook_id: str = field(default_factory=lambda: os.getenv("PAYPAL_WEBHOOK_ID", "").strip())
    # Optional explicit API base. Used to point at a local mock or a proxy in tests.
    # It is *validated* rather than trusted: ``sandbox_only`` refuses a live host.
    base_url_override: str = field(default_factory=lambda: os.getenv("PAYPAL_BASE_URL", "").rstrip("/"))
    # When set, a live (production) PayPal host is refused outright. This is the
    # switch that makes "sandbox only" a property of the process rather than a
    # convention, so an accidental production credential cannot move real money.
    sandbox_only: bool = field(default_factory=lambda: _bool("PACT_PAYPAL_SANDBOX_ONLY", False))
    # A PayPal *Sandbox* personal (buyer) account a guest can use to complete the
    # order-approval step. PayPal requires the buyer to approve an order before it can
    # be authorized, and a public judge has no sandbox account of their own, so
    # without one the flow stops at "buyer approval required". This is a test account
    # on the sandbox host: it moves no real money. It is surfaced *only* to a guest
    # session on a sandbox deployment (see ``api.auth_routes``), never to an
    # unauthenticated caller and never when the host is live, so it cannot leak from a
    # deployment that moves real money. Leave unset to hide the hint.
    sandbox_buyer_email: str = field(
        default_factory=lambda: os.getenv("PAYPAL_SANDBOX_BUYER_EMAIL", "").strip()
    )
    sandbox_buyer_password: str = field(
        default_factory=lambda: os.getenv("PAYPAL_SANDBOX_BUYER_PASSWORD", "")
    )

    @property
    def is_live(self) -> bool:
        """Whether this configuration resolves to PayPal's production host.

        An explicit ``PAYPAL_BASE_URL`` that is not the sandbox host counts as live
        unless it is a loopback/private host used by tests, so an override cannot be
        used to slip past the sandbox-only guard.

        An unrecognised ``environment`` is treated as live rather than sandbox: the
        client only resolves a host for ``sandbox``/``live``, so any other value is a
        misconfiguration, and reporting it as *not* live would present an ambiguous
        deployment as a safe one. ``validate_runtime`` refuses such a value at
        startup, so this is the fail-closed default, not the expected path.
        """
        if self.base_url_override:
            return not _is_local_or_sandbox_host(self.base_url_override)
        return self.environment != "sandbox"

    @property
    def base_url(self) -> str:
        if self.base_url_override:
            return self.base_url_override
        if self.environment == "live":
            return "https://api-m.paypal.com"
        return "https://api-m.sandbox.paypal.com"

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


@dataclass(frozen=True)
class AuthSettings:
    """Bearer-token signing configuration.

    ``configured`` is False when no secret is present. The API treats that as
    "refuse every request" rather than "allow anonymous", so a deployment that
    forgot to set a secret fails closed instead of serving money routes openly.
    """

    secret: str = field(default_factory=lambda: os.getenv("PACT_AUTH_SECRET", ""))
    algorithm: str = field(default_factory=lambda: os.getenv("PACT_AUTH_ALGORITHM", "HS256"))
    #: Customer self-service registration. Off by default: it stays disabled
    #: until an email provider and a verified sending domain are configured, so
    #: the operator-authenticated experience is the only path until then.
    self_service_enabled: bool = field(
        default_factory=lambda: _bool("PACT_SELF_SERVICE_AUTH", False)
    )
    #: Access-token lifetime, in minutes. Short so a revocation is bounded; the
    #: refresh family carries the long-lived session.
    access_token_ttl_minutes: int = field(
        default_factory=lambda: int(os.getenv("PACT_ACCESS_TOKEN_TTL_MINUTES", "15"))
    )
    #: Refresh-token family lifetime, in days.
    refresh_token_ttl_days: int = field(
        default_factory=lambda: int(os.getenv("PACT_REFRESH_TOKEN_TTL_DAYS", "30"))
    )
    #: Guest (judge) sessions. When enabled, a first-time visitor is given a
    #: server-managed guest workspace with no credentials to supply, so the
    #: deployed product can be evaluated end to end. Off by default: a deployment
    #: that does not want anonymous traffic must not silently start serving it.
    guests_enabled: bool = field(
        default_factory=lambda: _bool("PACT_GUEST_SESSIONS", False)
    )
    #: Guest session lifetime, in hours. Bounds how long an unattended workspace
    #: lives; the cookie carries the same expiry.
    guest_session_ttl_hours: int = field(
        default_factory=lambda: int(os.getenv("PACT_GUEST_SESSION_TTL_HOURS", "12"))
    )
    #: How many jobs one guest workspace may create. A ceiling on how much of the
    #: shared (real, sandbox) payment rail a single visitor can exercise.
    guest_max_jobs: int = field(
        default_factory=lambda: int(os.getenv("PACT_GUEST_MAX_JOBS", "8"))
    )
    #: New guest workspaces allowed per client IP within the window. Bounds
    #: workspace creation (and therefore database rows and sandbox orders) from a
    #: single source without hindering an ordinary evaluation.
    guest_new_per_ip: int = field(
        default_factory=lambda: int(os.getenv("PACT_GUEST_NEW_PER_IP", "30"))
    )
    guest_new_window_seconds: int = field(
        default_factory=lambda: int(os.getenv("PACT_GUEST_NEW_WINDOW_SECONDS", "3600"))
    )

    @property
    def configured(self) -> bool:
        return bool(self.secret)


# Signing algorithms Pact will accept. Restricted to the HMAC family: a symmetric
# secret is the only key material this deployment holds, and an allowlist stops a
# misconfigured ``PACT_AUTH_ALGORITHM`` (notably ``none``, or an asymmetric alg the
# server cannot verify) from ever being consulted. PyJWT already refuses ``none``
# with a key, but failing at startup is clearer than failing per request.
ALLOWED_AUTH_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})


@dataclass(frozen=True)
class LLMSettings:
    """Any OpenAI-compatible endpoint works; no vendor is hard-coded."""

    api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", "") or os.getenv("OPENAI_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"))
    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    temperature: float = field(default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.2")))
    timeout: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT", "90")))

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class RateLimitSettings:
    """Ceilings on how often the money-bearing routes may be called.

    These are per (endpoint, identity) fixed windows. They exist so one caller
    cannot drive unbounded PayPal calls or exhaust the provider's own rate limit
    for everyone. The defaults are deliberately generous for a human evaluator and
    only bite a runaway client or a script. Setting ``PACT_RATE_LIMIT_ENABLED=false``
    disables the guard, which is refused in production (see ``validate_runtime``).
    """

    enabled: bool = field(default_factory=lambda: _bool("PACT_RATE_LIMIT_ENABLED", True))
    #: Run drives authorize + deliver + verify + capture: the heaviest, most
    #: money-bearing call, and the one that reaches PayPal the most times.
    run_limit: int = field(default_factory=lambda: int(os.getenv("PACT_RATELIMIT_RUN_LIMIT", "10")))
    run_window_seconds: int = field(
        default_factory=lambda: int(os.getenv("PACT_RATELIMIT_RUN_WINDOW_SECONDS", "300"))
    )
    approve_limit: int = field(default_factory=lambda: int(os.getenv("PACT_RATELIMIT_APPROVE_LIMIT", "30")))
    approve_window_seconds: int = field(
        default_factory=lambda: int(os.getenv("PACT_RATELIMIT_APPROVE_WINDOW_SECONDS", "300"))
    )
    cancel_limit: int = field(default_factory=lambda: int(os.getenv("PACT_RATELIMIT_CANCEL_LIMIT", "30")))
    cancel_window_seconds: int = field(
        default_factory=lambda: int(os.getenv("PACT_RATELIMIT_CANCEL_WINDOW_SECONDS", "300"))
    )
    reconcile_limit: int = field(default_factory=lambda: int(os.getenv("PACT_RATELIMIT_RECONCILE_LIMIT", "60")))
    reconcile_window_seconds: int = field(
        default_factory=lambda: int(os.getenv("PACT_RATELIMIT_RECONCILE_WINDOW_SECONDS", "300"))
    )
    refund_limit: int = field(default_factory=lambda: int(os.getenv("PACT_RATELIMIT_REFUND_LIMIT", "20")))
    refund_window_seconds: int = field(
        default_factory=lambda: int(os.getenv("PACT_RATELIMIT_REFUND_WINDOW_SECONDS", "300"))
    )
    job_create_limit: int = field(default_factory=lambda: int(os.getenv("PACT_RATELIMIT_JOB_CREATE_LIMIT", "30")))
    job_create_window_seconds: int = field(
        default_factory=lambda: int(os.getenv("PACT_RATELIMIT_JOB_CREATE_WINDOW_SECONDS", "300"))
    )
    read_limit: int = field(default_factory=lambda: int(os.getenv("PACT_RATELIMIT_READ_LIMIT", "120")))
    read_window_seconds: int = field(
        default_factory=lambda: int(os.getenv("PACT_RATELIMIT_READ_WINDOW_SECONDS", "60"))
    )


@dataclass(frozen=True)
class Settings:
    paypal: PayPalSettings = field(default_factory=PayPalSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    rate_limit: RateLimitSettings = field(default_factory=RateLimitSettings)
    currency: str = field(default_factory=lambda: os.getenv("PACT_CURRENCY", "USD"))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("PACT_DATA_DIR", str(ROOT / "data"))))
    # Postgres is used when set. Serverless platforms have no durable disk, so a
    # connection string is the only way the ledger survives there.
    #
    # DATABASE_URL is the *pooled* endpoint (e.g. Neon's "-pooler" host, which is
    # PgBouncer in transaction mode). It is correct for ordinary request/response
    # queries, where every operation is short and self-contained.
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    # DATABASE_URL_UNPOOLED is the *direct* endpoint (the same host without the
    # "-pooler" suffix). Session-scoped work — the settlement advisory lock — must
    # use it, because a transaction-mode pooler hands each client a different
    # server connection per transaction, so a session lock is released between
    # statements and silently stops protecting the settlement. Never the pooled
    # DSN for that work (see core.pool.DatabasePool.direct).
    database_url_unpooled: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL_UNPOOLED", "")
    )
    # Mock PayPal is OFF by default. A mock settlement moves no money, so a
    # deployment that silently used it would report payments that never happened.
    # Development turns it on explicitly; production leaves it off.
    allow_mock_paypal: bool = field(default_factory=lambda: _bool("PACT_ALLOW_MOCK_PAYPAL", False))
    # Refuse to start rather than settle against the in-memory double. Set this on
    # any deployment where a settlement is meant to be real: a mock "settlement"
    # moves no money, so reporting one as settled would be a lie about payment.
    require_real_paypal: bool = field(default_factory=lambda: _bool("PACT_REQUIRE_REAL_PAYPAL", False))
    # The environment this process believes it is. Any value other than
    # "development"/"test" is treated as production, where mock settlement is
    # refused outright (see paypal.build_escrow_client).
    environment: str = field(default_factory=lambda: os.getenv("PACT_ENV", "production").strip().lower())
    sellers: str = field(default_factory=lambda: os.getenv("PACT_SELLERS", "seller_atlas,seller_nova,seller_quill"))
    # Public base URL of this deployment, used to build the PayPal return/cancel
    # URLs. PayPal's redirect flow sends the buyer back to the merchant only if the
    # order carries a return_url, so without this the buyer has nowhere to return to
    # and the approval appears to fail.
    public_url: str = field(default_factory=lambda: os.getenv("PACT_PUBLIC_URL", "").rstrip("/"))
    # Hosts the API is allowed to be reached on. Used to reject a forged Host /
    # X-Forwarded-Host header before it can be turned into a PayPal return_url.
    allowed_hosts: list[str] = field(
        default_factory=lambda: [
            h.strip().lower()
            for h in os.getenv("PACT_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
            if h.strip()
        ]
    )
    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip() for o in os.getenv("PACT_CORS_ORIGINS", "http://localhost:5173,http://localhost:4173").split(",") if o.strip()
        ]
    )
    # Bounded execution. A single run must finish inside the platform's function
    # timeout, so the engine enforces its own, shorter deadline and marks the job
    # failed rather than being killed mid-flight by the platform.
    run_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("PACT_RUN_TIMEOUT_SECONDS", "50")))
    # How long a run may hold a job before another worker may reclaim it. Must
    # exceed run_timeout_seconds so a live run is never reclaimed underneath.
    lease_seconds: int = field(default_factory=lambda: int(os.getenv("PACT_LEASE_SECONDS", "120")))
    # Ceiling on runs executing at once in this process. Beyond it, requests are
    # rejected with 429 instead of queueing without bound.
    max_concurrent_runs: int = field(default_factory=lambda: int(os.getenv("PACT_MAX_CONCURRENT_RUNS", "4")))
    # How long a caller will wait for a slot before being told to retry.
    run_queue_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("PACT_RUN_QUEUE_TIMEOUT_SECONDS", "2.0"))
    )
    # How long a claimed buyer approval stays claimed before another worker may
    # retry it. The claim exists only to stop two approvals racing; it must not
    # outlive the attempt, or a worker that dies between claiming and returning
    # would block the buyer from ever approving the order. The window is
    # comfortably longer than a PayPal round-trip (see the PayPal client timeout).
    approval_claim_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("PACT_APPROVAL_CLAIM_TTL_SECONDS", "120"))
    )

    # -- email (transactional) --------------------------------------------
    # None of this is configured by default. Until an owner sets a provider and a
    # verified sending domain, the outbox records messages as queued and the
    # dispatcher reports that no provider is configured — it never claims delivery.
    email_provider: str = field(default_factory=lambda: os.getenv("PACT_EMAIL_PROVIDER", "none"))
    email_api_key: str = field(default_factory=lambda: os.getenv("PACT_EMAIL_API_KEY", ""))
    email_from: str = field(default_factory=lambda: os.getenv("PACT_EMAIL_FROM", ""))
    email_base_url: str = field(default_factory=lambda: os.getenv("PACT_EMAIL_BASE_URL", ""))
    #: When set, a provider webhook must present this shared secret. Used to move
    #: a message from accepted to delivered/bounced; never trusted unverified.
    email_webhook_secret: str = field(default_factory=lambda: os.getenv("PACT_EMAIL_WEBHOOK_SECRET", ""))

    # -- PostgreSQL pool ---------------------------------------------------
    # A *per-instance* cap. The worst case across a serverless fleet is
    # ``active function instances x max_size``; budget it against the database
    # plan (see docs/AUTH.md). A pool is not a global connection limit.
    # These values are passed to ``core.pool.get_pool``; a malformed one (a value
    # the driver would reject) is reported by ``validate_runtime`` and refuses a
    # production boot rather than failing later on the first query.
    pg_pool_max_size: int = field(default_factory=lambda: _env_int("PACT_PG_POOL_MAX_SIZE", 5))
    pg_pool_min_size: int = field(default_factory=lambda: _env_int("PACT_PG_POOL_MIN_SIZE", 0))
    pg_pool_timeout_seconds: float = field(
        default_factory=lambda: _env_float("PACT_PG_POOL_TIMEOUT_SECONDS", 5.0)
    )
    pg_pool_max_idle_seconds: float = field(
        default_factory=lambda: _env_float("PACT_PG_POOL_MAX_IDLE_SECONDS", 300.0)
    )
    pg_statement_timeout_ms: int = field(
        default_factory=lambda: _env_int("PACT_PG_STATEMENT_TIMEOUT_MS", 10_000)
    )
    # psycopg3 prepares a statement server-side after this many executions. Behind
    # a transaction-mode pooler that is unsafe: the pooler hands a *server*
    # connection to a different client between transactions, and the next client's
    # fresh session re-issues ``PREPARE _pg3_0`` on a server connection that already
    # has it, raising ``DuplicatePreparedStatement`` and failing the request.
    # ``None`` (the default, via ``PACT_PG_PREPARE_THRESHOLD=none``) disables
    # automatic preparation, which is the documented safe setting for pgbouncer in
    # transaction mode. A positive integer re-enables it (psycopg's own default is
    # 5); only do that for an endpoint you know is a direct session, never a pooler.
    pg_prepare_threshold: int | None = field(
        default_factory=lambda: _optional_int("PACT_PG_PREPARE_THRESHOLD")
    )

    @property
    def settlement_database_url(self) -> str:
        """The DSN session-scoped work must use: a direct endpoint, never pooled.

        Prefers an explicit ``DATABASE_URL_UNPOOLED``; when only the pooled
        ``DATABASE_URL`` is set it derives the direct endpoint from a Neon-style
        ``-pooler`` host, which is what Neon documents (the direct host is the pooled
        host with the ``-pooler`` label removed). ``pool.direct()`` verifies the
        result with a live probe before holding a session lock, so a wrong guess is
        refused rather than used. Never returns the pooled DSN when that DSN is
        actually pooled — see ``pool.direct()``, which fails loudly instead.
        """
        explicit = self.database_url_unpooled.strip()
        if explicit:
            return explicit
        pooled = self.database_url.strip()
        if not pooled:
            return ""
        from .core.pool import derive_direct_dsn

        return derive_direct_dsn(pooled) or pooled

    @property
    def is_production(self) -> bool:
        return self.environment not in {"development", "dev", "test", "local"}

    def validate_runtime(self) -> list[str]:
        """Return a list of configuration problems that make this deployment unsafe.

        Empty means safe. These are *hard* problems: each one, left unfixed, would
        let the deployment move or report money it should not.
        """
        problems: list[str] = []

        if self.public_url and not self.public_url.startswith(("http://", "https://")):
            problems.append(
                f"PACT_PUBLIC_URL must be an absolute http(s) URL, got {self.public_url!r}"
            )
        # A sandbox-only deployment must never be pointed at PayPal's production host.
        # This is checked for every environment, not just production: the whole point
        # is to make an accidental live credential impossible to use, and a developer
        # laptop with live credentials is exactly the accident being prevented.
        if self.paypal.sandbox_only and self.paypal.is_live:
            problems.append(
                "PACT_PAYPAL_SANDBOX_ONLY is set but the PayPal configuration resolves "
                "to a live host; refusing to talk to production PayPal"
            )
        # An unrecognised PAYPAL_ENVIRONMENT is refused rather than guessed at. The
        # client only resolves a host for "sandbox"/"live"; any other value (a typo
        # such as "production", a stray space, an empty string) would otherwise
        # silently talk to the sandbox host while being *reported* as a safe sandbox
        # deployment — the ambiguous configuration presented as a safe one that the
        # sandbox-only design exists to prevent. Checked in every environment so a
        # developer laptop fails the same way. An explicit PAYPAL_BASE_URL makes the
        # environment irrelevant, so it is exempt.
        if not self.paypal.base_url_override and self.paypal.environment not in PAYPAL_ENVIRONMENTS:
            problems.append(
                f"PAYPAL_ENVIRONMENT {self.paypal.environment!r} is not one of "
                f"{list(PAYPAL_ENVIRONMENTS)}; refusing to guess whether this is a "
                "sandbox or a live PayPal deployment"
            )
        # Anonymous guests must never be able to drive a real-money capture. Sandbox
        # money is fine to expose publicly; live money is not, so a deployment that
        # would move real money is refused outright rather than silently gating the
        # guest surface (a gate that could be flipped by a later misconfiguration).
        if self.auth.guests_enabled and self.paypal.is_live:
            problems.append(
                "PACT_GUEST_SESSIONS is enabled but PayPal resolves to a live host; "
                "anonymous guest sessions must never move real money. Point PayPal at "
                "sandbox or disable guest sessions"
            )
        if self.is_production:
            if not self.auth.configured:
                problems.append(
                    "PACT_AUTH_SECRET is not set, so every API request would be refused "
                    "(set it to a long random string)"
                )
            if not self.paypal.configured and self.allow_mock_paypal:
                problems.append(
                    "no PayPal credentials, but PACT_ALLOW_MOCK_PAYPAL is on — this "
                    "deployment would report simulated settlements as real"
                )
            if self.auth.configured and len(self.auth.secret) < 16:
                problems.append(
                    "PACT_AUTH_SECRET is too short; use at least 32 random characters"
                )
            if not self.database_url.strip():
                # A production deployment without a shared database silently loses two
                # guarantees: SSE stream tickets fall back to a process-local registry
                # (so a ticket consumed by one instance is replayable against another),
                # and the job/reputation ledger falls back to non-durable local files
                # that no other instance can see and that vanish on a cold start. The
                # value is never echoed here — only its absence is reported.
                problems.append(
                    "DATABASE_URL is not set, so this production deployment would fall "
                    "back to process-local ticket storage and a non-durable local "
                    "ledger; set it to a PostgreSQL connection string"
                )
        if self.auth.configured and self.auth.algorithm not in ALLOWED_AUTH_ALGORITHMS:
            problems.append(
                f"PACT_AUTH_ALGORITHM {self.auth.algorithm!r} is not one of "
                f"{sorted(ALLOWED_AUTH_ALGORITHMS)}; refusing to verify tokens with it"
            )
        # Pool limits are passed straight to the driver, which rejects an impossible
        # combination by raising at pool construction — i.e. on the first database
        # access, deep in a request. Catch it at startup instead. Checked in every
        # environment because a malformed value is a typo, not a deployment choice.
        if self.pg_pool_max_size < 1:
            problems.append(
                f"PACT_PG_POOL_MAX_SIZE must be at least 1, got {self.pg_pool_max_size}"
            )
        if self.pg_pool_min_size < 0:
            problems.append(
                f"PACT_PG_POOL_MIN_SIZE cannot be negative, got {self.pg_pool_min_size}"
            )
        if self.pg_pool_min_size > self.pg_pool_max_size:
            problems.append(
                "PACT_PG_POOL_MIN_SIZE "
                f"({self.pg_pool_min_size}) cannot exceed PACT_PG_POOL_MAX_SIZE "
                f"({self.pg_pool_max_size})"
            )
        if self.pg_pool_timeout_seconds < 0:
            problems.append(
                "PACT_PG_POOL_TIMEOUT_SECONDS cannot be negative, got "
                f"{self.pg_pool_timeout_seconds}"
            )
        if self.pg_pool_max_idle_seconds < 0:
            problems.append(
                "PACT_PG_POOL_MAX_IDLE_SECONDS cannot be negative, got "
                f"{self.pg_pool_max_idle_seconds}"
            )
        if self.pg_statement_timeout_ms < 0:
            problems.append(
                "PACT_PG_STATEMENT_TIMEOUT_MS cannot be negative, got "
                f"{self.pg_statement_timeout_ms}"
            )
        # The money-route rate limits. A production deployment must not run with the
        # guard disabled: unbounded provider calls from one caller is exactly the
        # condition the guard exists to prevent, so switching it off is refused where
        # it matters rather than silently accepted. A nonsensical limit (zero,
        # negative, or a window that bounds nothing) is refused in every environment.
        if self.is_production and not self.rate_limit.enabled:
            problems.append(
                "PACT_RATE_LIMIT_ENABLED is false, so the money routes would be "
                "unbounded; enable it in production"
            )
        if self.rate_limit.enabled:
            checks = {
                "PACT_RATELIMIT_RUN": (self.rate_limit.run_limit, self.rate_limit.run_window_seconds),
                "PACT_RATELIMIT_APPROVE": (self.rate_limit.approve_limit, self.rate_limit.approve_window_seconds),
                "PACT_RATELIMIT_CANCEL": (self.rate_limit.cancel_limit, self.rate_limit.cancel_window_seconds),
                "PACT_RATELIMIT_RECONCILE": (self.rate_limit.reconcile_limit, self.rate_limit.reconcile_window_seconds),
                "PACT_RATELIMIT_REFUND": (self.rate_limit.refund_limit, self.rate_limit.refund_window_seconds),
                "PACT_RATELIMIT_JOB_CREATE": (self.rate_limit.job_create_limit, self.rate_limit.job_create_window_seconds),
                "PACT_RATELIMIT_READ": (self.rate_limit.read_limit, self.rate_limit.read_window_seconds),
            }
            for name, (limit, window) in checks.items():
                if limit < 1:
                    problems.append(f"{name}_LIMIT must be at least 1, got {limit}")
                if window < 1:
                    problems.append(f"{name}_WINDOW_SECONDS must be at least 1, got {window}")
        return problems


settings = Settings()
