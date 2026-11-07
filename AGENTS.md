# AGENTS.md — instructions for AI coding agents working on PACT

This file is the contract for any automated agent (or human) that edits this
repository. Read it before changing code. It is deliberately specific: PACT holds
and moves money through PayPal, so a plausible-looking change that weakens a
safety property is a defect even if the tests stay green.

## 1. What PACT is (do not change its identity)

PACT is **conditional settlement infrastructure for the agent economy**. Its
product thesis is one sentence: *money moves only when an independent verifier
confirms the delivery meets the buyer's acceptance criteria.* It is **guest-first**
(a public deployment provisions an isolated workspace with no account) and it is
built on **PayPal escrow** (`Orders v2` with `intent=AUTHORIZE`).

Do not turn PACT into a generic payment gateway. Do not remove or weaken the
guest-first experience. Do not represent PayPal **Sandbox** activity as
real-money movement. The deployed demo runs on the sandbox host by design.

The canonical description of the product lives in [`README.md`](README.md) and
[`docs/overview.md`](docs/overview.md). Keep them consistent; if you change one,
change the other.

## 2. Inspect before you change

- Read the module you are about to touch **and its tests** first. Almost every
  non-obvious decision in this codebase is explained in a docstring; the reasoning
  is part of the code.
- Do not assume a symbol is unused because it looks unused — search for it.
- The blast radius of anything under `backend/pact/core/settlement.py`,
  `backend/pact/core/dbstore.py`, `backend/pact/api/auth.py` and
  `backend/pact/api/routes.py` is the whole money path. Treat changes there as
  high-risk and cover them with a test.

## 3. Repository layout

```
backend/pact/
├── main.py            FastAPI app, middleware, SPA mount, startup validation
├── config.py          All settings + the fail-closed runtime validation
├── api/
│   ├── routes.py      Job lifecycle, ledger, SSE stream, PayPal webhook
│   ├── auth.py        Bearer/guest auth, roles, CSRF, stream tickets
│   └── auth_routes.py Customer accounts + guest session endpoints
├── core/
│   ├── engine.py      Orchestrator: one call drives brief → settled
│   ├── negotiation.py Multi-round bargaining + budget guardrail
│   ├── settlement.py  Verifier + escrow hold/release/void/refund
│   ├── models.py      Job, EscrowRecord, hash-chained LedgerEntry
│   ├── store.py       Filesystem job store + reputation ledger
│   ├── dbstore.py     PostgreSQL job store + reputation ledger (instance-safe)
│   ├── pool.py        Bounded per-instance connection pool; direct() for locks
│   ├── webhooks.py    Apply verified PayPal events to payment state
│   ├── webhook_store.py  Exactly-once webhook event registry
│   ├── ticket_store.py   Single-use SSE ticket registry
│   ├── guests.py      Guest (judge) workspace provisioning
│   ├── auth_store.py  Users, sessions, refresh tokens, email outbox
│   ├── accounts.py    Customer account lifecycle
│   ├── llm.py         Provider-agnostic LLM access + deterministic fallback
│   ├── passwords.py   Argon2id hashing
│   └── email.py       Transactional email dispatcher
├── paypal/
│   ├── escrow.py      Hand-written Orders v2 / Payments v2 escrow client
│   └── mock.py        In-memory double with the same state machine
└── agents/seller.py   Seller deliverable production

frontend/src/          React + Vite + Tailwind UI (guest-first)
api/index.py           Vercel Python entrypoint
docs/                  Canonical documentation (see docs/knowledge-map.md)
metadata/              Machine-readable project evidence (JSON)
```

## 4. Invariants you must not weaken

These are the properties the product is sold on. Each has a test; keep it passing
and add one if you extend the behaviour.

1. **Held means held.** A hold is recorded only when PayPal reports the
   authorization in a holdable state (`CREATED`/`PENDING`). `DENIED` is a failure;
   an already-`CAPTURED` authorization is flagged for reconciliation.
2. **Settled means paid.** A job is `settled` only when the capture status is
   `COMPLETED`. `PENDING`, `FAILED`, `DECLINED` are never settlement.
3. **Captured at most once.** The authorization is claimed (`IN_FLIGHT_CAPTURE`)
   and persisted *before* the provider call, and the whole transition is serialised
   on the settlement guard.
4. **An unknown outcome is not a failure.** A timeout / dropped connection / 5xx
   leaves the job `unresolved`. Never retry it automatically; reconcile instead.
5. **A void is confirmed** by re-reading the authorization from the provider.
6. **No capture without a passing verdict.** Verification is a conjunction of
   deterministic checks and model judgement.
7. **Fail closed.** Missing auth secret → refuse requests (503). Missing PayPal
   credentials in production → refuse to start. Unverifiable webhook → refuse.
8. **Simulated is never reported as real.** `escrow.simulated` is stamped at hold
   time and carried through; `settlement_is_real` is false for a mock.

## 5. Payment lifecycle rules

The lifecycle is linear and every transition appends to the ledger:

```
brief → negotiate → agree → HOLD (authorize) → deliver → verify → capture | void
```

- The `mode` of an escrow record is `sandbox`, `live`, or `mock`. **Never** enable
  `live`. `PACT_PAYPAL_SANDBOX_ONLY` exists so an accidental live credential cannot
  move real money — do not remove it, and do not point the client at a live host.
- `PACT_GUEST_SESSIONS` and a live PayPal host are mutually exclusive; startup
  refuses that combination. Keep it that way.
- Idempotency keys are per-operation and stable for a retry
  (`pact-order-{job}`, `pact-capture-{job}`, `pact-refund-{job}-{seq}`). An
  *authorize* retry deliberately uses a fresh key (see the comment in
  `settlement.authorize_hold`); do not "simplify" that.

## 6. Security restrictions

- Never commit secrets. `.env` is git-ignored; only `.env.example` (with empty
  values) is tracked.
- Never log or return a credential, a raw PayPal payload containing payer
  identity, or a full database connection string. Use the redaction helpers
  (`store.redact_credentials`, `routes._redact`).
- All untrusted input is validated at the boundary (Pydantic models + explicit
  bounds). Do not add an unvalidated path into a query, a URL, or a provider call.
- Authorization is object-level, not just route-level. Every access to a job checks
  that the caller is a party to it; a non-owner gets `404`, not `403`.
- Guest sessions are cookie-authenticated and require the CSRF token on mutating
  methods. Do not add a header path that accepts a guest token.

## 7. Testing requirements

- Run the suite before and after a change:
  - `cd backend && python -m pytest tests/ -q` (filesystem store; Postgres tests
    self-skip).
  - With a database: `DATABASE_URL=... python -m pytest tests/ -q` (nothing skips).
  - `cd frontend && npm test`.
  - `cd backend && python -m pyflakes pact/ scripts/`.
- **Never weaken or delete a test to make a change pass.** If a test is wrong, fix
  the test and say why; if the code is wrong, fix the code.
- A test that only proves the code compiles is not evidence. Exercise the real
  path (the suite already stubs only the network boundary for the PayPal client).
- Do not add mocks where the real object is cheap to construct.

## 8. Documentation requirements

- Update the docs that describe what you changed: `README.md`, `docs/*`, and the
  relevant `metadata/*.json` file. `docs/implementation-status.md` is the honest
  inventory — keep its status categories accurate.
- The documentation is part of the deliverable, not an afterthought. A stale
  command is a defect.
- Cross-link: every new document should be reachable from
  [`docs/knowledge-map.md`](docs/knowledge-map.md) and indexed in
  [`docs/README.md`](docs/README.md).

## 9. Prohibited shortcuts

- Do not fabricate functionality, test results, metrics, users, integrations, or
  screenshots. Report what you verified and how.
- Do not document a planned feature as implemented. Use the status vocabulary in
  [`docs/implementation-status.md`](docs/implementation-status.md).
- Do not replace working architecture with something simpler purely to make the
  documentation easier to write.
- Do not introduce unrelated features, dependencies, or refactors in a change
  scoped to something else.
- Do not modify application behaviour to make a documentation or lint check pass.

## 10. Reference

- Product and how to run it: [`README.md`](README.md)
- Architecture: [`docs/architecture.md`](docs/architecture.md)
- Payment lifecycle: [`docs/payment-lifecycle.md`](docs/payment-lifecycle.md)
- Security model: [`docs/security.md`](docs/security.md)
- Machine-readable metadata: [`metadata/project.json`](metadata/project.json)
