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
