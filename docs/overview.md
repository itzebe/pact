# PACT — overview

PACT is **conditional settlement infrastructure for the agent economy**: money moves
only when an independent verifier confirms that a delivery meets the buyer's
acceptance criteria. Funds are held in PayPal escrow before any work starts and
released (`capture`) on a pass or returned (`void`) on a fail. Every step is written
to a tamper-evident, hash-chained ledger.

- Live application: <https://pact-flame.vercel.app/>
- Health endpoint: <https://pact-flame.vercel.app/api/health>
- Source: <https://github.com/itzebe/pact>
- Status of each capability: [`implementation-status.md`](implementation-status.md)

## Problem statement

Two autonomous agents that do not trust each other cannot safely transact:

- If the **buyer pays first**, the seller can take the money and never deliver.
- If the **seller delivers first**, the buyer can refuse to pay.

Intermediaries solve this for humans, but the "agent trust layer" ecosystem mostly
settles in crypto, which excludes ordinary fiat commerce. Meanwhile the everyday
blocker to autonomous purchasing is not capability — it is that nobody will hand an
agent a card and hope for the best.

PayPal already exposes the exact primitive this needs. `Orders v2` with
`intent=AUTHORIZE` **reserves** funds instead of taking them, and then branches:
`capture` to release, or `void` to return. That is escrow. PACT builds the
coordination protocol — negotiation, verification, reputation, and an audit trail —
on top of it.

## Product philosophy

1. **Payment follows verified delivery.** The verifier is the load-bearing
   component: it decides whether held funds are released or returned.
2. **The engine decides, the model proposes.** The budget ceiling, the escrow state
   machine, and every money decision are enforced in code. The LLM negotiates and
   judges, but it never authorizes a payment.
3. **Fail closed.** When something cannot be verified — an auth secret is missing, a
   webhook signature cannot be checked, a provider times out — PACT refuses rather
   than assumes success.
4. **Never fake a settlement.** A simulated capture is flagged `simulated` and
   reported as `settlement_is_real: false`; it is never presented as a payment.
5. **Zero friction to evaluate.** The product is guest-first: the first screen is
   the working product, with no account to create.

## Who it serves

| Persona | What they need | How PACT serves them |
|---|---|---|
| **Buyer agent** | To commission work without risking payment for nothing | Funds are held, not paid, until a verified delivery |
| **Seller agent** | To be paid for delivered work without trusting the buyer | The hold is committed before work starts and captured on a pass |
| **Marketplace developer** | A settlement primitive that is not crypto | A REST API + escrow lifecycle on fiat rails |
| **Evaluator / researcher** | A runnable, auditable reference implementation | Guest-first demo, open source, hash-chained audit ledger |

## Main user journeys

### Journey 1 — the guest evaluation (no account)

1. A visitor opens the live URL. The server provisions an isolated guest workspace
   and sets a session cookie.
2. They pick a job preset (or write one) and press **Post job and settle**.
3. Seller agents negotiate, the funds are held (buyer approval on a real sandbox
   deployment), the seller delivers, the verifier checks it, and the ledger streams
   the outcome live.
4. They can re-verify the ledger hash chain and, on a captured job, issue a refund.

### Journey 2 — the operator (bearer token)

1. An operator mints a token with `python -m scripts.issue_token <subject> <role>`.
2. They call the API directly (or paste the token into the UI) to post, run, approve,
   reconcile, delete, and refund jobs according to their role.

### Journey 3 — the buyer-approval return (real sandbox)

1. A run stops at `awaiting_approval` and surfaces the PayPal approval link.
2. The buyer approves in PayPal, returns, and the client calls
   `POST /api/jobs/{id}/approve`; the run continues to settlement.

## Product boundaries

PACT is deliberately **not**:

- a generic payment gateway — it is an escrow/conditional-settlement layer;
- a custodian — PayPal holds the funds, PACT coordinates;
- a crypto product — it settles on fiat rails;
- a money-mover in the demo — the live deployment is PayPal **Sandbox**.

## Current capabilities

Implemented and verified (see [`implementation-status.md`](implementation-status.md)):

- Guest-first isolated sessions; job creation; multi-round negotiation with a budget
  guardrail; PayPal escrow hold/capture/void; delivery + independent verification;
  full and partial refunds; verified webhook ingestion with exactly-once processing;
  a hash-chained audit ledger; durable, instance-safe PostgreSQL persistence;
  fail-closed security.

## Current limitations

- **Sandbox only.** No real money moves in the demo; the client is pinned to the
  sandbox host and a sandbox-only guard can hard-refuse a live host.
- **No independent security audit or certification.**
- **Verification is heuristic**, not a formal proof of a deliverable.
- **Customer self-service accounts and transactional email are implemented but
  disabled by default** (they need a verified email sending domain).
- **The frontend is code-split**: the initial bundle is ~190 kB (gzip ~59 kB); the
  AG Grid ledger is a lazily-loaded ~1.26 MB chunk fetched only on the Ledger tab.

## Future direction (clearly separated from what is implemented)

These are ideas, **not shipped features**:

- Code-splitting the frontend bundle.
- A hosted, real-money deployment path with operational controls (this would require
  removing the sandbox-only guard under explicit operator intent).
- Pluggable verifiers (for example, running tests against a code deliverable).
- Additional settlement rails behind the same escrow interface.

Anything not in [`implementation-status.md`](implementation-status.md) as
*implemented* should be read as aspirational.

## Next

- How it is built: [`architecture.md`](architecture.md)
- How the money moves: [`payment-lifecycle.md`](payment-lifecycle.md)
- How to run it: [`development.md`](development.md)
