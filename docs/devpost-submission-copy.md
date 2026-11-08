# Devpost submission copy — PACT (PayPal AI Hackathon)

Field-by-field copy for the Devpost form, updated to match the current application and
[`README.md`](../README.md). The public page at
<https://devpost.com/software/pact-verified-payments-for-ai-agents> is **owner-edited**;
this file is the ready-to-paste revision. It is not a claim that the public page has been
changed.

Facts this revision corrects relative to the currently published page:

| Field | Published page | Corrected here | Why |
| --- | --- | --- | --- |
| "How we built it" backend | "React, Node.js, PostgreSQL" | **Python / FastAPI**, React + Vite, PostgreSQL | The backend is FastAPI; there is no Node server. Verified in `backend/pyproject.toml`. |
| Demo video link | (embedded) | Explicit public URL in the links table | The video is public: <https://www.youtube.com/watch?v=CMGsmqTr6FQ> (2:17). |
| "What's next" — reputation | listed as future | **Reputation is implemented** and feeds seller selection | `backend/pact/core/negotiation.py`, `store.py`, `dbstore.py`; covered by tests. |
| Positioning | guest-first stranger coordination | conditional settlement infrastructure for the agent economy | Unifies the story; both are true (see the note below). |

---

## Title

**PACT**

## Tagline (elevator pitch, one line)

Verifiable escrow for agent work — funds are held in PayPal, released only when an
independent verifier passes the delivery.

## Inspiration

AI agents can already negotiate, plan, and produce work. What they cannot do is **safely
pay each other**. Hand an agent a card and it hires another agent, and you have no recourse
when the work never arrives. Every serious "trust layer for agents" today — Circle, BNB
Chain, ERC-8004, x402 — settles in crypto, which excludes the overwhelming majority of real
commerce that runs on fiat rails.

Meanwhile PayPal already ships the exact primitive this needs. Orders v2 supports
`intent=AUTHORIZE`, which **reserves** funds instead of taking them, and then offers a
genuine two-way branch: `capture` to release, or `void` to return. That is escrow, and it
works in the sandbox. PACT is the coordination layer built on top of it.

## What it does

Two parties agree to a job. One fears paying and receiving nothing; the other fears
delivering and never being paid. PACT connects the **agreement** to the **payment** so that
money moves only when the agreed delivery conditions are verified.

The lifecycle is one mechanism:

```
AGREE ─▶ AUTHORIZE ─▶ DELIVER ─▶ VERIFY ─▶ SETTLE
                                   │
                        PASS ──────┴────── FAIL
                         │                   │
                    CAPTURE (release)    VOID (return)
```

1. **A guest workspace opens with no account.** A first-time visitor lands in an isolated
   workspace; a server-managed session is issued automatically. No form, no email.
2. **The buyer posts a job** with a brief, acceptance criteria, and a hard budget ceiling.
3. **Seller agents compete.** They quote; the buyer counters; the price converges over
   rounds. The budget is enforced in code — an agreement above the ceiling is rejected by
   the engine no matter what the model returns.
4. **Funds are held before work starts.** PACT creates a PayPal order with
   `intent=AUTHORIZE`. PayPal requires the buyer to approve it, so PACT surfaces the real
   approval link rather than pretending a created order is a hold. After approval, PACT
   authorizes it. The buyer has parted with nothing.
5. **The seller produces the deliverable.**
6. **An independent verifier checks it** against every acceptance criterion, recording
   per-criterion evidence. Deterministic checks (artifact present, no evasion markers) are
   combined with model judgement by AND, so a persuasive-but-empty delivery cannot pass.
7. **Settlement releases or returns.** Pass → `capture`: the seller is paid. Fail → `void`:
   the buyer is charged nothing.
8. **Everything is recorded** in a hash-chained ledger the buyer can re-verify, and a
   refund path reverses a capture — in full or in part — through PayPal.

Reputation feeds back: seller selection divides price by verified-delivery trust, so an
unreliable seller has to be materially cheaper to win. The market routes around failure with
no human in the loop.

## How we built it

A **FastAPI** backend (Python 3.11) and a **React + Vite + Tailwind** frontend, deployed on
**Vercel** with **PostgreSQL** (Neon) as the durable store and ledger.

- **PayPal escrow client**, built directly on **Orders v2 / Payments v2** — `intent=AUTHORIZE`,
  `authorize`, `capture`, `void`, and refunds. Every mutating call is idempotent via
  `PayPal-Request-Id`, so a retry cannot double-charge.
- **AI roles** (three distinct agents): seller quoting, buyer countering, and independent
  verification of the deliverable. Any OpenAI-compatible provider works; the deployed demo
  runs live on Groq's OpenAI-compatible endpoint with `openai/gpt-oss-20b` (the health
  endpoint reports `is_mock: false`). **AI never authorizes or moves money** — deterministic
  application logic and the buyer's own approval control financial execution.
- **AG Grid** renders the settlement ledger: sortable, resizable audit rows with live
  hash-chain status.
- **Hash-chained audit ledger**: each entry stores the hash of the one before it; editing
  history breaks the chain, and the UI verifies it live.
- **Guest sessions**: server-managed, HttpOnly/Secure/SameSite cookies with CSRF tokens;
  each guest gets an isolated workspace with ownership enforced on the backend.
- **Settlement concurrency**: a session-scoped advisory lock held across the external
  PayPal capture, run on a *direct* database endpoint (not the transaction-mode pooler) so
  the lock is real.

### Why not PayPal's Agent Toolkit

It hard-codes `"intent": "CAPTURE"` in its order builder, so it only does immediate
payment — no authorize, no void, and therefore no way to make settlement conditional on an
outcome. Escrow required building directly on the REST API.

## Challenges we ran into

- **Guest identity and ownership isolation.** Auto-provisioning a workspace with no
  credentials, while making sure one guest can never read or mutate another's jobs,
  required backend-enforced ownership on every route — not frontend filtering.
- **The PayPal Sandbox lifecycle.** The order → approve → authorize → capture → refund
  sequence has states (approval pending, hold placed, capture in flight) that must be
  surfaced truthfully; a job stuck at "awaiting approval" is not a hold.
- **Payment state transitions.** An unknown outcome must become `unresolved` for
  reconciliation, never an automatic retry; captures must be serialised so two requests
  cannot capture the same authorization twice.
- **Refund idempotency.** A retry after a full refund is a deliberate no-op returning the
  same refund id; over-large refunds are refused before the provider is called.
- **PostgreSQL / PgBouncer transaction pooling.** A session-scoped advisory lock held
  across an external call is silently lost through a transaction-mode pooler. We now run
  that lock on a direct endpoint and fail loudly if it is pointed at a pooler.
- **Defining the role of AI.** We wanted AI to make coordination more intelligent without
  giving a language model unrestricted authority over financial operations. We separated
  AI-assisted interpretation from deterministic payment execution.

## Accomplishments that we're proud of

- A **working, end-to-end product**, not a mock: guest onboarding → negotiation → a real
  PayPal Sandbox authorization → delivery → independent verification → capture or void →
  refund, all in one continuous flow.
- **Real AI inference** in three agent roles, with a deterministic fallback so the product
  still runs with no model key — and the mock path is loudly labelled, never disguised.
- **Honest payment states.** The buyer-approval step PayPal requires is surfaced rather
  than skipped; unknown outcomes become `unresolved` instead of a blind retry.
- **A hash-chained ledger** that makes the transaction record tamper-evident and
  independently re-checkable.
- **Payment safeguards**: ownership checks, idempotency, single-capture, refund limits, and
  a fail-closed mode that refuses to run when a guest session could reach a live host.

## What we learned

Connecting a payment API is not the same as building a reliable payment product. Financial
operations require careful state management, clear authorization boundaries, idempotency,
and safe handling of uncertainty. The second lesson: a payment integration is only as
trustworthy as its least honest state — surfacing "awaiting approval" and "unresolved"
truthfully matters more than making the happy path look smooth.

Most importantly, trust should not depend on promises alone. The process should produce
records both sides can inspect.

## What's next for PACT

- **Partial release**, so a mostly-correct delivery can be paid proportionally.
- **Dispute resolution** with a third-party arbiter agent.
- **Streaming settlement** for long-running work, releasing in milestones.
- **Signed receipts**, so a completed settlement can be verified without trusting PACT.
- **Additional payment providers** and reusable agreement templates.

## Built with

`python` · `fastapi` · `react` · `vite` · `tailwindcss` · `ag-grid` · `docker` ·
`paypal` · `paypal-orders-api` · `paypal-sandbox` · `postgresql` · `neon` · `groq` ·
`gpt-oss-20b` · `vercel` · `llm` · `escrow` · `ai`

## Links

| | |
| --- | --- |
| Live demo (no account) | <https://pact-flame.vercel.app> |
| Source repository | <https://github.com/itzebe/pact> |
| Demo video (public, 2:17) | <https://www.youtube.com/watch?v=CMGsmqTr6FQ> |
| License | MIT (`LICENSE`) |
| Judge walkthrough | [`judge-demo/JUDGE_WALKTHROUGH.md`](judge-demo/JUDGE_WALKTHROUGH.md) |
| Evidence pack | [`judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md`](judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md) |
| Architecture | [`TOPOLOGY.md`](TOPOLOGY.md) |

## Payment disclosure

This demonstration uses **PayPal Sandbox**. It exercises the integrated payment workflow —
approval, authorization, capture, settlement, and refund — but **transfers no real money**.

---

## Unifying the two descriptions (note for the operator)

The published Devpost page and the README describe the same product from two angles. Both
are true, and the revised copy above states both:

- **Conditional settlement infrastructure for the agent economy** is the *thesis*: the
  reason the product exists and where it creates value.
- **Guest-first coordination for people who have never met** is the *surface*: how a first
  visitor reaches the product with no account and how the agreement-to-payment link is
  demonstrated.

The unifying sentence: *PACT connects an agreement to a payment that only proceeds when the
agreed delivery conditions are verified — whether the parties are people or agents.* No
roadmap feature is described as shipped: reputation, verification-gated settlement, refunds,
and the hash-chained ledger are all implemented and covered by tests, while partial release,
dispute resolution, and additional providers are listed only under "What's next".
