"""Pool configuration: parsing the environment and validating the limits.

These are unit tests and need no database. They pin the contract that the
``PACT_PG_POOL_*`` / ``PACT_PG_STATEMENT_TIMEOUT_MS`` settings are real: parsed
from the environment, defaulted when unset, and rejected at startup when they are
values the driver would refuse (rather than raising later, on the first query).
"""

from __future__ import annotations

import dataclasses

import pytest

from pact.config import Settings, _env_float, _env_int


def test_env_int_uses_the_default_when_unset_or_blank(monkeypatch):
    monkeypatch.delenv("PACT_PG_POOL_MAX_SIZE", raising=False)
    assert _env_int("PACT_PG_POOL_MAX_SIZE", 5) == 5
    monkeypatch.setenv("PACT_PG_POOL_MAX_SIZE", "   ")
    assert _env_int("PACT_PG_POOL_MAX_SIZE", 5) == 5
    monkeypatch.setenv("PACT_PG_POOL_MAX_SIZE", "8")
    assert _env_int("PACT_PG_POOL_MAX_SIZE", 5) == 8


def test_env_float_uses_the_default_when_unset_or_blank(monkeypatch):
    monkeypatch.delenv("PACT_PG_POOL_TIMEOUT_SECONDS", raising=False)
    assert _env_float("PACT_PG_POOL_TIMEOUT_SECONDS", 5.0) == 5.0
    monkeypatch.setenv("PACT_PG_POOL_TIMEOUT_SECONDS", "2.5")
    assert _env_float("PACT_PG_POOL_TIMEOUT_SECONDS", 5.0) == 2.5


def test_a_malformed_pool_setting_raises_rather_than_being_ignored(monkeypatch):
    """A typo must fail loudly, not fall back to the default and look operational."""
    monkeypatch.setenv("PACT_PG_POOL_MAX_SIZE", "five")
    with pytest.raises(ValueError):
        _env_int("PACT_PG_POOL_MAX_SIZE", 5)


def test_the_pool_settings_default_to_the_documented_values(monkeypatch):
    for var in (
        "PACT_PG_POOL_MAX_SIZE",
        "PACT_PG_POOL_MIN_SIZE",
        "PACT_PG_POOL_TIMEOUT_SECONDS",
        "PACT_PG_POOL_MAX_IDLE_SECONDS",
        "PACT_PG_STATEMENT_TIMEOUT_MS",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.pg_pool_max_size == 5
    assert s.pg_pool_min_size == 0
    assert s.pg_pool_timeout_seconds == 5.0
    assert s.pg_pool_max_idle_seconds == 300.0
    assert s.pg_statement_timeout_ms == 10_000


def test_the_default_pool_settings_are_valid(monkeypatch):
    for var in (
        "PACT_PG_POOL_MAX_SIZE",
        "PACT_PG_POOL_MIN_SIZE",
        "PACT_PG_POOL_TIMEOUT_SECONDS",
        "PACT_PG_POOL_MAX_IDLE_SECONDS",
        "PACT_PG_STATEMENT_TIMEOUT_MS",
    ):
        monkeypatch.delenv(var, raising=False)
    assert Settings().validate_runtime() == []


@pytest.mark.parametrize(
    "field, value, needle",
    [
        ("pg_pool_max_size", 0, "PACT_PG_POOL_MAX_SIZE"),
        ("pg_pool_max_size", -1, "PACT_PG_POOL_MAX_SIZE"),
        ("pg_pool_min_size", -1, "PACT_PG_POOL_MIN_SIZE"),
        ("pg_pool_timeout_seconds", -1.0, "PACT_PG_POOL_TIMEOUT_SECONDS"),
        ("pg_pool_max_idle_seconds", -1.0, "PACT_PG_POOL_MAX_IDLE_SECONDS"),
        ("pg_statement_timeout_ms", -1, "PACT_PG_STATEMENT_TIMEOUT_MS"),
    ],
)
def test_validate_runtime_rejects_an_impossible_pool_limit(field, value, needle):
    bad = dataclasses.replace(Settings(), **{field: value})
    assert any(needle in p for p in bad.validate_runtime())


def test_validate_runtime_rejects_min_size_greater_than_max_size():
    bad = dataclasses.replace(Settings(), pg_pool_min_size=10, pg_pool_max_size=5)
    problems = bad.validate_runtime()
    assert any("PACT_PG_POOL_MIN_SIZE" in p and "PACT_PG_POOL_MAX_SIZE" in p for p in problems)


def _capture_get_pool_kwargs(monkeypatch, settings_overrides=None, **call_kwargs):
    """Call ``get_pool`` with a stubbed constructor and return the kwargs it passed.

    Proves the settings are actually handed to the pool. Uses a fake DSN the
    registry has never seen, so no real connection is opened and the shared pool
    registry is left untouched.
    """
    import pact.config as cfg
    from pact.core import pool as pool_mod

    captured: dict = {}

    class _Recorder:
        def __init__(self, dsn, **kwargs):
            captured["dsn"] = dsn
            captured.update(kwargs)

    if settings_overrides:
        monkeypatch.setattr(cfg, "settings", dataclasses.replace(cfg.settings, **settings_overrides))
    monkeypatch.setattr(pool_mod, "DatabasePool", _Recorder)
    dsn = "postgresql://u:p@pool-config-test.invalid:5432/db"
    try:
        pool_mod.get_pool(dsn, **call_kwargs)
    finally:
        pool_mod._pools.pop(dsn, None)
    return captured


def test_get_pool_passes_the_configured_pool_limits(monkeypatch):
    captured = _capture_get_pool_kwargs(
        monkeypatch,
        settings_overrides={
            "pg_pool_max_size": 7,
            "pg_pool_min_size": 1,
            "pg_pool_timeout_seconds": 2.0,
            "pg_pool_max_idle_seconds": 60.0,
            "pg_statement_timeout_ms": 1500,
        },
    )
    assert captured["max_size"] == 7
    assert captured["min_size"] == 1
    assert captured["timeout_seconds"] == 2.0
    assert captured["max_idle_seconds"] == 60.0
    assert captured["statement_timeout_ms"] == 1500


def test_get_pool_forwards_the_prepare_threshold_setting(monkeypatch):
    captured = _capture_get_pool_kwargs(
        monkeypatch, settings_overrides={"pg_prepare_threshold": None}
    )
    assert captured["prepare_threshold"] is None


def test_an_explicit_kwarg_wins_over_the_setting(monkeypatch):
    """A caller that knows better (e.g. a test) may still override a pool limit."""
    captured = _capture_get_pool_kwargs(
        monkeypatch, settings_overrides={"pg_pool_max_size": 7}, max_size=3
    )
    assert captured["max_size"] == 3


