# PACT — frequently asked questions

Answers are grounded in the implementation. Where a claim is backed by a file or a
test, the file is named. For the product story, see [`overview.md`](overview.md).

## What is PACT?

PACT is **conditional settlement infrastructure for the agent economy**. Two parties
agree to a small, well-specified deliverable; PACT holds the buyer's funds in PayPal
escrow before work starts, an independent verifier checks the delivery against the
buyer's acceptance criteria, and money moves only when it passes — `capture` on a
pass, `void` on a fail. Every step is written to a tamper-evident, hash-chained
ledger.

## What problem does it solve?

Two agents that do not trust each other cannot safely transact: a buyer who pays
first can be cheated, and a seller who delivers first can be stiffed. PACT removes
the need for trust by holding the funds with a neutral third party (PayPal) and
releasing them only on a verified outcome. See [`overview.md`](overview.md).

## Who is it for?

Autonomous buyer and seller agents; developers building agent marketplaces that need
a fiat settlement primitive; and evaluators who want a runnable, auditable reference
implementation of escrow-based conditional settlement.

## How does the guest-first model work?

A public deployment provisions an isolated, server-managed buyer workspace on the
first visit and sets an `HttpOnly`, `SameSite=Lax` session cookie scoped to `/api`.
There is nothing to register, paste, or verify. A guest is an ordinary `buyer`
principal, so every existing ownership check already scopes it to its own records; a
guest is never an `admin`. Mutating requests must echo a CSRF token returned on
session start. New workspaces are capped per client IP and each workspace has a job
quota. Source: `backend/pact/core/guests.py`, `backend/pact/api/auth_routes.py`.

## How are jobs created?

`POST /api/jobs` with a title, a brief, one or more acceptance criteria, and a hard
budget ceiling. The buyer identity comes from the authenticated token, never the
request body (a `buyer_id` field in the body is ignored and overwritten). All fields
are bounded. Source: `backend/pact/api/routes.py` (`JobCreate`).

## How are offers handled?

`POST /api/jobs/{id}/run` starts a multi-round negotiation. Each seller agent quotes;
the buyer counters; the price converges over up to three rounds. The budget ceiling
is enforced **in code** — an agreement above it is rejected regardless of what the
model returns — and a convergence guarantee closes an affordable deal even if the
model never emits an explicit acceptance. Source: `backend/pact/core/negotiation.py`.

## How does payment authorization work?

PACT creates a PayPal order with `intent=AUTHORIZE`, which **reserves** funds rather
than charging them. PayPal requires the buyer to approve the order before it can be
authorized, so a real sandbox run stops at `awaiting_approval` and surfaces the
order's `rel:payer-action` link. After approval, PACT authorizes the order and records
the hold only when PayPal reports a holdable status (`CREATED`/`PENDING`). Source:
`backend/pact/core/settlement.py` (`hold`, `authorize_hold`).

## Does PACT actually hold money?

No. **PayPal holds the money.** PACT coordinates the escrow: it creates the
authorization, decides when to capture or void, and records what happened. PACT is
not a custodian and never touches the funds directly.

## Is PACT using real money or Sandbox?

**Sandbox.** The client is pinned to PayPal's Sandbox host
(`api-m.sandbox.paypal.com`) by default, and the project ships a guard
(`PACT_PAYPAL_SANDBOX_ONLY`) that can hard-refuse a live host. The live demo moves no
real money. See [`payment-lifecycle.md`](payment-lifecycle.md).

To confirm the environment from outside, read `GET /api/health`: `paypal.environment`
is `sandbox`, `paypal.is_live` is `false`, and `paypal.mode` is `sandbox`. Note that
`paypal.sandbox_only` being `false` is expected — it reports whether the *guard* is
switched on, not whether the deployment is live; `paypal.is_live` is the indicator
that answers that question.

`PAYPAL_ENVIRONMENT` must be exactly `sandbox` or `live`. Any other value (a typo such
as `production`) is refused at startup, and `paypal.is_live` reports `true` for it, so
a mistyped environment can never be presented as a safe sandbox one.

## How does capture work?

When verification passes, PACT captures the authorization
(`POST /v2/payments/authorizations/{id}/capture`). Before the call it claims the
authorization (writing an in-flight marker and persisting it), and the whole
transition is serialised on the per-job settlement guard. The job is recorded as
settled **only** when the capture status is `COMPLETED`. Source:
`backend/pact/core/settlement.py` (`release`).

## How are refunds handled?

`POST /api/jobs/{id}/refund` reverses a captured payment, in full or in part, via
`POST /v2/payments/captures/{id}/refund`. The total refunded can never exceed the
captured amount — an over-large request is refused before PayPal is called — and each
refund operation has its own idempotency key, so a retry cannot refund twice. Source:
`backend/pact/core/settlement.py` (`refund_capture`).

Note the naming: a **void** (`settlement.refund`) releases a hold and moves no money;
a **refund** (`settlement.refund_capture`) returns money that was already captured.

## What prevents duplicate captures?

Three things, together:

1. The authorization is claimed (`IN_FLIGHT_CAPTURE`) and **persisted before** the
   provider call, so a second attempt sees a capture in progress.
2. The transition is serialised on the per-job settlement guard; on PostgreSQL that
   is a session advisory lock held on a direct connection, so it holds across
   instances.
3. PayPal itself is idempotent via `PayPal-Request-Id`.

Source: `backend/pact/core/settlement.py`, `backend/pact/core/dbstore.py`
(`settlement_claim`). Tests: `tests/test_settlement_pooling.py`.

## How does the AI functionality work?

Three AI roles share one OpenAI-compatible client: a **seller** agent (quotes, then
produces the deliverable), a **buyer** agent (counters, never above budget), and a
**verifier** (judges the delivery against each acceptance criterion). The AI proposes;
the engine decides. The model has no tools and no ability to call PayPal. Without an
API key, a deterministic mock runs so the lifecycle still works offline. See
[`ai-system.md`](ai-system.md).

## Can the AI initiate or authorize a payment?

**No.** There is no code path from a model response to a PayPal call. The model never
sees or supplies a PayPal id. The settlement engine independently decides whether
money moves, and verification is a conjunction of deterministic checks and the model
verdict, so the model cannot force a pass.

## What happens if PayPal fails?

A *definite* failure (a 4xx such as `ORDER_NOT_APPROVED` or `DENIED`) is recorded as
a failure with no hold. An *ambiguous* outcome (a timeout, a dropped connection, a
`5xx`) is different: it does not prove the operation failed, so the job is marked
`unresolved` and must be reconciled by reading PayPal. It is never retried
automatically, because a blind retry can double-charge. Source:
`backend/pact/core/settlement.py`, `backend/pact/paypal/escrow.py`.

## What happens if a webhook is repeated?

PayPal delivers webhooks at least once, so PACT claims each event id exactly once in
a shared store (PostgreSQL). A duplicate is acknowledged with `200` and ignored. Only
escrow-relevant event types are acted on; an unknown type is acknowledged and ignored.
An unverifiable event is refused (`401`) and changes nothing. Source:
`backend/pact/core/webhooks.py`, `backend/pact/core/webhook_store.py`.

## What happens if a caller floods the money routes?

Each money-bearing route (job create, run, approve, cancel, refund, reconcile, and
the provider-order read) enforces a per-identity fixed-window limit. A registered
account is keyed on its signed subject; a guest or anonymous caller is keyed on its
client address, so minting a fresh guest session does not reset the budget. Exceeding
a limit is `429` with a `Retry-After` header. When a database is configured the counter
is shared through PostgreSQL, so the limit is not spent once per instance; a
configured-but-unreachable store refuses rather than serving unbounded. Source:
`backend/pact/core/rate_limit.py`.

## What technology does PACT use?

Python 3.11 + FastAPI on the backend; React 18 + Vite + Tailwind on the frontend;
PostgreSQL for durable state; PayPal Orders v2 / Payments v2 for settlement; any
OpenAI-compatible LLM. The deployed demo uses Groq's `openai/gpt-oss-20b`. Full stack
table: [`overview.md`](overview.md) and [`architecture.md`](architecture.md).

## How can developers run PACT?

See [`development.md`](development.md). Short version: create a venv, `uv pip install
-e ".[dev]"`, set `PACT_AUTH_SECRET`, run `uvicorn pact.main:app`. No PayPal account
is required — the in-memory double runs the whole lifecycle.

## How can developers contribute?

See [`CONTRIBUTING.md`](../CONTRIBUTING.md). Run the test suites and `pyflakes`
before opening a pull request; keep money-path changes covered by tests that exercise
the real path.

## What are PACT's current limitations?

- Sandbox only; no real money moves in the demo.
- No independent security audit or certification.
- Verification is heuristic (a model plus deterministic checks), not a formal proof.
- Customer self-service accounts and transactional email are implemented but disabled
  by default (they need a verified email sending domain).
- The frontend bundle is a single large chunk (no code-splitting yet).

See [`implementation-status.md`](implementation-status.md) for the full, honest
inventory.

## Is PACT production-ready for real money?

Not as shipped. The code is fail-closed and the money invariants are tested, but a
real-money deployment would need an independent security review and operational
controls (key management, monitoring, incident response) that are out of scope here.
The sandbox-only guard exists precisely so an accidental live credential cannot move
real money.

## Related

- Glossary of terms: [`glossary.md`](glossary.md)
- The lifecycle in depth: [`payment-lifecycle.md`](payment-lifecycle.md)
- Security model: [`security.md`](security.md)
