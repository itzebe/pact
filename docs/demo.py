#!/usr/bin/env python3
"""Run the demo narrative end to end, on a clean ledger.

    python docs/demo.py

Three settlements are run in sequence. The first is won by the cheapest seller,
which happens to be unreliable, so verification fails and the hold is voided.
That result is recorded against the seller, and the second and third settlements
route to a seller that actually delivers — without any human intervention.

This is the whole thesis in one script: reputation has to change who gets paid,
or it is just a dashboard number.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pact.core.engine import PactEngine  # noqa: E402
from pact.config import settings  # noqa: E402
from pact.core.store import JobStore, ReputationLedger  # noqa: E402
from pact.paypal import build_escrow_client  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED, BLUE, YELLOW = "\033[32m", "\033[31m", "\033[34m", "\033[33m"

CRITERIA = [
    "Hero headline is under 12 words",
    "Exactly three feature blocks, each with a heading and one sentence",
    "A single clear call to action",
]


def line(char: str = "─", width: int = 74) -> None:
    print(f"{DIM}{char * width}{RESET}")


def headline(text: str) -> None:
    print()
    line("═")
    print(f"{BOLD}{text}{RESET}")
    line("═")


def show_run(index: int, job) -> None:
    print(f"\n{BOLD}Settlement {index}{RESET}  {DIM}{job.title}{RESET}")
    line()

    for msg in job.messages:
        who = "buyer " if msg.role == "buyer" else msg.agent_id
        accepted = msg.role == "buyer" and msg.text.startswith("Accepted")
        colour = GREEN if accepted else (BLUE if msg.role == "buyer" else YELLOW)
        print(f"  r{msg.round} {colour}{who:<13}{RESET} {msg.text}")

    print()
    print(f"  agreed price   {BOLD}${job.agreed_price:,.2f}{RESET}   (ceiling ${job.budget_max:,.2f})")
    print(f"  escrow         {job.escrow.provider} / {job.escrow.mode}")

    held = next((e for e in job.ledger if e.kind == "escrow_held"), None)
    if held:
        print(f"  {YELLOW}hold{RESET}           {held.detail['authorization_id']}")

    v = job.verification
    verdict = f"{GREEN}PASSED{RESET}" if v.passed else f"{RED}FAILED{RESET}"
    print(f"  verification   {verdict}  score {v.score:.2f}")
    for c in v.criteria[:3]:
        mark = f"{GREEN}✓{RESET}" if c.passed else f"{RED}✗{RESET}"
        print(f"                 {mark} {c.criterion[:58]}")

    if job.escrow.state.value == "released":
        captured = next(e for e in job.ledger if e.kind == "escrow_released")
        print(f"  {GREEN}released{RESET}       {captured.detail['capture_id']}")
        print(f"                 {DIM}the seller was paid{RESET}")
    else:
        voided = next((e for e in job.ledger if e.kind == "escrow_voided"), None)
        if voided:
            print(f"  {RED}voided{RESET}         {voided.detail.get('authorization_id', '')}")
        print(f"                 {DIM}the buyer was charged nothing{RESET}")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="pact-demo-"))
    try:
        escrow = build_escrow_client()
        real = settings.paypal.configured
        engine = PactEngine(
            escrow=escrow,
            store=JobStore(workdir / "jobs"),
            reputation_ledger=ReputationLedger(workdir / "reputation.json"),
        )

        headline("Pact — settlement infrastructure for the agent economy")
        print(f"{DIM}PayPal escrow: {'PayPal ' + escrow.environment if real else 'simulated (no credentials)'}")
        print(f"Agents:        {engine.llm.name}")
        print(f"Chain:         SHA-256 hash-chained audit ledger{RESET}")
        if not real:
            print(f"\n{YELLOW}No PayPal credentials found — running against the simulated escrow.{RESET}")
            print(f"{DIM}Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET for live sandbox settlement.{RESET}")

        jobs = []
        for i, title in enumerate(
            ("Launch page copy", "Pricing page copy", "Onboarding email sequence"), start=1
        ):
            job = engine.create_job(
                title=title,
                brief="Write the copy for a small-business payments product launch.",
                criteria=CRITERIA,
                budget_max=Decimal("200"),
            )
            jobs.append(engine.run(job))
            show_run(i, jobs[-1])

        headline("What changed between settlement 1 and settlement 3")

        first, last = jobs[0], jobs[-1]
        print(f"\n  Settlement 1 winner  {BOLD}{first.seller_id}{RESET} at ${first.agreed_price:,.2f}")
        print(f"  Settlement 3 winner  {BOLD}{last.seller_id}{RESET} at ${last.agreed_price:,.2f}")

        if first.seller_id != last.seller_id:
            print(
                f"\n  {GREEN}The market routed around the seller that failed.{RESET}\n"
                f"  {DIM}No human intervened. The failed verification became a reputation\n"
                f"  penalty, and the penalty changed who won the next job.{RESET}"
            )
        else:
            print(f"\n  {DIM}Same seller won both; reputation had no effect here.{RESET}")

        print(f"\n  {BOLD}Seller reputation{RESET}")
        line()
        for row in sorted(engine.reputation.all(), key=lambda r: -r["score"]):
            bar_width = max(1, int(row["score"] * 20))
            colour = GREEN if row["score"] >= 0.7 else (YELLOW if row["score"] >= 0.45 else RED)
            bar = f"{colour}{'█' * bar_width}{DIM}{'░' * (20 - bar_width)}{RESET}"
            print(
                f"  {row['agent_id']:<14} {bar} {row['score']:.2f}   "
                f"{DIM}{row['settled']} settled · {row['refunded']} refunded{RESET}"
            )

        total = len(last.ledger)
        print(f"\n  {BOLD}Audit ledger{RESET}")
        line()
        print(f"  {total} entries for the final settlement, chain valid: {last.verify_chain()}")
        for entry in last.ledger[:4]:
            print(f"    {entry.seq:02d} {entry.kind:<24} {DIM}{entry.summary[:44]}{RESET}")
        print(f"    {DIM}…{RESET}")
        print(f"    {last.ledger[-1].seq:02d} {last.ledger[-1].kind:<24} {DIM}{last.ledger[-1].summary[:44]}{RESET}")

        headline("Every settlement decision is in the record")
        print(f"{DIM}Tamper with any historical entry and the chain breaks, because each entry")
        print(f"stores the hash of the one before it.{RESET}\n")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
