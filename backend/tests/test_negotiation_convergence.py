"""Regression tests for the negotiation convergence guarantee.

The reported production failure: a job with a USD 200 ceiling whose every quote
came in between USD 110 and USD 150 was recorded as ``failed`` with the ledger
summary "No seller produced an offer within budget". Every quote was affordable,
so the summary was false, and the job died before any PayPal step ran.

The cause was that the close was left entirely to the model. The buyer model
(``gpt-oss-20b`` on Groq) kept countering and never emitted ``accept: true``, and
its final-round call failed outright — the ledger shows three round-3 seller
quotes and no buyer response. ``negotiate`` returned an empty list, and the
engine reported that as a pricing failure.

The fix makes the engine, not the model, responsible for closing on an affordable
deal: when the rounds run out with no acceptance, every seller's latest quote that
is within budget is accepted. The budget ceiling stays absolute, so a job whose
every quote exceeds the budget still fails and still moves no money.

Each protection below has a negative control that fails if it is removed.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from pact.core.engine import PactEngine
from pact.core.llm import LLMError, MockLLM
from pact.core.models import EscrowState, JobStatus, Offer
from pact.core.negotiation import NegotiationEngine
from pact.core.store import JobStore, ReputationLedger
from pact.paypal import MockPayPalEscrowClient

CRITERIA = ["Hero headline under 12 words", "Three feature blocks", "A clear call to action"]


def _context_of(user: str) -> dict:
    return json.loads(user.split("CONTEXT:", 1)[1].split("\n\n", 1)[0])


class NeverAcceptsLLM(MockLLM):
    """A real-model stand-in that negotiates hard but never closes.

    It reproduces both things the production model did: it never returns
    ``accept: true``, and its buyer call raises on the final round. Quotes are
    still produced for every seller, so affordable offers exist.
    """

    is_mock = False
    name = "flaky-real-model"

    def complete_json(self, system: str, user: str, *, max_tokens: int = 800) -> dict:
        if "TASK:buyer_counter" in system and int(_context_of(user).get("round", 1)) >= 3:
            raise LLMError("provider returned a non-JSON body")
        out = super().complete_json(system, user, max_tokens=max_tokens)
        if "TASK:buyer_counter" in system:
            out = dict(out)
            out["accept"] = False
        return out


class SilentLLM(MockLLM):
    """No seller ever produces a quote."""

    is_mock = False
    name = "silent-model"

    def complete_json(self, system: str, user: str, *, max_tokens: int = 800) -> dict:
        if "TASK:seller_offer" in system:
            raise LLMError("provider unavailable")
        return super().complete_json(system, user, max_tokens=max_tokens)


def _engine(tmp_path: Path, llm) -> PactEngine:
    return PactEngine(
        llm=llm,
        escrow=MockPayPalEscrowClient(),
        store=JobStore(tmp_path / "jobs"),
        reputation_ledger=ReputationLedger(tmp_path / "rep.json"),
    )


def _job(engine: PactEngine, budget: str = "200"):
    return engine.create_job(
        title="Landing page copy for a fintech launch",
        brief="Write the hero, three feature blocks, and a CTA.",
        criteria=CRITERIA,
        budget_max=Decimal(budget),
    )


# ------------------------------------------------- the reported failure itself


def test_an_affordable_job_does_not_fail_when_the_model_never_accepts(tmp_path):
    """The exact production symptom: in-budget quotes, model never says accept.

    This is the regression test for the reported failure. Before the fix the job
    ended ``failed`` with escrow ``none`` and a false "within budget" summary.
    """
    engine = _engine(tmp_path, NeverAcceptsLLM())
    job = engine.run(_job(engine))

    assert job.offers, "sellers must still have quoted"
    assert any(o.price <= job.budget_max for o in job.offers), "at least one quote must be affordable"
    assert job.status is not JobStatus.FAILED, (
        "an affordable job must not fail just because the model never accepted"
    )
    assert job.seller_id is not None, "a winner must be selected"
    assert job.agreed_price is not None and job.agreed_price <= job.budget_max


def test_the_failure_summary_must_not_blame_the_budget_when_quotes_fit(tmp_path):
    """The summary is evidence. Claiming a budget problem when every quote fit is
    what misdirected the last investigation; this pins the corrected wording."""
    engine = _engine(tmp_path, NeverAcceptsLLM())
    job = engine.run(_job(engine))
    failed = [e for e in job.ledger if e.kind == "negotiation_failed"]
    assert not failed, "no negotiation_failed entry may be written when a deal was reachable"

    # Negative control on the message itself: when the ceiling is genuinely below
    # every quote, the summary must say so and name the cheapest quote.
    broke = _engine(tmp_path / "broke", MockLLM())
    hard = broke.run(_job(broke, budget="20"))
    summary = next(e for e in hard.ledger if e.kind == "negotiation_failed").summary
    assert "within budget" in summary and "20.00" in summary, summary
    assert "cheapest" in summary, summary


def test_the_engine_records_a_truthful_acceptance_when_it_closes(tmp_path):
    """The close must be auditable: a real acceptance entry and buyer message,
    not a silent jump to agreement."""
    engine = _engine(tmp_path, NeverAcceptsLLM())
    job = engine.run(_job(engine))

    acceptances = [e for e in job.ledger if e.kind == "acceptance"]
    assert acceptances, "the engine-enforced close must be recorded as an acceptance"
    assert all(e.actor == job.buyer_id for e in acceptances)
    buyer_msgs = [m for m in job.messages if m.role == "buyer" and m.text.startswith("Accepted")]
    assert buyer_msgs, "the buyer side of the dialogue must show the acceptance"
    # The ledger stays a valid hash chain after the extra entries.
    assert job.verify_chain() is True


def test_selection_still_uses_risk_adjusted_cost(tmp_path):
    """Closing in the engine must not bypass winner selection. Among affordable
    offers the cheapest still wins when reputations are equal."""
    engine = _engine(tmp_path, NeverAcceptsLLM())
    job = engine.run(_job(engine))
    affordable = [o for o in job.offers if o.price <= job.budget_max]
    cheapest = min(affordable, key=lambda o: o.price)
    assert job.agreed_price == cheapest.price, (
        f"expected the cheapest affordable offer {cheapest.price}, got {job.agreed_price}"
    )


# ------------------------------------------------------ the guardrail survives


def test_a_ceiling_below_every_quote_still_fails_and_moves_no_money(tmp_path):
    """The budget ceiling is still absolute. A deal that is genuinely unreachable
    must fail and must not create a PayPal object."""
    engine = _engine(tmp_path, MockLLM())
    job = engine.run(_job(engine, budget="20"))

    assert job.status is JobStatus.FAILED
    assert job.seller_id is None
    assert job.agreed_price is None
    assert job.escrow.state is EscrowState.NONE
    assert job.escrow.order_id == "", "no order may be created for an unreachable deal"
    assert job.escrow.authorization_id == ""


def test_the_convergence_close_never_accepts_a_quote_above_budget(tmp_path):
    """A model that returns an unaffordable quote and no acceptance must not
    produce an over-budget agreement. This is the guardrail the fix could have
    broken; it is asserted directly on the protocol."""
    class PriceyLLM(MockLLM):
        is_mock = False
        name = "pricey"

        def complete_json(self, system, user, *, max_tokens=800):
            if "TASK:seller_offer" in system:
                return {"price": 5000, "terms": "t", "delivery_estimate": "d", "confidence": 0.9}
            if "TASK:buyer_counter" in system:
                return {"price": 100, "accept": False, "rationale": "no"}
            return super().complete_json(system, user, max_tokens=max_tokens)

    engine = _engine(tmp_path, PriceyLLM())
    job = engine.run(_job(engine, budget="200"))

    assert job.status is JobStatus.FAILED, "an over-budget job must not settle"
    assert job.agreed_price is None
    assert job.escrow.state is EscrowState.NONE


def test_no_quote_at_all_reports_the_real_reason(tmp_path):
    """When sellers cannot be reached the summary must not blame the budget."""
    engine = _engine(tmp_path, SilentLLM())
    job = engine.run(_job(engine))

    assert job.status is JobStatus.FAILED
    summary = next(e for e in job.ledger if e.kind == "negotiation_failed").summary
    assert "No seller produced an offer" == summary, summary


# ------------------------------------------------- negative control, protocol


def test_the_protocol_returns_nothing_when_this_close_is_disabled(monkeypatch):
    """Negative control: with the convergence step neutralised, the same inputs
    again yield no accepted offer — proving the new code is what closes the deal."""
    engine = _engine(Path("/tmp"), NeverAcceptsLLM())  # store unused by negotiate
    job = engine.create_job(
        title="t", brief="b", criteria=CRITERIA, budget_max=Decimal("200")
    )
    negotiation = engine.negotiation

    original = negotiation.negotiate

    def without_close(job, seller_ids):
        # Re-implement the old behaviour: model-only close.
        accepted: list[Offer] = []
        committed: set[str] = set()
        last_counter: dict[str, Decimal] = {}
        for round_no in range(1, negotiation.max_rounds + 1):
            for seller_id in seller_ids:
                if seller_id in committed:
                    continue
                try:
                    offer = negotiation.seller_quote(job, seller_id, round_no, last_counter.get(seller_id))
                    counter, accept, _ = negotiation.buyer_counter(job, offer, round_no)
                except LLMError:
                    continue
                job.offers.append(offer)
                if accept and offer.price <= job.budget_max:
                    committed.add(seller_id)
                    accepted.append(offer)
                else:
                    last_counter[seller_id] = counter
            if accepted:
                break
        return accepted

    monkeypatch.setattr(negotiation, "negotiate", without_close)
    assert negotiation.negotiate(job, ["seller_atlas", "seller_nova", "seller_quill"]) == [], (
        "without the engine close there is no acceptance — the fix is load-bearing"
    )

    # And with the real implementation the same inputs do converge.
    monkeypatch.setattr(negotiation, "negotiate", original)
    job2 = engine.create_job(title="t", brief="b", criteria=CRITERIA, budget_max=Decimal("200"))
    assert negotiation.negotiate(job2, ["seller_atlas", "seller_nova", "seller_quill"]), (
        "the shipped implementation must accept an affordable quote"
    )


# ------------------------------------------------- PayPal request-id policy


class RecordingKeysEscrow(MockPayPalEscrowClient):
    """Records the PayPal-Request-Id sent with each provider call."""

    def __init__(self) -> None:
        super().__init__()
        self.keys: dict[str, list[str | None]] = {"order": [], "auth": [], "capture": [], "void": []}

    def create_order(self, **kwargs):
        self.keys["order"].append(kwargs.get("idempotency_key"))
        return super().create_order(**kwargs)

    def authorize_order(self, order_id, *, idempotency_key=None):
        self.keys["auth"].append(idempotency_key)
        return super().authorize_order(order_id, idempotency_key=idempotency_key)

    def capture_authorization(self, authorization_id, *, amount=None, currency=None,
                              note_to_payer="", idempotency_key=None):
        self.keys["capture"].append(idempotency_key)
        return super().capture_authorization(
            authorization_id, amount=amount, currency=currency,
            note_to_payer=note_to_payer, idempotency_key=idempotency_key,
        )

    def void_authorization(self, authorization_id, *, idempotency_key=None):
        self.keys["void"].append(idempotency_key)
        return super().void_authorization(authorization_id, idempotency_key=idempotency_key)


def test_authorization_uses_a_fresh_request_id_but_capture_is_stable(tmp_path):
    """Pin the PR #8 request-id policy.

    Authorization must use a *fresh* id per attempt, because PayPal replays the
    cached response for a repeated id and a stable key would pin a pre-approval
    ``ORDER_NOT_APPROVED`` forever. Capture and void must use a *stable,
    job-derived* id, so a genuine replay of the same operation is deduplicated by
    PayPal instead of moving money twice. This test locks both directions in.
    """
    escrow = RecordingKeysEscrow()
    engine = _engine(tmp_path, MockLLM())
    engine.escrow = escrow
    engine.settlement.escrow = escrow

    job = engine.run(_job(engine), seller_ids=["seller_nova"])

    order_keys = escrow.keys["order"]
    auth_keys = [k for k in escrow.keys["auth"] if k]
    capture_keys = [k for k in escrow.keys["capture"] if k]

    assert order_keys == [f"pact-order-{job.id}"], order_keys
    assert auth_keys, "the hold must have attempted an authorization"
    assert all(k.startswith(f"pact-auth-{job.id}-") for k in auth_keys), auth_keys
    assert len(set(auth_keys)) == len(auth_keys), "each authorize attempt needs a distinct request id"
    assert capture_keys == [f"pact-capture-{job.id}"], capture_keys
    assert "pact-order-" + job.id in order_keys[0]
    # The stable keys must carry no random component, or replay protection is lost.
    assert capture_keys[0].count("-") == 2, capture_keys[0]
