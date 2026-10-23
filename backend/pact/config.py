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
    currency: str = field(default_factory=lambda: os.getenv("PACT_CURRENCY", "USD"))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("PACT_DATA_DIR", str(ROOT / "data"))))
    allow_mock_paypal: bool = field(default_factory=lambda: _bool("PACT_ALLOW_MOCK_PAYPAL", True))
    sellers: str = field(default_factory=lambda: os.getenv("PACT_SELLERS", "seller_atlas,seller_nova,seller_quill"))
    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip() for o in os.getenv("PACT_CORS_ORIGINS", "http://localhost:5173,http://localhost:4173").split(",") if o.strip()
        ]
    )


settings = Settings()
