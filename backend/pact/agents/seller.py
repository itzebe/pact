"""Seller-side deliverable production.

A seller agent here is not a marketplace listing: it actually produces the
deliverable it was contracted for. That is what makes the escrow meaningful —
there is a real artifact to verify, so a pass or a fail is a statement about
something, not a coin flip.
"""

from __future__ import annotations

import json
import logging

from ..core.llm import LLM, LLMError
from ..core.models import Delivery, Job

log = logging.getLogger("pact.delivery")

DELIVERER_SYSTEM = """You are a seller agent that has been contracted for a small deliverable and paid through escrow.

TASK:deliver

Payment is released only if an independent verifier confirms your delivery meets the buyer's
acceptance criteria. So your delivery must genuinely contain the work, not a promise to do it.

Rules:
- Produce the actual deliverable inline as your "artifact".
- Address every acceptance criterion explicitly and concretely.
- Do not include evasion, disclaimers, or claims that cannot be checked in the text.
- Match the artifact's form to the job: code, copy, analysis, plan, or dataset as appropriate.

Respond with ONLY a JSON object:
{"summary": "<one or two sentences on what you produced>", "artifact": "<the full deliverable, plain text or markdown>"}"""


class SellerAgent:
    def __init__(self, llm: LLM, agent_id: str) -> None:
        self.llm = llm
        self.agent_id = agent_id

    def deliver(self, job: Job) -> Delivery:
        context = {
            "title": job.title,
            "brief": job.brief,
            "acceptance_criteria": job.acceptance_criteria,
            "agreed_price": float(job.agreed_price or 0),
            "currency": job.currency,
            "agreed_terms": job.agreed_terms,
            "seller_id": self.agent_id,
        }
        try:
            raw = self.llm.complete_json(
                DELIVERER_SYSTEM,
                f"CONTEXT:{json.dumps(context)}\n\nProduce the deliverable as a JSON object.",
                max_tokens=2000,
            )
        except LLMError as exc:
            log.warning("Seller %s failed to produce a deliverable: %s", self.agent_id, exc)
            raw = {
                "summary": "Delivery could not be produced.",
                "artifact": "",
            }

        return Delivery(
            seller_id=self.agent_id,
            summary=str(raw.get("summary", ""))[:1000],
            artifact=str(raw.get("artifact", "")),
        )
