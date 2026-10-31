"""The negotiation protocol between a buyer agent and competing seller agents.

This is a real multi-round bargaining loop with a hard economic constraint: the
buyer may never agree to a price above its budget, and the engine enforces that
in code rather than trusting the model to respect it. The model proposes; the
engine disposes. Any agent that hallucinates an out-of-budget agreement simply
does not get one.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

from ..config import settings
from .llm import LLM, LLMError, as_bool
from .models import Job, Message, Offer

log = logging.getLogger("pact.negotiation")

BUYER_SYSTEM = """You are the buyer agent for a business procuring a small, well-specified deliverable.

TASK:buyer_counter

You negotiate on behalf of the buyer. Rules you must follow:
- You may NEVER agree to a price above the buyer's maximum budget. The engine rejects it.
- Push for a fair price, but do not lose the deal over trivia when the ask is close to budget.
- Be concise and commercial. No pleasantries.

Respond with ONLY a JSON object:
{"price": <number>, "accept": <true|false>, "rationale": "<one or two sentences>"}"""

SELLER_SYSTEM = """You are a seller agent quoting for a small, well-specified deliverable.

TASK:seller_offer

You are one of several sellers competing for the same job. Rules:
- Quote a price that reflects the scope. You may quote above the stated ceiling; it is a ceiling, not an offer.
- Concede modestly each round when the buyer counters, but never below your floor of 55% of the stated ceiling.
- Payment is held in PayPal escrow and released only when the delivery passes verification, so do not over-promise.
- Be concise and commercial. No pleasantries.

Respond with ONLY a JSON object:
{"price": <number>, "terms": "<short terms>", "delivery_estimate": "<short>", "confidence": <0..1>, "rationale": "<one or two sentences>"}"""


class NegotiationEngine:
    def __init__(self, llm: LLM, max_rounds: int = 3) -> None:
        self.llm = llm
        self.max_rounds = max_rounds

    # -------------------------------------------------------------- seller

    def seller_quote(self, job: Job, seller_id: str, round_no: int, last_counter: Decimal | None = None) -> Offer:
        context = {
            "title": job.title,
            "brief": job.brief,
            "acceptance_criteria": job.acceptance_criteria,
            "budget_max": float(job.budget_max),
            "currency": job.currency,
            "round": round_no,
            "seller_id": seller_id,
            "buyer_counter": float(last_counter) if last_counter is not None else None,
        }
        raw = self._ask(
            SELLER_SYSTEM,
            f"CONTEXT:{json.dumps(context)}\n\nProduce your quote as a JSON object.",
            seller_id,
        )
        # A seller that cannot produce a usable positive price is dropped from the
        # round rather than silently priced at the buyer's ceiling. Falling back to
        # ``budget_max`` would let a malformed model response charge the buyer the
        # maximum they said they would pay, which is the opposite of a guardrail.
        price = _parse_price(raw.get("price"))
        if price is None:
            raise LLMError(f"{seller_id} returned no usable price ({raw.get('price')!r})")
        return Offer(
            seller_id=seller_id,
            price=price,
            terms=str(raw.get("terms", ""))[:400],
            delivery_estimate=str(raw.get("delivery_estimate", ""))[:120],
            confidence=_clamp(raw.get("confidence"), 0.0, 1.0, 0.5),
        )

    # --------------------------------------------------------------- buyer

    def buyer_counter(self, job: Job, offer: Offer, round_no: int) -> tuple[Decimal, bool, str]:
        """Return (counter_price, accept, rationale).

        The engine clamps the result: a buyer can never accept, or counter, above
        budget regardless of what the model returns.
        """
        context = {
            "title": job.title,
            "brief": job.brief,
            "acceptance_criteria": job.acceptance_criteria,
            "budget_max": float(job.budget_max),
            "currency": job.currency,
            "round": round_no,
            "max_rounds": self.max_rounds,
            "seller_id": offer.seller_id,
            "seller_price": float(offer.price),
            "seller_terms": offer.terms,
        }
        raw = self._ask(
            BUYER_SYSTEM,
            f"CONTEXT:{json.dumps(context)}\n\nRespond with your counter as a JSON object.",
            job.buyer_id,
        )

        counter = _parse_price(raw.get("price"))
        if counter is None:
            # The buyer's own counter is not a seller price, so defaulting it to the
            # seller's ask (clamped to budget) is safe — it never exceeds the ceiling.
            counter = min(offer.price, job.budget_max)
        accept = as_bool(raw.get("accept", False))

        if counter > job.budget_max:
            counter = job.budget_max
        # Hard guardrail: an out-of-budget acceptance is downgraded to a counter at budget.
        if accept and offer.price > job.budget_max:
            accept = False
        if accept:
            counter = min(offer.price, job.budget_max)

        return counter.quantize(Decimal("0.01")), accept, str(raw.get("rationale", ""))[:400]

    # ------------------------------------------------------------ protocol

    def negotiate(self, job: Job, seller_ids: list[str]) -> list[Offer]:
        """Run the protocol and return every offer the buyer accepted.

        Each round every still-uncommitted seller quotes, the buyer accepts or
        counters, and a seller that is accepted drops out. The round loop stops
        as soon as at least one offer has been accepted, so a seller cannot keep
        undercutting after the buyer has already agreed. Selection among the
        accepted offers is left to the caller, which can weigh more than price.

        The buyer model is not trusted to close the deal. If the rounds run out
        with no acceptance, the engine itself accepts every seller's latest quote
        that is within budget, so an affordable job cannot fail merely because the
        model never said the word. Quotes above budget are never accepted.
        """
        accepted: list[Offer] = []
        committed: set[str] = set()
        last_counter: dict[str, Decimal] = {}
        latest: dict[str, Offer] = {}
        rounds_run = 0

        for round_no in range(1, self.max_rounds + 1):
            rounds_run = round_no
            for seller_id in seller_ids:
                if seller_id in committed:
                    continue
                try:
                    offer = self.seller_quote(job, seller_id, round_no, last_counter.get(seller_id))
                except LLMError as exc:
                    log.warning("Seller %s failed to quote: %s", seller_id, exc)
                    continue

                job.offers.append(offer)
                latest[seller_id] = offer
                job.messages.append(
                    Message(
                        round=round_no,
                        role="seller",
                        agent_id=seller_id,
                        text=f"{job.currency} {offer.price:.2f} — {offer.terms or 'standard terms'}",
                        offer=offer.price,
                        rationale=offer.terms,
                    )
                )
                job.append(
                    "offer",
                    seller_id,
                    f"Quoted {job.currency} {offer.price:.2f} in round {round_no}",
                    price=float(offer.price),
                    round=round_no,
                    delivery_estimate=offer.delivery_estimate,
                    confidence=offer.confidence,
                )

                try:
                    counter, accept, rationale = self.buyer_counter(job, offer, round_no)
                except LLMError as exc:
                    log.warning("Buyer failed to counter %s: %s", seller_id, exc)
                    continue

                # The engine, not the model, decides whether an acceptance is valid.
                if accept and offer.price <= job.budget_max:
                    committed.add(seller_id)
                    accepted.append(offer)
                    job.messages.append(
                        Message(
                            round=round_no,
                            role="buyer",
                            agent_id=job.buyer_id,
                            text=f"Accepted at {job.currency} {offer.price:.2f}",
                            offer=offer.price,
                            rationale=rationale,
                        )
                    )
                    job.append(
                        "acceptance",
                        job.buyer_id,
                        f"Accepted {seller_id} at {job.currency} {offer.price:.2f}",
                        price=float(offer.price),
                        round=round_no,
                        rationale=rationale,
                    )
                else:
                    job.messages.append(
                        Message(
                            round=round_no,
                            role="buyer",
                            agent_id=job.buyer_id,
                            text=f"Counter {job.currency} {counter:.2f}",
                            offer=counter,
                            rationale=rationale,
                        )
                    )
                    job.append(
                        "counter",
                        job.buyer_id,
                        f"Countered {seller_id} at {job.currency} {counter:.2f}",
                        price=float(counter),
                        round=round_no,
                        rationale=rationale,
                    )
                    last_counter[seller_id] = counter

            if accepted:
                break

        if accepted:
            return accepted

        # Convergence guarantee. A real model does not always emit accept=true,
        # even when every quote is comfortably inside budget — it keeps countering,
        # or its final-round call fails outright. Leaving the close entirely to the
        # model makes an affordable job fail, which is the opposite of what the
        # budget ceiling means. So when the rounds run out with no acceptance, the
        # engine closes on each seller's latest in-budget quote, exactly mirroring
        # the buyer policy the deterministic agent already encodes. The budget
        # ceiling is still absolute: a quote above it is never accepted, so a job
        # whose every quote exceeds the budget still fails (and moves no money).
        # Selection among these offers is still the caller's, on risk-adjusted cost.
        for seller_id, offer in latest.items():
            if seller_id in committed or offer.price > job.budget_max:
                continue
            accepted.append(offer)
            rationale = "Rounds exhausted; accepting the latest quote because it is within budget."
            job.messages.append(
                Message(
                    round=rounds_run,
                    role="buyer",
                    agent_id=job.buyer_id,
                    text=f"Accepted at {job.currency} {offer.price:.2f}",
                    offer=offer.price,
                    rationale=rationale,
                )
            )
            job.append(
                "acceptance",
                job.buyer_id,
                f"Accepted {seller_id} at {job.currency} {offer.price:.2f}",
                price=float(offer.price),
                round=rounds_run,
                rationale=rationale,
            )

        return accepted

    @staticmethod
    def select_winner(offers: list[Offer], score_of) -> Offer | None:
        """Pick the best offer on risk-adjusted cost, not headline price.

        A seller with a poor verified-delivery record has to be materially
        cheaper to win, because the buyer is pricing in the chance that the
        escrow gets voided and the work has to be re-sourced. This is what makes
        reputation economically real rather than decorative: a seller that
        repeatedly fails verification gets routed around.

        ``score_of`` maps a seller id to a 0..1 trust score. Cost is divided by
        the score, floored so a very low score cannot produce a division blow-up.
        """
        if not offers:
            return None
        return min(
            offers,
            key=lambda o: float(o.price) / max(float(score_of(o.seller_id)), 0.05),
        )

    # ---------------------------------------------------------------- util

    def _ask(self, system: str, user: str, actor: str) -> dict:
        raw = self.llm.complete_json(system, user)
        if not isinstance(raw, dict):
            raise LLMError(f"{actor} returned a non-object response")
        return raw


def _parse_price(value) -> Decimal | None:
    """Parse a model-supplied price, or return None if it is unusable.

    Returns None for anything that is not a finite, strictly positive number with
    at most two decimal places. Callers must decide what an unusable price means;
    they must never substitute the buyer's ceiling for a seller's price.
    """
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    # Reject absurd magnitudes rather than passing them to PayPal.
    if parsed > Decimal("100000000"):
        return None
    try:
        return parsed.quantize(Decimal("0.01"))
    except Exception:
        return None


def _clamp(value, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except Exception:
        return default


def default_seller_ids() -> list[str]:
    return [s.strip() for s in settings.sellers.split(",") if s.strip()]
