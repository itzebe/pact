# Hackathon submission — PACT

Submission copy for the **PayPal AI Hackathon**
(<https://paypalaihackathon.devpost.com/>, submission period closes **Thu Nov 12, 2026
12:00 pm Pacific** — per the [official rules](https://paypalaihackathon.devpost.com/rules)).
Field-by-field, ready to paste. Every claim here is backed by the implementation or by
[`judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md`](judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md).

---

## 1. Project title

**PACT**

## 2. One-line description

Verifiable escrow for agent work: two strangers agree, funds are held in PayPal, and
money moves only when an independent verifier passes the delivery.

## 3. Short description (~50 words)

Two strangers agree to a job; each fears being burned. Pact holds the buyer's funds in
PayPal before work starts, has an independent AI verifier check the delivery against
every acceptance criterion, and releases or returns the money on that verdict — with a
hash-chained record of every step. No sign-up to try it.

## 4. Inspiration

Agents can already negotiate, plan and produce work. What they cannot safely do is **pay
each other**. Hand an agent a card and it hires another agent, and you have no recourse
when the work never arrives. Every serious "agent trust layer" settles in crypto, which
excludes real commerce; meanwhile PayPal already ships the exact primitive this needs.
`intent=AUTHORIZE` *reserves* funds instead of taking them, and then branches: `capture`
to release, or `void` to return. That is escrow. Pact is the coordination layer built on
top of it.

## 5. What it does — the end-to-end journey

1. **A guest workspace opens with no account.** A first-time visitor lands in an isolated
   workspace; a server-managed session is issued automatically. No form, no email.
2. **The buyer posts a job** with a brief, acceptance criteria, and a hard budget ceiling.
3. **Seller agents compete.** They quote; the buyer counters; the price converges over
   rounds. The budget is enforced in code — an agreement above the ceiling is rejected by
   the engine no matter what the model returns.
4. **Funds are held before work starts.** Pact creates a PayPal order with
   `intent=AUTHORIZE`. PayPal requires the buyer to approve it, so Pact surfaces the real
   approval link rather than pretending a created order is a hold. After approval, Pact
   authorizes it. The buyer has parted with nothing.
5. **The seller produces the deliverable.**
6. **An independent verifier checks it** against every acceptance criterion, recording
   per-criterion evidence. Deterministic checks and model judgement are combined with AND.
7. **Settlement releases or returns.** Pass → `capture`: the seller is paid. Fail → `void`:
   the buyer is charged nothing.
8. **Everything is recorded** in a hash-chained ledger the buyer can re-verify, and a
   refund path reverses a capture — in full or in part — through PayPal.

Reputation feeds back: seller selection divides price by verified-delivery trust, so an
unreliable seller has to be materially cheaper to win. The market routes around failure
with no human in the loop.

## 6. How we built it

A **FastAPI** backend and a **React + Vite + Tailwind** frontend, deployed on **Vercel**
with **PostgreSQL** (Neon) as the durable ledger.

- **PayPal escrow client** built directly on **Orders v2 / Payments v2** — `intent=AUTHORIZE`,
  `authorize`, `capture`, `void`, and refunds. Every mutating call is idempotent via
  `PayPal-Request-Id`, so a retry cannot double-charge.
- **AI roles** (three distinct agents): seller quoting, buyer countering, and independent
  verification of the deliverable. Any OpenAI-compatible provider works; the deployed demo
  runs live on Groq's OpenAI-compatible endpoint with `openai/gpt-oss-20b` (health reports
  `is_mock: false`).
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
payment — no authorize, no void, and therefore no way to make settlement conditional on
an outcome. Escrow required building directly on the REST API.

## 7. What makes it different

It is the **combination**, not any single piece:

- **Guest-first** — the product is usable in one click, no registration.
- **Isolated guest identities** — guests cannot see or touch each other's records.
- **Agreement-driven coordination** — negotiation converges to a bounded, enforced price.
- **Payment-provider integration** — a real PayPal hold, not a simulated one.
- **Verdict-gated settlement** — release depends on an independent verification, not a timer.
- **Refund controls** — capped and deduplicated, so asking twice never moves money twice.
- **Verifiable record** — a hash-chained ledger a third party can re-check.

The single most honest difference: **Pact surfaces the buyer-approval step PayPal requires
instead of pretending a created order is a hold.** Many "escrow on PayPal" demos skip it.

## 8. Challenges

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
- **Settlement concurrency.** The lock above is what makes single-capture provable under
  concurrent requests.
- **Production deployment.** Making all of the above boot in production (guards, pooled vs
  direct connections, sandbox-only mode) without a fake demo mode.

## 9. What we learned

The primitive already exists. The interesting work in agent commerce is not moving money —
it is deciding **when** money should move, and having a record that proves the decision
was made honestly. The second lesson was that a payment integration is only as trustworthy
as its least honest state: surfacing "awaiting approval" and "unresolved" truthfully
matters more than making the happy path look smooth.

## 10. Future improvements

- Partial release, so a mostly-correct delivery can be paid proportionally.
- Dispute resolution with a third-party arbiter agent.
- Streaming settlement for long-running work, releasing in milestones.
- Signed receipts so a completed settlement can be verified without trusting Pact.

## 11. Links

| | |
| --- | --- |
| Repository | <https://github.com/itzebe/pact> |
| Production (live) | <https://pact-flame.vercel.app> |
| License | MIT (`LICENSE`) |
| Judge walkthrough | [`judge-demo/JUDGE_WALKTHROUGH.md`](judge-demo/JUDGE_WALKTHROUGH.md) |
| Evidence pack | [`judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md`](judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md) |
| Screenshots | [`judge-demo/screenshots/`](judge-demo/screenshots) |
| Architecture | [`TOPOLOGY.md`](TOPOLOGY.md) |
| Demo video (final narrated, 2:17) | [`judge-demo/pact-demo-final-narrated.mp4`](judge-demo/pact-demo-final-narrated.mp4) |
| Demo video (silent walkthrough) | [`judge-demo/pact-walkthrough-silent.mp4`](judge-demo/pact-walkthrough-silent.mp4) |
| Demo video (YouTube, public) | <https://www.youtube.com/watch?v=CMGsmqTr6FQ> — "PACT — Agreements You Can Verify \| PayPal AI Hackathon 2026" (2:17) |
| Judge entry point | [`FINAL_JUDGE_PACKAGE.md`](FINAL_JUDGE_PACKAGE.md) |
| Judging strategy | [`JUDGING_STRATEGY.md`](JUDGING_STRATEGY.md) |

## 12. Built with

`python` · `fastapi` · `react` · `vite` · `tailwindcss` · `ag-grid` · `docker` ·
`paypal-orders-api` · `paypal-sandbox` · `postgresql` · `llm`

## 13. Tools — which were used, and how

| Tool | How it was used |
| --- | --- |
| **PayPal Orders v2 API** | The settlement rail. `intent=AUTHORIZE` on order creation, `POST /v2/checkout/orders/{id}/authorize` to place the hold, `POST /v2/payments/authorizations/{id}/capture` to release, `.../void` to return. Idempotent via `PayPal-Request-Id`. |
| **PayPal Payments v2 (refunds)** | `POST /v2/payments/captures/{id}/refund`, full or partial, capped and deduplicated. |
| **PayPal Sandbox** | The environment everything runs against, so the money movement is real API traffic without real money. |
| **AI model (OpenAI-compatible)** | Three agent roles — seller quoting, buyer countering, independent verification. Deployed demo runs on Groq `openai/gpt-oss-20b`. |
| **AG Grid** | The settlement ledger grid (`ag-grid-community` + `ag-grid-react` v36, Quartz theme): sortable, resizable audit rows with hash-chain status. |
| **Docker** | Single-container build serving API + built UI, so a judge can run the whole project with one command. |

---

## Judge quick-start

```bash
docker build -t pact . && docker run -p 8000:8000 pact
# open http://localhost:8000
```

No credentials required. Add `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` for real
Sandbox settlement. Full instructions in [`../README.md`](../README.md).

## Compliance notes

- **Sandbox only.** No real money moves anywhere in this project. The rules require the
  sandbox environment.
- **No fabricated evidence.** Every identifier in the evidence pack is a real Sandbox
  object; historical and fresh checks are labelled as such.
- **Word limits.** The Devpost form has no per-field word limit; the required text
  description above is intentionally compact. The demo video is under the 3-minute limit.
