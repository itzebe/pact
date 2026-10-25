# Submission copy

Paste-ready text for the Devpost submission form.

**Live demo:** <https://pact-flame.vercel.app/> · **Source:** <https://github.com/itzebe/pact>

---

## Inspiration

Agents can already negotiate, plan, and produce work. What they cannot do is **safely pay each other**. If you hand an agent a card and it hires another agent, you have no recourse when the work never arrives or arrives wrong.

Every serious attempt at an agent trust layer right now — Circle, BNB Chain, ERC-8004, x402 — settles in crypto. That rules out almost all real commerce, which runs on fiat. Meanwhile PayPal already ships the exact primitive this problem needs, and almost nobody has built on it this way.

PayPal Orders v2 supports `intent=AUTHORIZE`, which *reserves* funds instead of taking them, and then offers a genuine two-way branch: `capture` to release, or `void` to return. That is escrow. Pact is the protocol layer built on top of it.

## What it does

A buyer posts a job with a brief, acceptance criteria, and a hard budget ceiling.

1. **Seller agents compete.** They quote, the buyer counters, and the price converges over rounds. The budget is enforced in code — an out-of-budget agreement is rejected by the engine no matter what the model returns.
2. **Funds are held before work begins.** Pact creates a PayPal order with `intent=AUTHORIZE`, the buyer approves it, and Pact authorizes it. The seller has committed funds to deliver against; the buyer has parted with nothing. PayPal requires that approval step, so Pact surfaces it rather than pretending a created order is a hold.
3. **The seller produces the deliverable.**
4. **An independent verifier checks it** against every acceptance criterion, recording per-criterion evidence. Deterministic checks and model judgement are combined with AND, so a confident-sounding but empty delivery cannot pass.
5. **Settlement releases or returns.** Pass → `capture`, seller paid, reputation up. Fail → `void`, buyer charged nothing, reputation down.

The part that makes it more than a payment wrapper: **reputation feeds back into who wins the next job.** A seller with a poor verified-delivery record must be materially cheaper to win, so the market routes around unreliable sellers on its own. Reputation becomes an economic force rather than a dashboard number.

## How we built it

A FastAPI backend with a purpose-built PayPal escrow client, a multi-round negotiation engine, a verifier, a settlement engine, and a hash-chained audit ledger. React frontend with AG Grid for the ledger.

Three decisions worth calling out:

**We could not use PayPal's official Agent Toolkit.** It hard-codes `"intent": "CAPTURE"` in its order builder, so it only does immediate payment — no authorize, no void, and therefore no way to make settlement conditional on an outcome. Escrow required building directly on the REST API. Every call is idempotent via `PayPal-Request-Id`, so a retry cannot double-charge.

**The audit ledger is hash-chained.** Each entry stores the hash of the one before it. Editing any historical row breaks the chain, and the UI verifies it live. That is what makes the output defensible as a financial record rather than an application log.

**The mock path is deliberately loud.** With no credentials, Pact runs against an in-memory escrow double with the same state machine, and every id is prefixed `MOCK-` and labelled **Simulated PayPal** in the UI. A simulated settlement must never be mistaken for real money movement.

## Challenges

The hardest problem was not the integration, it was making the agents' incentives real. Our first version selected the cheapest offer, which meant the cheapest seller always won — and because we had built that seller to be unreliable, every single settlement failed verification and the hold was voided. The demo was correct and useless.

The fix was to price risk instead of headline cost: seller selection divides price by trust score, so a seller with a bad record has to be substantially cheaper to compete. That one change is what makes the reputation ledger load-bearing, and it turned the demo into the actual thesis — watch the market route around the seller that fails, with no human involved.

The second challenge was guardrails. Models will happily agree to a price above the stated ceiling. The engine validates every acceptance independently and rejects out-of-budget agreements, so the model cannot be talked into overspending.

## Accomplishments

- A complete, working escrow lifecycle on real PayPal primitives: authorize → verify → capture or void, including the buyer-approval step PayPal requires before a hold can exist.
- Payment invariants enforced in code and covered by tests: a hold is recorded only in a holdable state, settlement only on a `COMPLETED` capture, capture at most once under a lock, an unknown outcome marked `unresolved` for reconciliation instead of being retried, and a void re-read to confirm it landed.
- An agent market that visibly self-corrects through reputation, demonstrated end to end.
- A tamper-evident audit trail a finance team could actually reconcile against.
- Runs with zero setup: no PayPal account or API key needed to see the whole thing work, but real sandbox settlement the moment credentials are added.
- 234 tests, including the real PayPal client exercised through its HTTP layer with only the network boundary stubbed, and API contract tests against the live FastAPI app.

## What we learned

The primitive already exists. The interesting work in agent commerce is not moving money — it is deciding *when* money should move, and having a record that proves the decision was made honestly.

## What's next

- Partial release, so a mostly-correct delivery can be paid proportionally.
- Dispute resolution with a third-party arbiter agent.
- Streaming settlement for long-running work, releasing in milestones.
- Signed receipts so a completed settlement can be verified by a third party without trusting Pact.

---

## Built with

`python` · `fastapi` · `react` · `tailwindcss` · `ag-grid` · `vite` · `docker` · `paypal-orders-api` · `paypal-sandbox` · `llm`

---

## Tools — which were used, and how

| Tool | How it was used |
|---|---|
| **PayPal Orders v2 API** | The settlement rail. `intent=AUTHORIZE` on order creation, `POST /v2/checkout/orders/{id}/authorize` to place the hold, `POST /v2/payments/authorizations/{id}/capture` to release, `POST /v2/payments/authorizations/{id}/void` to return. Idempotent via `PayPal-Request-Id`. |
| **PayPal Sandbox** | The environment the demo runs against, so the money movement is real API traffic without real money. Sandbox credentials via `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET`. |
| **AG Grid** | The settlement ledger grid: sortable, resizable audit records with hash-chain status. |
| **AI model (OpenAI-compatible)** | Three distinct agent roles — seller quoting, buyer countering, and independent verification of the deliverable. Any OpenAI-compatible provider works; the model and base URL are configuration, not hard-coded. |
| **PayPal Agent Toolkit** | Evaluated as a candidate integration path. Rejected for this use case because it hard-codes immediate capture and cannot express a conditional hold. See "How we built it". |
| **Docker** | Single-container build that serves both the API and the built UI, so a judge can run the whole project with one command. |

---

## Judge quick-start

```bash
docker build -t pact . && docker run -p 8000:8000 pact
# open http://localhost:8000
```

No credentials required. Add `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` for live sandbox settlement.

Or without Docker — see [README.md](../README.md#quick-start). The narrative demo script:

```bash
python docs/demo.py
```

That script runs three settlements on a clean ledger and prints exactly how the market routes around the seller that fails verification.
