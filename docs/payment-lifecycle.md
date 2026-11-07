# PACT — payment lifecycle

This is the definitive description of how money moves (or does not) through PACT. It
documents the PayPal escrow lifecycle stage by stage, the code that handles each
stage, the database records it touches, the safeguards that apply, and how the
result is verified.

**Sandbox vs live.** Every stage below is implemented against PayPal's **Sandbox**
host (`api-m.sandbox.paypal.com`) by default. The client supports a `live`
environment, but the project's guard (`PACT_PAYPAL_SANDBOX_ONLY`) can hard-refuse a
live host, and startup refuses to run guest sessions against a live host. The demo
deployment is Sandbox: it moves **no real money**. Where this document says "money
moves", read it as "the provider records the movement" — in the demo, on the sandbox.

## The lifecycle at a glance

```
Job + offer ──▶ Order created ──▶ Buyer approves ──▶ Authorization (HOLD)
                                                          │
                       ┌──────────────────────────────────┤
                       ▼                                  ▼
              capture (release)                   void (return)
                       │                                  │
                       ▼                                  ▼
                   RELEASED                            VOIDED
                       │
                       ▼
              refund (full or partial) ──▶ REFUNDED
```

| State (`EscrowState`) | Meaning |
|---|---|
| `none` | No escrow opened |
| `awaiting_approval` | Order created; buyer has not approved |
| `held` | Authorization placed; funds reserved |
| `released` | Capture completed; seller paid |
| `voided` | Hold released; buyer charged nothing |
| `refunded` | A capture was reversed |
| `unresolved` | Outcome unknown; reconciliation required |

Job status (`JobStatus`) tracks the wider lifecycle and runs alongside escrow state
(`open`, `negotiating`, `agreed`, `awaiting_approval`, `escrowed`, `delivered`,
`verifying`, `settled`, `voided`, `refunded`, `unresolved`, `failed`).

## Stage 1 — Job and offer creation

- **Trigger:** `POST /api/jobs`, then `POST /api/jobs/{id}/run`.
- **Component:** `api/routes.py` → `core/engine.py` → `core/negotiation.py`.
- **Database:** a new `jobs` row (JSONB document) is written; each negotiation turn
  appends a `LedgerEntry`.
- **External API:** the LLM endpoint (seller quotes, buyer counters).
- **What can fail:** a model returns an unusable price (rejected and retried
  within the round), or no seller quotes within budget.
- **Safeguard:** the budget ceiling is enforced in code — an agreement above it is
  rejected regardless of the model's output. A convergence guarantee closes an
  affordable deal even if the model never emits an explicit acceptance.
- **Verification:** the agreed price and the winning seller are recorded in the job
  and in the ledger (`seller_selected`, `agreement`).

## Stage 2 — Payment order creation

- **Trigger:** negotiation has produced an agreed price and a seller.
- **Component:** `SettlementEngine.hold` → `PayPalEscrowClient.create_order`.
- **Database:** `escrow.order_id`, `escrow.amount`, `escrow.mode`, and
  `escrow.simulated` are written; ledger entries `escrow_requested`,
  `escrow_order_created`.
- **External API:** `POST /v2/checkout/orders` with `intent=AUTHORIZE`, an
  idempotency key `pact-order-{job.id}`, and `payment_source` at the order level so
  PayPal returns the modern `payer-action` approval link.
- **What can fail:** a transport error or a `5xx` is an `AmbiguousOutcome`.
- **Safeguard:** the order *reserves* rather than charges. `return_url`/`cancel_url`
  are built only from `PACT_PUBLIC_URL` or a host that passes the allowed-host
  allowlist, so a forged `Host` cannot redirect the buyer to an attacker.
- **Verification:** the order id and its approval URL are recorded; a created order
  is never treated as a hold.

## Stage 3 — Buyer approval

- **Trigger:** a real sandbox deployment cannot authorize an unapproved order, so
  the run stops at `awaiting_approval` and surfaces the order's `rel:payer-action`
  link.
- **Component:** `api/routes.py` (`POST /api/jobs/{id}/approve`) →
  `PactEngine.approve_and_hold`.
- **Database:** `escrow.state = awaiting_approval`, then the hold is recorded on
  approval; ledger entry `escrow_awaiting_approval`.
- **External API:** none at this stage (the buyer approves in PayPal's own UI).
- **What can fail:** the buyer never approves, or the order expires.
- **Safeguard:** the approval is claimed atomically with a TTL
  (`approval_claimed_at`, `approval_claim_ttl_seconds`) so two concurrent approvals
  cannot both proceed, and a worker that dies between claiming and returning does not
  block the buyer forever. `awaiting_approval` is deliberately **not** reclaimable by
  a lease, so a pending approval is never re-run into a second order.
- **Verification:** only a successful `authorize` moves the job out of
  `awaiting_approval`.

## Stage 4 — Authorization (the hold)

- **Trigger:** `authorize_hold`, called directly (simulated mode auto-approves) or
  after buyer approval.
- **Component:** `SettlementEngine.authorize_hold` →
  `PayPalEscrowClient.authorize_order`.
- **Database:** `escrow.authorization_id`, `escrow.authorization_status`,
  `escrow.state = held`; ledger entry `escrow_held`.
- **External API:** `POST /v2/checkout/orders/{id}/authorize`.
- **What can fail:** `ORDER_NOT_APPROVED`, `DENIED`, a transport error.
- **Safeguard:** the hold is recorded **only** when PayPal reports a holdable status
  (`CREATED`/`PENDING`). A `DENIED`/`VOIDED` result is a definite failure with no
  hold; an already-`CAPTURED` authorization means money may have moved and the job is
  marked `unresolved` rather than reported as held. The authorize retry uses a fresh
  idempotency key on purpose: PayPal replays a cached response for a repeated key, so
  a stable key would pin the first `ORDER_NOT_APPROVED` rejection forever (see the
  comment in `settlement.authorize_hold`).
- **Verification:** the authorization id and status are read back from PayPal's
  response and recorded in the ledger.

## Stage 5 — Delivery and verification

- **Trigger:** the hold is confirmed.
- **Component:** `agents/seller.py` produces the deliverable; `Verifier.verify` in
  `core/settlement.py` judges it.
- **Database:** `delivery`, `verification`, and ledger entries `delivery`,
  `verification_started`, `verification_result`.
- **External API:** the LLM endpoint (delivery production and verification).
- **What can fail:** the model returns no deliverable or a malformed verdict.
- **Safeguard:** verification is a conjunction — every deterministic check must pass
  **and** the model must pass. A model verdict is parsed strictly: `as_bool` fails
  closed, so `"passed": "false"` is `False`, never truthy.
- **Verification:** per-criterion results with evidence are stored; a failed or
  inconclusive verification never reaches the capture call.

## Stage 6 — Capture (release / settlement)

- **Trigger:** verification passed.
- **Component:** `SettlementEngine.release` → `PayPalEscrowClient.capture_authorization`.
- **Database:** `escrow.capture_id` is set to the in-flight sentinel and **persisted
  before the call**, then to the real capture id; `escrow.state = released`,
  `status = settled`; ledger entries `escrow_released`, `reputation_updated`.
- **External API:** `POST /v2/payments/authorizations/{id}/capture`, idempotency key
  `pact-capture-{job.id}`.
- **What can fail:** a definite failure (no money moved) withdraws the in-flight
  claim; an ambiguous outcome keeps the claim and marks the job `unresolved`.
- **Safeguard:** the whole transition is serialised on the settlement guard and the
  authorization is claimed before the call, so concurrent requests cannot capture
  twice. On PostgreSQL the guard is a session advisory lock held on a direct
  connection, so it holds across instances.
- **Verification:** the job is recorded as settled **only** when the capture status
  is `COMPLETED`. `PENDING`/`FAILED`/`DECLINED` leave the job `unresolved`.

## Stage 7 — Void (return on a failed verification)

- **Trigger:** verification failed.
- **Component:** `SettlementEngine.refund` (a void, despite the name — nothing was
  captured) → `PayPalEscrowClient.void_authorization`.
- **Database:** `escrow.state = voided`, `status = voided`; ledger entries
  `escrow_voided`, `reputation_updated`.
- **External API:** `POST /v2/payments/authorizations/{id}/void`, idempotency key
  `pact-void-{job.id}`.
- **What can fail:** a transport error or a provider refusal.
- **Safeguard:** PayPal refuses to void a captured authorization, so the capture and
  void paths are mutually exclusive; after the void, PACT **re-reads** the
  authorization to confirm it is `VOIDED` rather than reporting success on the
  strength of a success response alone.
- **Verification:** the confirmed-void flag is recorded in the ledger.

## Stage 8 — Refund (reverse a capture)

- **Trigger:** `POST /api/jobs/{id}/refund` on a `released` (or partially `refunded`)
  escrow.
- **Component:** `SettlementEngine.refund_capture` → `PayPalEscrowClient.refund_capture`.
- **Database:** `escrow.refund_id`, `escrow.refund_status`, `escrow.refunded_amount`,
  `escrow.refund_sequence`, `escrow.refund_ids`; ledger entry `escrow_refunded`.
- **External API:** `POST /v2/payments/captures/{id}/refund`, idempotency key
  `pact-refund-{job.id}-{sequence + 1}`.
- **What can fail:** an over-large request, or an ambiguous provider outcome.
- **Safeguard:** the sum of confirmed refunds can never exceed the captured amount —
  an over-large request is refused **before** PayPal is called. A fully-refunded
  capture is a no-op that returns the existing result, so a retry cannot refund twice.
  The provider refund id is the single source of truth: a refund already counted (for
  example by its webhook) is not counted again.
- **Verification:** only a `COMPLETED` refund is recorded; a `PENDING`/`FAILED`
  refund leaves the job `unresolved`.

## Stage 9 — Webhook processing

- **Trigger:** PayPal delivers an event to `POST /api/paypal/webhook`.
- **Component:** `api/routes.py` → `PayPalEscrowClient.verify_webhook_signature` →
  `core/webhooks.py` (`process_event`) → `core/webhook_store.py`.
- **Database:** `processed_webhooks` (event id claimed once); the affected `jobs`
  row.
- **External API:** PayPal's `POST /v1/notifications/verify-webhook-signature`.
- **What can fail:** an unverifiable event, a malformed body, a missing store.
- **Safeguard:** a webhook is a notification, not an instruction. It never moves
  money; it reads the provider, then records what the provider says. Only
  escrow-relevant event types are acted on; an unknown type is acknowledged and
  ignored rather than guessed at.
- **Verification:** the response code tells PayPal how to behave: `200` for an
  applied/duplicate/ignored/unknown-job event, `400` for a malformed body (a retry
  cannot fix it), `503` when verification is unavailable so PayPal retries later.

## Stage 10 — Failure handling and ambiguity

A timeout, a dropped connection, or a `5xx` does **not** prove the remote operation
failed — it may have been applied before the response was lost. PACT classifies these
as `AmbiguousOutcome` and marks the job `unresolved`, which:

- keeps any in-flight capture marker (so the "capture may have landed" fact survives);
- is never retried automatically (a blind retry can double-charge);
- must be resolved by `POST /api/jobs/{id}/reconcile` (admin), which reads PayPal
  directly to establish what actually happened.

## Stage 11 — Reconciliation

- **Trigger:** `POST /api/jobs/{id}/reconcile` on an `unresolved` job.
- **Component:** `SettlementEngine.reconcile` / `PactEngine.reconcile_job`.
- **Database:** the resolved escrow state; ledger entries describing the resolution.
- **External API:** `GET` on the authorization, capture, and (if the authorize itself
  was lost) the order.
- **What can fail:** PayPal is unreachable.
- **Safeguard:** reconciliation **asks PayPal, it does not guess**. A `CAPTURED`
  authorization settles the job using the real capture id recovered from PayPal and
  credits the seller (reversing the failure recorded while the outcome was unknown);
  an intact hold clears the in-flight marker so the job is retryable; an unreachable
  PayPal leaves the job `unresolved` and blocked, because an outage is not evidence
  that no money moved.
- **Verification:** the resolved state and the provider evidence are recorded.

## Duplicate-event handling and idempotency

| Operation | Idempotency mechanism |
|---|---|
| Create order | `PayPal-Request-Id: pact-order-{job.id}` |
| Authorize | Fresh key per attempt (deliberate — see Stage 4) |
| Capture | `PayPal-Request-Id: pact-capture-{job.id}` + in-flight claim persisted first |
| Void | `PayPal-Request-Id: pact-void-{job.id}` |
| Refund | `PayPal-Request-Id: pact-refund-{job.id}-{sequence+1}` |
| Webhook event | Event id claimed once in a shared store (`processed_webhooks`) |
| SSE stream ticket | Ticket id consumed once in a shared store (`consumed_tickets`) |

## Transaction verification (the audit ledger)

Every transition above appends a `LedgerEntry` to the job's ledger. Each entry stores
the SHA-256 hash of the previous entry, so the record is tamper-evident: editing any
historical row breaks the chain. `Job.verify_chain()` recomputes the chain, and
`GET /api/jobs/{id}/ledger` returns `chain_valid`. The UI verifies it live. This is
what makes the output defensible as a financial record rather than an application
log.

## Related

- The client's provider behaviour: [`architecture-decisions/0001-hand-written-paypal-client.md`](architecture-decisions/0001-hand-written-paypal-client.md)
- Security controls around the money path: [`security.md`](security.md)
- The refund state in the UI: `frontend/src/lib/status.js`
- Database records touched here: [`database.md`](database.md)
