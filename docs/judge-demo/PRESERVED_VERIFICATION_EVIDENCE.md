# Preserved verification evidence

This document preserves the evidence trail for PACT's verified end-to-end PayPal
**Sandbox** lifecycle. It separates three things that are easy to conflate:

- **Fresh checks** — performed while preparing this presentation package
  (2026-11-03), against the live production deployment.
- **Historical evidence** — recorded in the earlier verification report and
  reproduced here for reference; it was not re-issued from PayPal during this
  package's preparation.
- **Local automated tests** — run from a clean checkout against real
  PostgreSQL + PgBouncer.

No real-money transaction was performed. No payment transaction was created
merely to refresh evidence.

---

## 1. Baseline (fresh checks)

| Item | Value | How checked |
| --- | --- | --- |
| Repository | `https://github.com/itzebe/pact` | `git remote -v` |
| Branch | `master` | `git branch --show-current` |
| Commit (HEAD) | `46fa3b5e96b25503f101e1b12c63bd77145d97ec` | `git rev-parse HEAD` |
| `origin/master` | `46fa3b5e96b25503f101e1b12c63bd77145d97ec` (equal to HEAD) | `git fetch && git rev-parse origin/master` |
| Production deployment | `dpl_CqWUFB6wnotWgD8MwQGVRf7nwpEY` | Vercel API `GET /v13/deployments/…` |
| Deployment state | `READY`, `target: production` | Vercel API |
| Deployment commit | `46fa3b5e96b25503f101e1b12c63bd77145d97ec` | Vercel API `meta.githubCommitSha` |
| Alias | `pact-flame.vercel.app` → `dpl_CqWUFB6wnotWgD8MwQGVRf7nwpEY` | Vercel API `GET /v4/aliases/pact-flame.vercel.app` |
| PayPal mode | `sandbox` | `GET /api/health` → `paypal.mode` |
| Settlement real | `paypal.settlement_is_real: true` | `GET /api/health` |
| Webhook verification | `paypal.webhook_verification: true` | `GET /api/health` |
| Storage | `postgres`, `durable: true` | `GET /api/health` |
| Guest sessions | available (`POST /api/auth/guest` → `201`, hardened cookie) | live request |

`GET /api/health` (fresh, this session):

```json
{
  "status": "ok",
  "paypal": {
    "configured": true, "environment": "sandbox", "mode": "sandbox",
    "require_real": false, "settlement_is_real": true,
    "webhook_verification": true, "sandbox_only": false
  },
  "environment": "production", "is_production": true,
  "auth": { "configured": true },
  "storage": { "backend": "postgres", "durable": true }
}
```

---

## 2. Payment objects — the verified lifecycle

These are the identifiers PACT recorded from PayPal's own responses during the
verified end-to-end run. They are **historical evidence** from the recorded
verification report: they were not re-issued from PayPal while preparing this
package, and no new transaction was created to refresh them.

| Object | Identifier | Provider status |
| --- | --- | --- |
| PayPal Sandbox order | `60E54463RB1559702` | created (`PAYER_ACTION_REQUIRED`) |
| Authorization | `0N5736387U1180942` | `CREATED` (hold) |
| Capture | `2YX75520SD675715C` | `COMPLETED` (released) |
| Refund | `3UB477660D466340R` | `COMPLETED` |

Verified settlement result (historical): amount **250.00 USD**, escrow `mode=sandbox`,
`simulated=false`, `settlement_is_real=true`, verification `passed` (score `1.0`),
job status `refunded` after the refund above.

Ledger hash-chain verification (historical): `ledger_chain_valid: true`, with the
PayPal-sourced events `escrow_order_created → escrow_awaiting_approval → escrow_held
→ escrow_released → escrow_refunded` all attributed to actor `paypal`.

### Second capture and a void (historical, corroborating the protections)

During the same verification, a second job reached `released` with capture
`0MA99158MK626023P` (150.00 USD) and was refunded in two halves —
`0LE7376643760144H` and `4WX839101A1287017`, both `COMPLETED` — with the refund cap
refusing an over-large request (HTTP `409`) and a third job ending `voided` (hold
released, buyer charged nothing) when verification failed.

> **These identifiers are historical.** PACT did not retrieve them from PayPal
> during the preparation of this package. They are reproduced from the recorded
> verification report, and they remain consistent with the deployed application's
> behaviour, but they are not a fresh provider check.

---

## 3. Fresh production journey during screenshot capture (2026-11-03)

Capturing the production screenshots (see [`screenshots/README.md`](screenshots/README.md))
ran a genuine Sandbox journey through the live deployment: a real order was created,
approved in the PayPal Sandbox checkout, authorized, captured, and refunded. That
run's own provider identifiers are **not reproduced here**, because they were not
captured to a durable record; the identifiers above are the preserved evidence set.

What the capture session did establish, freshly:

- `GET /api/health` reports `paypal.mode: sandbox`, `settlement_is_real: true`
  (checked immediately before the capture run).
- The guest flow reached `awaiting_approval`, then `released`, then `refunded`
  within the deployed application (observed in the browser session).
- The approval step is the real PayPal Sandbox checkout
  (`www.sandbox.paypal.com`), not a simulation.

---

## 4. Local automated test evidence (fresh)

Run from a clean checkout at `46fa3b5e`, against real PostgreSQL 17 + PgBouncer
(transaction mode on `127.0.0.1:6432`), with `DATABASE_URL` and `PGBOUNCER_URL` set
as in CI. With `PGBOUNCER_URL` set, the pooling suite's four transaction-mode tests
run instead of skipping; without it they report `PGBOUNCER_URL not set` and skip.

| Suite | Command | Result |
| --- | --- | --- |
| Backend | `pytest tests/ -q` | **554 passed, 1 skipped** |
| Settlement pooling (PR #20) | `pytest tests/test_settlement_pooling.py -v` | **13 passed** |
| Backend lint | `pyflakes pact/ scripts/` | clean |
| Frontend tests | `npm test` | **40 passed** |
| Frontend build | `npm run build` | success |

The single backend skip is `tests/test_guests.py:166` — *"the deterministic mock did
not settle this run"* — a documented non-determinism of the mock verifier, **not** a
skipped PostgreSQL integration test. The CI job separately asserts that the
PostgreSQL integration tests are not skipped.

`tests/test_settlement_pooling.py` (13 passed) is the PR #20 safety net: it proves
the settlement session lock runs on a **direct** database endpoint, that a
transaction-mode pooler endpoint is refused, that the lock is held across the whole
settlement lifecycle, that a settlement race is exactly-once even when the
application DSN is pooled, and that `statement_timeout` applies without leaking to
other connections.

---

## 5. CI evidence

GitHub Actions workflow **CI** (`.github/workflows/ci.yml`), on the pull requests
that produced the verified commit:

| PR | Title | Run | Result |
| --- | --- | --- | --- |
| #20 | Fix settlement advisory lock lost behind a transaction-mode pooler | [41518229902](https://github.com/itzebe/pact/actions/runs/41518229902) | success |
| #21 | Fully functional public judge experience: zero-friction guest sessions | [41587016474](https://github.com/itzebe/pact/actions/runs/41587016474) | success |
| #22 | Keep the header on one line on narrow screens | [41588221731](https://github.com/itzebe/pact/actions/runs/41588221731) | success |

Merge commits: PR #20 → `f8572f1d…`, PR #21 → `1269be76…`, PR #22 → `46fa3b5e…`
(the deployed commit).

---

## 6. Security verification outcomes (fresh + tests)

| Check | Outcome |
| --- | --- |
| Guest ownership isolation | Cross-guest read → `404`; cross-guest delete/refund → `403` (tests `tests/test_guests.py`; live check in the prior verification) |
| CSRF | A cookie-authenticated mutation without `X-CSRF-Token` → `403` |
| Operator separation | Guest on an admin-only route (`/reconcile`) → `403` |
| Fail-closed live-money guard | With guests enabled and PayPal resolving to a live host, `validate_runtime()` refuses to start (reproduced locally; tests `test_deployment_safety.py`, `test_guests.py`) |
| Sandbox buyer hint | Withheld on a live host and from unauthenticated callers (tests `test_guests.py`) |
| Duplicate capture | Repeat `/approve` on a settled job → `409`; capture id unchanged |
| Refund cap / idempotency | Over-large refund → `409` before the provider call; repeat after a full refund → same refund id (no second movement) |

---

## 7. What is preserved, and where

- **This document** — the provider identifier set, statuses, settlement result,
  refund result, ledger verification, test and CI references, and security outcomes.
- **[`screenshots/`](screenshots/)** — ten genuine production captures, indexed in
  [`screenshots/README.md`](screenshots/README.md), with an OCR-confirmed absence of
  credentials.
- **[`JUDGE_WALKTHROUGH.md`](JUDGE_WALKTHROUGH.md)** — the judge-facing flow and the
  same evidence categories, kept in sync with this file.
- **[`CLEANUP_REPORT.md`](CLEANUP_REPORT.md)** — which verification artifacts could
  be safely removed and which were preserved, and why.

### What is deliberately **not** preserved

- No credential values (Sandbox buyer email/password, tokens, cookies, keys) appear
  in this package.
- No screenshots contain the Sandbox buyer password (redacted before capture; OCR
  verified).
- No fabricated or recreated payment evidence.
