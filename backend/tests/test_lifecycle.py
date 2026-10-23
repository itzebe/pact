"""End-to-end lifecycle tests: negotiation, guardrails, settlement, audit."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from pact.core.engine import PactEngine
from pact.core.llm import MockLLM
from pact.core.models import EscrowState, JobStatus
from pact.core.store import JobStore, ReputationLedger
from pact.paypal import MockPayPalEscrowClient

CRITERIA = ["Hero headline under 12 words", "Three feature blocks", "A clear call to action"]


@pytest.fixture()
def engine(tmp_path: Path) -> PactEngine:
    return PactEngine(
        llm=MockLLM(),
        escrow=MockPayPalEscrowClient(),
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
    )


def make_job(engine: PactEngine, budget: str = "200", title: str = "Landing page copy"):
    return engine.create_job(
        title=title,
        brief="Write the hero and feature copy for a product launch page.",
        criteria=CRITERIA,
        budget_max=Decimal(budget),
    )


# ------------------------------------------------------------------ guardrails


def test_agreed_price_never_exceeds_the_budget(engine):
    """The budget is enforced in code, not merely requested of the model."""
    for budget in ("50", "137.42", "1000"):
        job = make_job(engine, budget=budget)
        result = engine.run(job)
        if result.agreed_price is not None:
            assert result.agreed_price <= Decimal(budget)


def test_negotiation_produces_a_multi_round_dialogue(engine):
    job = make_job(engine)
    result = engine.run(job)
    rounds = {m.round for m in result.messages}
    roles = {m.role for m in result.messages}
    assert len(rounds) >= 2, "negotiation should take more than one round"
    assert roles == {"buyer", "seller"}


def test_no_deal_is_struck_when_the_ceiling_is_below_every_seller_floor(engine):
    """A budget too small for any supplier must move no money at all.

    Sellers have an absolute cost floor, so a ceiling beneath it is genuinely
    unreachable rather than being quietly discounted to fit.
    """
    result = engine.run(make_job(engine, budget="20"))
    assert result.status is JobStatus.FAILED
    assert result.seller_id is None
    assert result.agreed_price is None
    assert result.escrow.state is EscrowState.NONE
    assert result.escrow.authorization_id == ""
    assert any(e.kind == "negotiation_failed" for e in result.ledger)


def test_every_seller_gets_to_quote(engine):
    job = make_job(engine)
    result = engine.run(job)
    quoted = {o.seller_id for o in result.offers}
    assert quoted == {"seller_atlas", "seller_nova", "seller_quill"}


# ----------------------------------------------------------------- settlement


def test_successful_delivery_releases_escrow(engine):
    job = make_job(engine)
    # Force a seller that actually delivers the work.
    result = engine.run(job, seller_ids=["seller_nova"])
    assert result.verification.passed is True
    assert result.escrow.state is EscrowState.RELEASED
    assert result.escrow.capture_id
    assert result.status is JobStatus.SETTLED


def test_failed_delivery_voids_the_hold_and_charges_nothing(engine):
    """The refund path is the reason escrow is used instead of immediate capture."""
    job = make_job(engine)
    result = engine.run(job, seller_ids=["seller_quill"])
    assert result.verification.passed is False
    assert result.escrow.state is EscrowState.VOIDED
    assert result.escrow.capture_id == "", "a voided hold must never produce a capture"
    assert result.status is JobStatus.REFUNDED


def test_escrow_is_held_before_the_deliverable_exists(engine):
    """Funds must be committed before work is produced, or escrow means nothing."""
    job = make_job(engine)
    result = engine.run(job, seller_ids=["seller_nova"])
    kinds = [e.kind for e in result.ledger]
    assert kinds.index("escrow_held") < kinds.index("delivery")


# ---------------------------------------------------------------- reputation


def test_reputation_records_settlement_and_refund(engine):
    engine.run(make_job(engine, title="Good job"), seller_ids=["seller_nova"])
    engine.run(make_job(engine, title="Bad job"), seller_ids=["seller_quill"])

    nova = engine.reputation.get("seller_nova")
    quill = engine.reputation.get("seller_quill")
    assert nova["settled"] == 1 and nova["refunded"] == 0
    assert quill["refunded"] == 1 and quill["settled"] == 0
    assert nova["score"] > quill["score"]


def test_market_routes_away_from_a_seller_that_fails_verification(engine):
    """Reputation has to change behaviour, or it is just a dashboard number."""
    first = engine.run(make_job(engine, title="First"), seller_ids=["seller_quill", "seller_nova"])
    second = engine.run(make_job(engine, title="Second"), seller_ids=["seller_quill", "seller_nova"])

    # The unreliable seller is cheapest and may win the first job, but must lose
    # the next one once its record is known.
    assert second.seller_id != "seller_quill"
    assert second.escrow.state is EscrowState.RELEASED


# -------------------------------------------------------------------- ledger


def test_ledger_chain_is_valid_and_detects_tampering(engine):
    job = make_job(engine)
    result = engine.run(job)
    assert result.verify_chain() is True
    assert len(result.ledger) > 10

    # Rewriting history must invalidate the chain.
    result.ledger[3].summary = "tampered"
    assert result.verify_chain() is False


def test_ledger_records_the_paypal_objects_that_moved_money(engine):
    job = make_job(engine)
    result = engine.run(job, seller_ids=["seller_nova"])
    held = next(e for e in result.ledger if e.kind == "escrow_held")
    released = next(e for e in result.ledger if e.kind == "escrow_released")
    assert held.detail["authorization_id"] == result.escrow.authorization_id
    assert released.detail["capture_id"] == result.escrow.capture_id


def test_ledger_sequence_numbers_are_contiguous(engine):
    job = make_job(engine)
    result = engine.run(job)
    assert [e.seq for e in result.ledger] == list(range(len(result.ledger)))


def test_verification_records_per_criterion_evidence(engine):
    job = make_job(engine)
    result = engine.run(job, seller_ids=["seller_nova"])
    criteria = result.verification.criteria
    assert len(criteria) >= len(CRITERIA)
    assert all(c.evidence for c in criteria), "every verdict needs stated evidence"


# ---------------------------------------------------------------- durability


def test_jobs_survive_a_restart(tmp_path: Path):
    store = JobStore(tmp_path / "jobs")
    ledger = ReputationLedger(tmp_path / "rep.json")
    first = PactEngine(llm=MockLLM(), escrow=MockPayPalEscrowClient(), store=store, reputation_ledger=ledger)
    job = first.create_job(title="Persisted", brief="b", criteria=CRITERIA, budget_max=Decimal("100"))
    first.run(job, seller_ids=["seller_nova"])

    reloaded = JobStore(tmp_path / "jobs")
    restored = reloaded.get(job.id)
    assert restored is not None
    assert restored.escrow.capture_id == job.escrow.capture_id
    assert restored.verify_chain() is True
