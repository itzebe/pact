"""Model-supplied booleans must be read strictly, or settlement fails open.

Language models routinely serialise a boolean as a string. ``bool("false")`` is
``True`` in Python, so the obvious coercion turns an explicit refusal into an
affirmation. On the settlement path that is fail-open in the worst possible
direction: a verifier that wrote ``"passed": "false"`` released the buyer's money
for a delivery it had just rejected.

These tests pin the strict reading and prove the settlement guard is load-bearing
with a negative control that re-enables the old coercion.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pact.core import settlement as settlement_module
from pact.core.llm import MockLLM, as_bool
from pact.core.models import JobStatus
from pact.core.settlement import _parse_verification

CRITERION = "The deliverable states the headline result"

# Every shape a provider might plausibly use for "no". All must read as False.
FALSEY_SHAPES = ["false", "False", "FALSE", " false ", "no", "No", "n", "0",
                 0, 0.0, None, "", "maybe", "unclear", [], {}]
# Unambiguous affirmatives only.
TRUTHY_SHAPES = [True, 1, "true", "True", "TRUE", "yes", "y", "1", "passed", "pass"]


@pytest.mark.parametrize("value", FALSEY_SHAPES)
def test_as_bool_reads_every_falsey_shape_as_false(value):
    assert as_bool(value) is False


@pytest.mark.parametrize("value", TRUTHY_SHAPES)
def test_as_bool_reads_only_clear_affirmatives_as_true(value):
    assert as_bool(value) is True


def test_the_builtin_coercion_is_why_this_exists():
    """Negative control for the helper itself.

    If ``bool`` did not have this behaviour the helper would be pointless; this
    asserts the trap is real so nobody "simplifies" ``as_bool`` back to ``bool``.
    """
    assert bool("false") is True, "the trap this helper exists to avoid"
    assert as_bool("false") is False


@pytest.mark.parametrize("value", FALSEY_SHAPES)
def test_a_string_false_verdict_does_not_pass(value):
    raw = {
        "passed": value,
        "score": 0.0,
        "criteria": [{"criterion": CRITERION, "passed": value, "evidence": "nothing found"}],
        "summary": "Not met.",
    }
    verdict = _parse_verification(raw, [CRITERION])
    assert verdict.passed is False, f"{value!r} must not read as a pass"
    assert verdict.criteria[0].passed is False


@pytest.mark.parametrize("value", TRUTHY_SHAPES)
def test_a_clear_affirmative_verdict_still_passes(value):
    raw = {
        "passed": value,
        "score": 0.9,
        "criteria": [{"criterion": CRITERION, "passed": value, "evidence": "present in the artifact"}],
        "summary": "Met.",
    }
    assert _parse_verification(raw, [CRITERION]).passed is True


def test_a_pass_that_skips_a_criterion_is_rejected():
    raw = {"passed": "true", "score": 1.0, "criteria": [], "summary": "trust me"}
    verdict = _parse_verification(raw, [CRITERION])
    assert verdict.passed is False
    assert "did not assess every acceptance criterion" in verdict.summary


def test_a_self_contradicting_pass_is_rejected():
    """A model that claims a pass while its own findings say "failed" is not trusted.

    Trusting the top-level flag alone would release the buyer's money on a verdict
    that contradicts itself, which is exactly the failure mode a verifier exists to
    prevent.
    """
    raw = {
        "passed": True,
        "score": 0.9,
        "criteria": [{"criterion": CRITERION, "passed": False, "evidence": "nothing found"}],
        "summary": "All criteria met.",
    }
    verdict = _parse_verification(raw, [CRITERION])
    assert verdict.passed is False
    assert "do not support it" in verdict.summary


def test_a_consistent_pass_with_all_criteria_met_still_passes():
    raw = {
        "passed": True,
        "score": 0.9,
        "criteria": [{"criterion": CRITERION, "passed": True, "evidence": "present"}],
        "summary": "All criteria met.",
    }
    assert _parse_verification(raw, [CRITERION]).passed is True


def test_the_old_coercion_would_have_failed_open(monkeypatch):
    """Negative control for the settlement guard.

    Re-instate ``bool`` as the parser and the string-false verdict becomes a pass.
    This is what proves the regression test above is load-bearing rather than
    merely describing behaviour that was already correct.
    """
    raw = {
        "passed": "false",
        "score": 0.0,
        "criteria": [{"criterion": CRITERION, "passed": "false", "evidence": "nothing"}],
        "summary": "Not met.",
    }
    monkeypatch.setattr(settlement_module, "as_bool", bool)
    assert _parse_verification(raw, [CRITERION]).passed is True, "bool() fails open here"


# ------------------------------------------------- money must not move


class _StringBoolVerifier(MockLLM):
    """A provider that says "failed" but writes the flag as a string."""

    is_mock = False
    name = "string-bool-provider"

    def complete_json(self, system: str, user: str, *, max_tokens: int = 800) -> dict:
        out = super().complete_json(system, user, max_tokens=max_tokens)
        if "TASK:verify" in system:
            return {
                "passed": "false",
                "score": 0.0,
                "criteria": [{"criterion": c, "passed": "false", "evidence": "nothing found"}
                             for c in out.get("criteria") or []],
                "summary": "Delivery does not meet the criteria.",
            }
        return out


def _run(tmp_path, llm):
    from pact.core.engine import PactEngine
    from pact.core.store import JobStore, ReputationLedger
    from pact.paypal import MockPayPalEscrowClient

    engine = PactEngine(
        llm=llm,
        escrow=MockPayPalEscrowClient(),
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
    )
    job = engine.run(
        engine.create_job(title="Coercion", brief="b", criteria=[CRITERION], budget_max=Decimal("300")),
        seller_ids=["seller_nova"],
    )
    return engine, job


def test_a_string_false_verdict_never_captures_the_payment(tmp_path):
    _, job = _run(tmp_path, _StringBoolVerifier())
    assert job.verification.passed is False
    assert job.status is not JobStatus.SETTLED
    assert job.escrow.capture_id == "", "a rejected delivery must not be paid"


class _StringBoolBuyer(MockLLM):
    """A buyer agent that declines, writing the flag as a string."""

    def complete_json(self, system: str, user: str, *, max_tokens: int = 800) -> dict:
        if "TASK:buyer_counter" in system:
            return {"price": 95, "accept": "false", "rationale": "declining this quote"}
        return super().complete_json(system, user, max_tokens=max_tokens)


def test_a_string_false_accept_does_not_commit_the_buyer():
    from pact.core.models import Job, Offer
    from pact.core.negotiation import NegotiationEngine

    job = Job(title="T", brief="b", acceptance_criteria=["c"], budget_max=Decimal("100"), currency="USD")
    offer = Offer(seller_id="seller_nova", price=Decimal("95"))
    _, accept, _ = NegotiationEngine(_StringBoolBuyer()).buyer_counter(job, offer, 1)
    assert accept is False, 'accept="false" must not commit the buyer'
