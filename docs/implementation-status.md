# PACT — implementation status

An honest, verifiable inventory of what PACT does. Every row names where the feature
lives and the evidence that it works. Status categories are used strictly:

- **Implemented and verified** — the feature exists and is exercised by a test and/or
  a live check recorded in [`evidence/`](evidence/README.md).
- **Implemented but partially verified** — the feature exists; some path is not
  covered by an automated test or a live check.
- **Partially implemented** — the feature works for some inputs but is incomplete.
- **Planned** — not implemented; a roadmap item.
- **Deprecated** — present but superseded.
- **Unknown** — not confirmed.

A passing local test is **not** proof of production deployment, and a successful
deployment is **not** proof that every feature works. Where a status rests on a live
check, it is marked *(live)*.

## Feature inventory

| Feature | Implementation | Status | Evidence |
|---|---|---|---|
| Guest sessions (isolated workspaces) | `core/guests.py`, `api/auth_routes.py` | Implemented and verified | `tests/test_guests.py`; live: `POST /api/auth/guest` provisions a workspace |
| Job creation | `api/routes.py` (`POST /api/jobs`), `core/engine.py` | Implemented and verified | `tests/test_api.py`; live: local run created a job |
| Offers / negotiation | `core/negotiation.py` | Implemented and verified | `tests/test_negotiation_convergence.py`; live ledger shows `offer`/`counter` |
| Budget guardrail | `core/negotiation.py`, `api/routes.py` | Implemented and verified | `tests/test_lifecycle.py`; live: quotes above budget were rejected |
| Payment order creation | `core/settlement.py` (`hold`) | Implemented and verified | `tests/test_paypal_order_route.py`; live: production health reports `mode: sandbox` |
| Buyer approval flow | `api/routes.py` (`approve`), `core/settlement.py` | Implemented and verified | `tests/test_approval_return.py` |
| Payment authorization (hold) | `core/settlement.py` (`authorize_hold`) | Implemented and verified | `tests/test_escrow.py`, `tests/test_state_machine.py` |
| Capture (release / settlement) | `core/settlement.py` (`release`) | Implemented and verified | `tests/test_state_machine.py`, `tests/test_lifecycle.py` |
| Void (return on fail) | `core/settlement.py` (`refund`), `_confirm_void` | Implemented and verified | `tests/test_escrow.py`; live: local run ended `voided` with a confirmed void |
| Refund (reverse a capture) | `core/settlement.py` (`refund_capture`), `api/routes.py` | Implemented and verified | `tests/test_refund.py`, `tests/test_refund_route.py` |
| Duplicate-capture prevention | `core/settlement.py`, `core/dbstore.py` | Implemented and verified | `tests/test_settlement_pooling.py`, `tests/test_settlement_webhook_race.py` |
| Duplicate-refund prevention | `core/settlement.py` | Implemented and verified | `tests/test_refund.py` |
| Reconciliation of ambiguous outcomes | `core/settlement.py` (`reconcile`) | Implemented and verified | `tests/test_ambiguous_settlement.py`, `tests/test_reconciliation_reputation.py` |
| AI negotiation + verification | `core/llm.py`, `core/negotiation.py`, `agents/seller.py`, `core/settlement.py` | Implemented and verified | `tests/test_llm_provider.py`, `tests/test_verdict_coercion.py`; live: production health reports `is_mock: false` |
| Deterministic no-key fallback | `core/llm.py` (`MockLLM`) | Implemented and verified | live: local run completed with `is_mock: true` |
| Hash-chained audit ledger | `core/models.py` (`Job.append`, `verify_chain`) | Implemented and verified | `tests/test_api.py`; live: `chain_valid: true` on a 24-entry run |
| Durable PostgreSQL ledger | `core/dbstore.py`, `core/pool.py` | Implemented and verified | `tests/test_postgres_store.py`; live: production health `storage.backend: postgres`, `durable: true` |
| Webhook signature verification | `paypal/escrow.py`, `api/routes.py` | Implemented and verified | `tests/test_webhook_route.py` |
| Exactly-once webhook processing | `core/webhooks.py`, `core/webhook_store.py` | Implemented and verified | `tests/test_webhook_persistence.py`, `tests/test_webhook_postgres.py` |
| SSE live ledger stream | `api/routes.py` (`stream_job`) | Implemented and verified | `tests/test_api.py` |
| Single-use SSE tickets | `api/auth.py`, `core/ticket_store.py` | Implemented and verified | `tests/test_security.py`, `tests/test_ticket_store_postgres.py` |
| Bearer auth + roles | `api/auth.py` | Implemented and verified | `tests/test_security.py` |
| Object-level authorization | `api/routes.py`, `api/auth.py` | Implemented and verified | `tests/test_security.py` |
| CSRF protection for guests | `api/auth.py`, `core/guests.py` | Implemented and verified | `tests/test_guests.py` |
| Sandbox-only guard | `config.py` (`PayPalSettings.is_live`, `PAYPAL_ENVIRONMENTS`) | Implemented and verified | `tests/test_security.py`, `tests/test_deployment_safety.py`, `tests/test_paypal_sandbox_config.py` (an unrecognised `PAYPAL_ENVIRONMENT` is classified live and refused at startup) |
| Fail-closed startup validation | `config.py`, `main.py` | Implemented and verified | `tests/test_security.py`, `tests/test_deployment_safety.py` |
| Request body cap | `main.py` (`LimitRequestBodyMiddleware`) | Implemented and verified | `tests/test_security.py` |
| Response hardening headers | `main.py` (`SecurityHeadersMiddleware`), `vercel.json` | Implemented and verified | `tests/test_security.py` (nosniff, `X-Frame-Options`/`frame-ancestors`, `Referrer-Policy`, `Permissions-Policy`, COOP on 200/401/404) |
| Bounded run concurrency | `core/engine.py`, `api/routes.py` | Implemented and verified | `tests/test_security.py` |
| Money-route rate limiting | `core/rate_limit.py`, `api/routes.py` (`_enforce_rate_limit`) | Implemented and verified | `tests/test_rate_limit.py`, `tests/test_rate_limit_postgres.py`, `tests/test_deployment_safety.py` (shared counter, guest keyed on address, fails closed, production refuses disable) |
| Reputation + market selection | `core/store.py`, `core/dbstore.py`, `core/negotiation.py` | Implemented and verified | `tests/test_reconciliation_reputation.py` |
| Frontend UI (guest-first) | `frontend/src/` | Implemented and verified | `frontend/test/*` (52 tests); production app returns 200; axe-core reports 0 violations on the home and ledger views at 390/768/1440px |
| Frontend code-splitting | `frontend/src/components/LedgerPanel.jsx`, `frontend/src/App.jsx` | Implemented and verified (on PR #33; not yet deployed) | `frontend/test/bundle-a11y.test.js`; the initial bundle is ~190 kB (AG Grid is loaded lazily on the Ledger tab, its own ~1.26 MB chunk). Production at `530ced6` still serves the pre-split ~1.14 MB initial chunk |
| Deterministic production build | `frontend/vite.config.js` | Implemented and verified | `frontend/test/bundle-a11y.test.js`; the build pins `NODE_ENV=production`, so an ambient `NODE_ENV=development` can no longer ship React's development bundle (411 kB vs 190 kB). See `docs/architecture-decisions/0010-pin-node-env-for-build.md` |
| Frontend payment-state honesty | `frontend/src/lib/status.js` | Implemented and verified | `frontend/test/payment-status.test.js` |
| Customer accounts + email outbox | `core/accounts.py`, `core/auth_store.py`, `core/email.py` | Implemented but partially verified | `tests/test_accounts.py`, `tests/test_auth_routes.py`, `tests/test_email_dispatcher.py`; the end-to-end email path needs a provider and is not exercised live |
| Pooled-endpoint settlement routing | `core/pool.py`, `core/dbstore.py` | Implemented but partially verified | `tests/test_settlement_pooling.py` runs only when `PGBOUNCER_URL` is set (CI provides it; a bare local run skips 5 tests) |
| Configurable pool limits | `pact/config.py`, `core/pool.py` (`get_pool`) | Implemented and verified | `tests/test_pool_config.py`: `get_pool` forwards `PACT_PG_POOL_MAX_SIZE`/`MIN_SIZE`/`TIMEOUT_SECONDS`/`MAX_IDLE_SECONDS`/`PACT_PG_STATEMENT_TIMEOUT_MS` to the pool, an explicit kwarg overrides a setting, and `validate_runtime` rejects a value the driver would refuse (previously these settings were defined but never passed) |

## Not implemented (planned / out of scope)

These are **not** shipped. They are recorded so the boundary is explicit.

| Idea | Status |
|---|---|
| A real-money deployment path with operational controls | Planned (out of scope; the sandbox-only guard would need explicit operator intent) |
| Pluggable verifiers (e.g. running tests against a code deliverable) | Planned |
| Additional settlement rails behind the same escrow interface | Planned |
| An independent security audit / certification | Not done |

> The ledger grid accessibility limitation recorded here previously (AG Grid 33
> emitting a `tabindex="-1"` on a `role="presentation"` header viewport, flagged by
> axe as `aria-required-children`) was **resolved in the beta-readiness pass** by
> upgrading to AG Grid 36, whose DOM omits it. See the feature inventory below and
> `frontend/test/bundle-a11y.test.js`.

## Verification summary (this repository)

Recorded in [`evidence/README.md`](evidence/README.md) and
[`metadata/evidence.json`](../metadata/evidence.json):

- Backend (filesystem store): **516 passed, 103 skipped**.
- Backend (PostgreSQL 17): **613 passed, 6 skipped**.
- Backend (PostgreSQL + transaction-mode pooler): **619 passed, 0 skipped**.
- Frontend: **52 passed**; production build succeeds.
- `pyflakes`: clean.
- Dependencies: `pip-audit` and `npm audit` report **0 known vulnerabilities**.
- Production `/api/health`: `status: ok`, `paypal.environment: sandbox`,
  `paypal.is_live: false`, `paypal.mode: sandbox`, `llm.is_mock: false`,
  `storage: {backend: postgres, durable: true}`, `rate_limit: {enabled: true,
  scope: postgres}` *(live, deployed commit `530ced6`)*.

## Related

- What each status means in detail: [`evidence/README.md`](evidence/README.md)
- The product story: [`overview.md`](overview.md)
- Machine-readable inventory: [`metadata/features.json`](../metadata/features.json)
