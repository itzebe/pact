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


@dataclass(frozen=True)
class PayPalSettings:
    client_id: str = field(default_factory=lambda: os.getenv("PAYPAL_CLIENT_ID", ""))
    client_secret: str = field(default_factory=lambda: os.getenv("PAYPAL_CLIENT_SECRET", ""))
    environment: str = field(default_factory=lambda: os.getenv("PAYPAL_ENVIRONMENT", "sandbox").lower())

    @property
    def base_url(self) -> str:
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
class Settings:
    paypal: PayPalSettings = field(default_factory=PayPalSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    currency: str = field(default_factory=lambda: os.getenv("PACT_CURRENCY", "USD"))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("PACT_DATA_DIR", str(ROOT / "data"))))
    # Postgres is used when set. Serverless platforms have no durable disk, so a
    # connection string is the only way the ledger survives there.
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
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
        if self.auth.configured and self.auth.algorithm not in ALLOWED_AUTH_ALGORITHMS:
            problems.append(
                f"PACT_AUTH_ALGORITHM {self.auth.algorithm!r} is not one of "
                f"{sorted(ALLOWED_AUTH_ALGORITHMS)}; refusing to verify tokens with it"
            )
        return problems


settings = Settings()
