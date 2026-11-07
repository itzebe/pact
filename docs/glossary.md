# PACT — glossary

Terms used across PACT's documentation and code. Where a term maps to a code symbol,
the symbol is named.

## PACT

**PACT** — the project itself: conditional settlement infrastructure for the agent
economy, built on PayPal escrow. The name also appears as the API title and the
`Job`'s orchestrator class (`PactEngine`).

## Domain concepts

**Guest session** — a server-provisioned, isolated buyer workspace given to a
first-time visitor so the product can be used with no account. Implemented in
`backend/pact/core/guests.py`; delivered as an `HttpOnly`, `SameSite=Lax` cookie
scoped to `/api`.

**Job** — a unit of work: a title, a brief, acceptance criteria, a hard budget
ceiling, and a lifecycle. Represented by `Job` in `backend/pact/core/models.py` and
persisted as a JSONB document.

**Offer** — a seller's quote for a job: a price, terms, a delivery estimate, and a
confidence. Represented by `Offer`.

**Agreement** — the point at which a seller and a price are chosen. Recorded as the
`agreement` ledger entry; the job carries `agreed_price`, `agreed_terms`, and
`seller_id`.

**Delivery** — the artifact a seller produces, plus a summary. Represented by
`Delivery`.

**Verification** — the independent check of a delivery against each acceptance
criterion. Represented by `Verification` and `CriterionResult`; produced by
`Verifier` in `backend/pact/core/settlement.py`.

**Verifier** — the component that decides whether a delivery passes: deterministic
checks combined with model judgement by logical AND.

**Reputation** — a smoothed, verified-delivery score per seller, updated on every
settlement and used to bias seller selection. Implemented by `ReputationLedger`.

## Payment concepts

**Escrow** — holding a buyer's funds with a neutral party and releasing them only on a
condition. In PACT, PayPal holds the funds; PACT coordinates the condition.

**Authorization** — a PayPal `Orders v2` operation (`intent=AUTHORIZE`) that
**reserves** a buyer's funds without charging them. The reserved state is a "hold".

**Hold** — a recorded, holdable authorization (`EscrowState.HELD`). A hold is
recorded only when PayPal reports `CREATED`/`PENDING`.

**Capture** — the operation that releases held funds to the seller
(`POST /v2/payments/authorizations/{id}/capture`). Only a `COMPLETED` capture is
settlement.

**Settlement** — the terminal outcome of a job: a capture (paid) or a void (returned).

**Void** — the operation that releases a hold without moving money
(`POST /v2/payments/authorizations/{id}/void`). In PACT this is the "fail" branch and
the job ends `VOIDED` (the buyer was charged nothing). The method is named
`SettlementEngine.refund` for historical reasons — it voids a hold, it does not refund
a capture.

**Refund** — the operation that reverses a payment PayPal already captured
(`POST /v2/payments/captures/{id}/refund`), in full or in part. Distinct from a void.

**Payment state** — the `EscrowState` of a job: `none`, `awaiting_approval`, `held`,
`released`, `voided`, `refunded`, `unresolved`.

**Unresolved** — a payment state meaning the outcome is unknown (an ambiguous
provider result). It requires reconciliation and is never auto-completed.

**Reconciliation** — reading PayPal directly to establish what actually happened to an
`unresolved` job. `POST /api/jobs/{id}/reconcile` (admin).

**Simulated** — a settlement produced by the in-memory double rather than a real
provider. Flagged on the escrow record (`escrow.simulated`) and reported as
`settlement_is_real: false`; never presented as a real payment.

**Sandbox** — PayPal's test environment (`api-m.sandbox.paypal.com`). The demo runs
here; it moves no real money.

## Engineering concepts

**Ledger / audit ledger** — the append-only, per-job list of `LedgerEntry` records.
Each entry stores the SHA-256 hash of the previous entry, so the record is
tamper-evident.

**Hash chaining** — the mechanism that makes the ledger tamper-evident: each entry's
hash covers its own fields and the previous entry's hash. `Job.verify_chain()`
recomputes it.

**Webhook** — a PayPal event delivered to `POST /api/paypal/webhook`. A webhook is a
*notification, not an instruction*: it never moves money on its own; it is verified,
applied at most once, and only reads the provider to record what the provider says.

**Idempotency** — the property that repeating an operation has no additional effect.
PACT uses `PayPal-Request-Id` per operation, an in-flight capture claim, and
exactly-once registries for webhook events and SSE tickets.

**Ambiguous outcome** — a provider result that neither confirms nor denies success (a
timeout, a dropped connection, a `5xx`). Modelled as `AmbiguousOutcome`; leads to
`unresolved`, never to a blind retry.

**Settlement guard** — the per-job lock that serialises money transitions. In-process
it is a re-entrant lock; on PostgreSQL it is a session advisory lock held on a direct
connection so it holds across instances.

**Direct endpoint** — the unpooled PostgreSQL connection used for the settlement
session lock (`DATABASE_URL_UNPOOLED`, or a Neon-style host derived from the pooled
one). A transaction-mode pooler would release a session lock between statements.

**SSE stream ticket** — a single-use, 60-second, single-job capability that lets an
`EventSource` authenticate without putting a real token in a URL.

**CSRF token** — a per-session token that a cookie-authenticated request must echo on
mutating methods, so a cross-origin page cannot drive a money route.

**AI-assisted workflow** — the parts of PACT where a language model contributes:
seller quoting, buyer countering, deliverable production, and verification. The model
proposes; the engine decides.

**Fail closed** — the default on uncertainty: refuse rather than assume success.

## Related

- Terms in context: [`overview.md`](overview.md), [`payment-lifecycle.md`](payment-lifecycle.md)
- The full API: [`api/README.md`](api/README.md)
