"""AI provider behaviour: real, mock, and the fallback when the provider fails.

Pact speaks to any OpenAI-compatible chat-completions endpoint, and degrades to a
deterministic policy engine when no key is configured. Two properties matter:

* the health endpoint must distinguish a genuinely configured provider from the
  mock, so a deployment can never present mock agents as a real integration; and
* a provider failure must fail *closed* — a model that is down, slow, or holding
  invalid credentials must never be able to turn an unverified delivery into a
  captured payment.

The client is exercised through its real HTTP layer with only the network boundary
stubbed, matching how the PayPal client is tested.
"""

from __future__ import annotations

import json

import httpx
import pytest

from pact.core import llm as llm_module
from pact.core.llm import LLMError, MockLLM, OpenAICompatibleLLM, build_llm


class Transport:
    """A scriptable httpx transport that records the requests it receives."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: list[tuple[int, dict]] = []
        self.raise_on: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_on is not None:
            raise self.raise_on
        status, body = self.responses.pop(0) if self.responses else (200, {})
        return httpx.Response(status, json=body, request=request)


@pytest.fixture()
def transport(monkeypatch) -> Transport:
    t = Transport()
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(t.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "Client", fake_client)
    # Retries sleep between attempts; the tests do not need to.
    monkeypatch.setattr(llm_module.time, "sleep", lambda _seconds: None)
    return t


def _ok(content: str) -> tuple[int, dict]:
    return 200, {"choices": [{"message": {"content": content}}]}


def test_a_configured_key_makes_the_provider_real(transport):
    client = OpenAICompatibleLLM(api_key="test-key", base_url="https://example.test/v1", model="m")
    assert client.is_mock is False
    transport.responses = [_ok("hello")]
    assert client.complete("sys", "user") == "hello"
    sent = transport.requests[0]
    assert sent.headers["Authorization"] == "Bearer test-key"
    assert json.loads(sent.content)["model"] == "m"


def test_an_invalid_key_fails_without_retrying(transport):
    """A 4xx will not improve on retry, so it must not be retried or swallowed."""
    client = OpenAICompatibleLLM(api_key="bad", attempts=3)
    transport.responses = [(401, {"error": {"message": "invalid api key"}})]
    with pytest.raises(LLMError, match="401"):
        client.complete("sys", "user")
    assert len(transport.requests) == 1, "a bad key must not be retried"


def test_a_transient_5xx_is_retried_then_surfaces_as_an_error(transport):
    client = OpenAICompatibleLLM(api_key="k", attempts=3)
    transport.responses = [(503, {}), (503, {}), (503, {})]
    with pytest.raises(LLMError, match="after 3 attempts"):
        client.complete("sys", "user")
    assert len(transport.requests) == 3


def test_a_timeout_is_a_transport_error_not_a_crash(transport):
    client = OpenAICompatibleLLM(api_key="k", attempts=2)
    transport.raise_on = httpx.TimeoutException("timed out")
    with pytest.raises(LLMError):
        client.complete("sys", "user")


def test_an_empty_body_is_retried_before_failing(transport):
    client = OpenAICompatibleLLM(api_key="k", attempts=2)
    transport.responses = [_ok(""), _ok("real content")]
    assert client.complete("sys", "user") == "real content"
    assert len(transport.requests) == 2


def test_json_is_extracted_from_a_fenced_block(transport):
    client = OpenAICompatibleLLM(api_key="k")
    transport.responses = [_ok('```json\n{"passed": true, "score": 0.9}\n```')]
    assert client.complete_json("sys", "user") == {"passed": True, "score": 0.9}


# ------------------------------------------- fail closed when the model is down


def test_a_model_outage_never_produces_a_pass(tmp_path):
    """A verifier that cannot reach its model must not release funds.

    This is the fail-safe direction: the buyer keeps the money rather than paying
    for a delivery that was never checked. If this ever flipped, an outage would
    become a way to get paid without verification.
    """
    from decimal import Decimal

    from pact.core.engine import PactEngine
    from pact.core.models import JobStatus
    from pact.core.store import JobStore, ReputationLedger
    from pact.paypal import MockPayPalEscrowClient

    class BrokenLLM(MockLLM):
        is_mock = False
        name = "broken-provider"

        def complete_json(self, system, user, *, max_tokens=800):
            if "TASK:verify" in system:
                raise LLMError("provider unavailable")
            return super().complete_json(system, user, max_tokens=max_tokens)

    engine = PactEngine(
        llm=BrokenLLM(),
        escrow=MockPayPalEscrowClient(),
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
    )
    job = engine.run(
        engine.create_job(title="Fail closed", brief="b", criteria=["c"], budget_max=Decimal("200")),
        seller_ids=["seller_nova"],
    )

    assert job.verification.passed is False, "an unreachable model must not pass a delivery"
    assert job.status is not JobStatus.SETTLED
    assert job.escrow.capture_id == "", "no capture may follow an unverified delivery"


# ------------------------------------------- health must not mislabel the mode


def test_health_reports_a_real_provider_as_not_mock(tmp_path, monkeypatch):
    """With a key set, health must report a real provider, not the mock."""
    from pact.config import settings
    from pact.core.engine import PactEngine
    from pact.core.store import JobStore, ReputationLedger
    from pact.paypal import MockPayPalEscrowClient

    object.__setattr__(settings.llm, "api_key", "test-key")
    try:
        engine = PactEngine(
            llm=build_llm(),
            escrow=MockPayPalEscrowClient(),
            store=JobStore(tmp_path / "jobs"),
            reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
        )
        assert engine.llm.is_mock is False
        assert engine.llm.name != "deterministic-mock"
    finally:
        object.__setattr__(settings.llm, "api_key", "")


def test_build_llm_returns_the_mock_only_when_unconfigured(monkeypatch):
    from pact.config import settings

    original = settings.llm.api_key
    try:
        object.__setattr__(settings.llm, "api_key", "")
        assert build_llm().is_mock is True
        object.__setattr__(settings.llm, "api_key", "test-key")
        assert build_llm().is_mock is False
    finally:
        object.__setattr__(settings.llm, "api_key", original)
