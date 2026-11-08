"""Deployment safety: which storage and which payment mode are actually active.

Two ways a deployment can lie about itself, both of which would be worse than an
outage because they look healthy:

* reporting durable storage while writing to an ephemeral directory, and
* reporting a "settled" job while no money moved, because settlement ran against
  the in-memory double.

These tests pin the behaviour that prevents both.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


def test_requirements_file_declares_every_runtime_dependency():
    """The image installs from requirements.txt, so it must not omit a dependency.

    A dependency declared in pyproject.toml but missing here is silently absent
    from the Docker image. That is exactly how PyJWT went missing (every
    authenticated request raised ModuleNotFoundError) and how uvicorn was dropped
    (the container could not start at all). Parse both and require parity for the
    core runtime packages.
    """
    import tomllib

    pyproject = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    declared = {
        re.split(r"[<>=!\[ ]", dep, maxsplit=1)[0].lower()
        for dep in pyproject["project"]["dependencies"]
    }
    text = (REPO / "requirements.txt").read_text()
    pinned = {
        re.split(r"[<>=!\[ ]", line.strip(), maxsplit=1)[0].lower()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    missing = declared - pinned
    assert not missing, f"requirements.txt is missing runtime deps: {sorted(missing)}"


def _run_snippet(code: str, env_extra: dict[str, str] | None = None) -> dict:
    """Run a snippet in a fresh interpreter and return its JSON output.

    Import-time behaviour cannot be observed from inside an already-imported
    process, and backend selection happens at import, so a subprocess is the only
    honest way to test it.
    """
    import os

    env = {k: v for k, v in os.environ.items()
           if k not in {"DATABASE_URL", "PACT_DATA_DIR", "PACT_REQUIRE_REAL_PAYPAL",
                        "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "LLM_API_KEY"}}
    env["PYTHONPATH"] = str(BACKEND)
    env.update(env_extra or {})
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, f"snippet failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


# ------------------------------------------- local development stays unchanged


def test_default_backend_is_filesystem_without_database_url(tmp_path):
    """Local development and Docker must keep using the filesystem store."""
    out = _run_snippet(
        """
import json
from pact.core.store import job_store, reputation
print(json.dumps({
    "backend": job_store.backend,
    "durable": job_store.durable,
    "reputation_durable": getattr(reputation, "durable", None),
}))
""",
        {"PACT_DATA_DIR": str(tmp_path / "data")},
    )
    assert out["backend"] == "filesystem", "the default backend must not change"
    assert out["durable"] is True, "a real writable directory is durable"


def test_read_only_data_dir_degrades_honestly_instead_of_crashing(tmp_path):
    """The serverless failure mode must not be reported as durable."""
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o500)
    try:
        out = _run_snippet(
            """
import json
from pact.core.store import job_store
print(json.dumps({"backend": job_store.backend, "durable": job_store.durable}))
""",
            {"PACT_DATA_DIR": str(readonly / "nested")},
        )
    finally:
        readonly.chmod(0o700)
    assert out["backend"] == "filesystem"
    assert out["durable"] is False, (
        "a fallback directory must never be reported as durable"
    )


# --------------------------------------- production must not fake a settlement


def test_production_refuses_to_settle_against_the_mock(tmp_path):
    """PACT_REQUIRE_REAL_PAYPAL must turn missing credentials into a hard failure."""
    code = """
import json
from pact.paypal import build_escrow_client
try:
    client = build_escrow_client()
    print(json.dumps({"mode": getattr(client, "environment", "?"), "refused": False}))
except Exception as exc:
    print(json.dumps({"refused": True, "message": str(exc)}))
"""
    out = _run_snippet(code, {"PACT_REQUIRE_REAL_PAYPAL": "true"})
    assert out["refused"] is True, (
        "a deployment that requires real PayPal must not silently use the mock"
    )
    assert "PAYPAL_CLIENT_ID" in out["message"]


def test_mock_is_used_by_default_for_local_development(tmp_path):
    """Without the flag, the mock still works so the demo runs with no setup."""
    out = _run_snippet(
        """
import json
from pact.paypal import build_escrow_client
print(json.dumps({"mode": build_escrow_client().environment}))
"""
    )
    assert out["mode"] == "mock"


# ------------------------------------------------------- health must not lie


def test_health_reports_filesystem_and_not_durable_by_default(tmp_path):
    out = _run_snippet(
        """
import json
from pact.api.routes import health
body = health()
print(json.dumps({"storage": body["storage"], "status": body["status"],
                  "require_real": body["paypal"]["require_real"]}))
""",
        {"PACT_DATA_DIR": str(tmp_path / "data")},
    )
    assert out["storage"]["backend"] == "filesystem"
    assert out["storage"]["durable"] is True
    assert out["status"] == "ok"


def test_health_reports_degraded_when_the_database_is_unreachable(tmp_path):
    """An unreachable configured database must be visible, not a blanket 500."""
    out = _run_snippet(
        """
import json
from pact.api.routes import health
body = health()
print(json.dumps({"storage": body["storage"], "status": body["status"]}))
""",
        {"DATABASE_URL": "postgresql://nobody@127.0.0.1:1/nope"},
    )
    assert out["storage"]["backend"] == "unavailable"
    assert out["storage"]["durable"] is False
    assert out["storage"]["settlement_disabled"] is True
    assert out["status"] == "degraded"
    assert out["storage"]["error"], "the cause must be reported, not hidden"


# ------------------------- production must not silently lose shared guarantees
#
# A production deployment with no DATABASE_URL loses two instance-safety
# guarantees at once: the consumed-SSE-ticket registry becomes process-local (a
# replay landing on another instance is accepted) and the ledger becomes a
# non-durable local file (lost on cold start, invisible to other instances). Both
# are silent — the deployment looks healthy. These tests pin the enforcement that
# turns each into a hard, visible failure, and confirm local development is
# unaffected.


def _run_startup(env_extra: dict[str, str]) -> dict:
    """Import the app in a fresh interpreter and run its startup hook.

    Startup runs at import-adjacent time and depends on import-time store
    selection, so it can only be observed from a separate process.
    """
    import os

    code = """
import asyncio, json
from pact.main import announce_configuration
from pact.config import settings
from pact.core.store import job_store
from pact.core.ticket_store import ticket_store
out = {
    "is_production": settings.is_production,
    "problems": settings.validate_runtime(),
    "ticket_backend": ticket_store.backend,
    "ticket_shared": ticket_store.shared,
    "job_backend": job_store.backend,
    "job_durable": job_store.durable,
}
try:
    asyncio.run(announce_configuration())
    out["started"] = True
    out["error"] = None
except Exception as exc:  # the refusal we are asserting on
    out["started"] = False
    out["error"] = str(exc)
print("JSON:" + json.dumps(out))
"""
    env = {k: v for k, v in os.environ.items()
           if k not in {"DATABASE_URL", "DATABASE_URL_UNPOOLED", "PACT_DATA_DIR", "PACT_ENV",
                        "PACT_REQUIRE_REAL_PAYPAL",
                        "PACT_ALLOW_MOCK_PAYPAL", "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET",
                        "LLM_API_KEY"}}
    env["PYTHONPATH"] = str(BACKEND)
    env.update(env_extra)
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, f"snippet crashed:\n{result.stdout}\n{result.stderr}"
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("JSON:"))
    return json.loads(line[len("JSON:"):])


# A sound production base: real auth, real PayPal, mock explicitly off. Each test
# overrides only the one variable it is about, so a failure cannot be blamed on an
# unrelated missing setting.
_PROD_BASE = {
    "PACT_ENV": "production",
    "PACT_AUTH_SECRET": "a-sufficiently-long-random-secret-value",
    "PAYPAL_CLIENT_ID": "sandbox-id",
    "PAYPAL_CLIENT_SECRET": "sandbox-secret",
    "PAYPAL_ENVIRONMENT": "sandbox",
    "PACT_ALLOW_MOCK_PAYPAL": "false",
}


def test_production_refuses_to_disable_money_route_rate_limiting(tmp_path):
    """The money routes must not be unbounded in production.

    Switching the guard off is a deliberate act, and it is refused where it
    matters: unbounded provider calls from one caller is the condition the guard
    exists to prevent.
    """
    out = _run_startup({
        **_PROD_BASE,
        "PACT_DATA_DIR": str(tmp_path / "data"),
        "PACT_RATE_LIMIT_ENABLED": "false",
    })
    assert any("PACT_RATE_LIMIT_ENABLED" in p for p in out["problems"]), out["problems"]
    assert out["started"] is False


def test_a_nonsensical_rate_limit_is_refused_in_production(tmp_path):
    """A zero limit would silently disable the guard, so it is refused, not ignored."""
    out = _run_startup({
        **_PROD_BASE,
        "PACT_DATA_DIR": str(tmp_path / "data"),
        "PACT_RATELIMIT_RUN_LIMIT": "0",
    })
    assert any("PACT_RATELIMIT_RUN_LIMIT" in p for p in out["problems"]), out["problems"]
    assert out["started"] is False


def test_a_nonsensical_rate_limit_is_reported_in_development(tmp_path):
    """Development still starts, but the misconfiguration is surfaced, not hidden."""
    out = _run_startup({
        "PACT_ENV": "development",
        "PACT_DATA_DIR": str(tmp_path / "data"),
        "PACT_ALLOW_MOCK_PAYPAL": "true",
        "PACT_RATELIMIT_RUN_LIMIT": "0",
    })
    assert any("PACT_RATELIMIT_RUN_LIMIT" in p for p in out["problems"]), out["problems"]


def test_production_without_database_url_is_refused(tmp_path):
    out = _run_startup({**_PROD_BASE, "PACT_DATA_DIR": str(tmp_path / "data")})
    assert out["is_production"] is True
    assert any("DATABASE_URL" in p for p in out["problems"]), out["problems"]
    assert out["started"] is False, "production must refuse to start without a database"
    assert "DATABASE_URL" in out["error"]
    # No local fallback is ever served: the process never starts with one.
    assert out["ticket_shared"] is False, "sanity: no DSN really does mean a local registry"


def test_production_with_empty_database_url_is_refused(tmp_path):
    out = _run_startup({**_PROD_BASE, "PACT_DATA_DIR": str(tmp_path / "data"), "DATABASE_URL": ""})
    assert any("DATABASE_URL" in p for p in out["problems"]), out["problems"]
    assert out["started"] is False


def test_production_with_whitespace_database_url_is_refused(tmp_path):
    """An empty-looking value is not a configured database."""
    out = _run_startup({**_PROD_BASE, "PACT_DATA_DIR": str(tmp_path / "data"), "DATABASE_URL": "   "})
    assert any("DATABASE_URL" in p for p in out["problems"]), out["problems"]
    assert out["started"] is False


def test_development_without_database_url_still_starts(tmp_path):
    """Local development, which has no Postgres, must keep working."""
    out = _run_startup({
        "PACT_ENV": "development",
        "PACT_DATA_DIR": str(tmp_path / "data"),
        "PACT_ALLOW_MOCK_PAYPAL": "true",
    })
    assert out["is_production"] is False
    assert out["started"] is True, out["error"]
    assert out["job_backend"] == "filesystem"
    assert out["ticket_backend"] == "memory", "development may use the process-local registry"


def test_production_with_unavailable_postgresql_is_refused(tmp_path):
    """A configured-but-unreachable database must fail closed at startup."""
    out = _run_startup({
        **_PROD_BASE,
        "PACT_DATA_DIR": str(tmp_path / "data"),
        "DATABASE_URL": "postgresql://nobody@127.0.0.1:1/nope",
    })
    # The DSN is present, so config validation passes; the failure is the store.
    assert out["problems"] == [], out["problems"]
    assert out["ticket_backend"] == "unavailable"
    assert out["started"] is False, "an unreachable production database must refuse to boot"
    assert "DATABASE_URL" in out["error"]
    # The refusal message must never echo credentials.
    assert "nobody" not in out["error"]


def test_production_never_falls_back_to_local_ticket_storage(tmp_path):
    """With no DSN, production is refused rather than served a local registry."""
    out = _run_startup({**_PROD_BASE, "PACT_DATA_DIR": str(tmp_path / "data")})
    # If it had started, ticket storage would have been process-local. It did not.
    assert not (out["started"] and out["ticket_backend"] == "memory")


def test_production_never_falls_back_to_non_durable_ledger(tmp_path):
    """With no DSN, production is refused rather than served a local ledger."""
    out = _run_startup({**_PROD_BASE, "PACT_DATA_DIR": str(tmp_path / "data")})
    assert not (out["started"] and out["job_backend"] == "filesystem" and out["job_durable"])


def test_production_with_valid_postgresql_starts(tmp_path):
    """With a reachable database the deployment starts and the store is shared."""
    import os

    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        pytest.skip("DATABASE_URL not set; the valid-Postgres startup test is opt-in")
    out = _run_startup({
        **_PROD_BASE,
        "PACT_DATA_DIR": str(tmp_path / "data"),
        "DATABASE_URL": dsn,
    })
    assert out["problems"] == [], out["problems"]
    assert out["started"] is True, out["error"]
    assert out["ticket_backend"] == "postgres"
    assert out["ticket_shared"] is True
    assert out["job_backend"] == "postgres"
    assert out["job_durable"] is True


def test_production_with_a_neon_style_pooled_dsn_is_accepted(tmp_path):
    """A Neon ``-pooler`` host must boot: the direct endpoint is derived, not required.

    Production is a Neon pooled endpoint and has no ``DATABASE_URL_UNPOOLED``. Neon
    documents the direct endpoint as the pooled host with ``-pooler`` removed, so the
    deployment must not be refused for lacking the variable.
    """
    out = _run_startup({
        **_PROD_BASE,
        "PACT_DATA_DIR": str(tmp_path / "data"),
        "DATABASE_URL": "postgresql://user:pw@ep-x-pooler.c-1.us-east-2.aws.neon.tech/pact",
    })
    assert out["problems"] == [], out["problems"]

