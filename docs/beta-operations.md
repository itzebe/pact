# PACT — beta operations

How to run a controlled beta of PACT: the safety posture a beta must hold, the signals
to watch, how to operate the escrow lifecycle, and what to do when something goes wrong.

This document is operational. For the design behind these controls see
[`security.md`](security.md), [`payment-lifecycle.md`](payment-lifecycle.md), and
[`TOPOLOGY.md`](TOPOLOGY.md).

## 1. Safety posture: Sandbox only

A beta runs against **PayPal Sandbox**. No real money moves and no live credential is
used. Two independent mechanisms enforce this:

- `PACT_PAYPAL_SANDBOX_ONLY=true` makes the process refuse to start if it is pointed at
  a live PayPal host, in every environment.
- The sandbox guard in `backend/pact/config.py` fails closed: an unset, misspelled, or
  unknown `PAYPAL_ENVIRONMENT` is treated as **live** and refused, and a deployment that
  both enables guests and points at a live host is refused outright.

Confirm both are in force from the public health endpoint before inviting anyone:

```bash
curl -s https://<deployment>/api/health | jq '.paypal'
# expect: environment "sandbox", is_live false, sandbox_only true, settlement_is_real true
```

`is_live: false` is the authoritative environment indicator. `sandbox_only: true` is the
guard switch (off by default; see [ADR 0007](architecture-decisions/0007-health-reports-is-live.md)).
If `is_live` is ever `true`, stop the beta and treat it as an incident.

## 2. Beta readiness checklist

Before inviting testers, verify every item:

| # | Check | How |
| --- | --- | --- |
| 1 | Health is `ok` and Sandbox-only | `curl -s <deployment>/api/health` → `status: ok`, `paypal.is_live: false`, `paypal.sandbox_only: true` |
| 2 | Durable, shared storage | health → `storage.backend: "postgres"`, `storage.durable: true` |
| 3 | Shared rate limiting | health → `rate_limit.enabled: true`, `scope: "postgres"`, `shared: true` |
| 4 | A real (non-mock) model | health → `llm.is_mock: false` |
| 5 | Guest sessions enabled | health → guest route returns `201` (`curl -X POST <deployment>/api/auth/guest -H 'Content-Type: application/json' -d '{}'`) |
| 6 | Cross-guest isolation | Two guest cookies: guest B reading guest A's job returns `404`; a write without the CSRF token returns `403` |
| 7 | Webhook verification | health → `paypal.webhook_verification: true` |
| 8 | Security headers | `curl -sD - -o /dev/null <deployment>/ \| grep -i x-frame-options` → `DENY` (and CSP, HSTS, etc.) |
| 9 | Suite green | `pytest tests/` reports 619 passed (or 516/613 without a database); `npm test` reports 52 passed |

A failing item is a beta blocker. Do not soften the check to pass it.

## 3. Signals to watch

`GET /api/health` is the single operational signal. It is public and cheap.

| Field | Healthy | Meaning if not |
| --- | --- | --- |
| `status` | `ok` | `degraded` → the durable store could not be opened; **settlement is disabled** (`storage.settlement_disabled: true`) and `storage.error` carries a fixed classification |
| `storage.durable` | `true` | `false` → the ledger is not guaranteed to survive a restart; do not run a beta |
| `rate_limit.shared` | `true` | `false` → the counter is process-local, so a multi-instance deployment under-counts |
| `paypal.is_live` | `false` | `true` → live money; incident |
| `paypal.webhook_verification` | `true` | `false` → webhooks cannot be authenticated; a webhook POST is refused `503` rather than trusted |
| `llm.is_mock` | `false` | `true` → no model key; the deterministic mock verifier runs |

When `status` is `degraded`, money routes fail closed: a job cannot be settled because
the store is unreachable. This is deliberate — a settlement is never recorded against a
store that cannot durably hold it.

## 4. Operating the escrow lifecycle

The lifecycle is `order → approval → authorization → capture → settlement → refund`
(see [`payment-lifecycle.md`](payment-lifecycle.md)). In a beta you mostly observe it
through the guest UI; the API surface is in [`api/README.md`](api/README.md).

Money-moving routes and who may call them:

| Route | Method | Who | Rate limit (default) |
| --- | --- | --- | --- |
| `/api/jobs` | POST | any authenticated principal | 30 / 300 s (`job_create`) |
| `/api/jobs/{id}/run` | POST | owning buyer or admin | 10 / 300 s (`run`) |
| `/api/jobs/{id}/approve` | POST | owning buyer or admin | 30 / 300 s (`approve`) |
| `/api/jobs/{id}/cancel` | POST | owning buyer or admin | 30 / 300 s (`cancel`) |
| `/api/jobs/{id}/refund` | POST | owning buyer or admin | 20 / 300 s (`refund`) |
| `/api/jobs/{id}/reconcile` | POST | **admin only** | 60 / 300 s (`reconcile`) |

Guest-specific bounds (on top of the above):

- **30 new guest workspaces per IP per hour** (`PACT_GUEST_NEW_PER_IP`, window
  `PACT_GUEST_NEW_WINDOW_SECONDS`). The attempt is recorded *before* provisioning, so a
  burst cannot race past the cap; a refusal is `429`.
- **8 jobs per guest workspace** (`PACT_GUEST_MAX_JOBS`). A registered buyer or an
  operator is not bounded here.

Request bodies are capped at 64 KiB of *received* bytes (`MAX_REQUEST_BYTES`), counted
on the ASGI receive channel so a chunked or header-less body cannot bypass the cap.

## 5. Handling an unresolved settlement

A job enters `unresolved` when a provider call's outcome is genuinely unknown — a
capture or refund that may or may not have landed. PACT never guesses:

- An **ambiguous capture** keeps an in-flight marker and blocks settlement, so a blind
  retry cannot capture twice.
- An **ambiguous refund** leaves the job unresolved and records no refund.

Resolution is an **administrative act** because it can settle real money. Use the
`reconcile` route with an admin credential:

```bash
TOKEN=$(cd backend && python -m scripts.issue_token ops-admin admin)
curl -s -X POST https://<deployment>/api/jobs/<job_id>/reconcile \
  -H "Authorization: Bearer $TOKEN"
```

`reconcile` only **reads** PayPal to establish what actually happened (the
authorization is authoritative for a capture), so it is safe to call repeatedly and
never initiates a capture. It returns `409` for a job that is not unresolved. It runs
under the settlement guard, so two instances cannot reconcile the same job at once.

Do **not** retry `run` on an unresolved job hoping it settles — the correct action is
`reconcile`. Do not delete or hand-edit an unresolved job: the ledger is append-only and
a live authorization must never be silently discarded.

## 6. Database operations

Production requires PostgreSQL (see [`deployment.md`](deployment.md)). Tables are created
on first use; migrations are additive and idempotent (`ADD COLUMN IF NOT EXISTS`,
`CREATE INDEX IF NOT EXISTS`), so there is no separate migration step and no down-migration.

**Durability.** With PostgreSQL the ledger is durable across restarts and shared across
instances. The settlement lock needs a *direct* (unpooled) endpoint
(`DATABASE_URL_UNPOOLED`, or a Neon-style host derived automatically); `pool.direct()`
verifies the endpoint with a live probe and refuses one that behaves like a pooler.

**Retention** (see [`database.md`](database.md) → Retention):

- `processed_webhooks`: 30 days, then pruned in bounded batches.
- `consumed_tickets`: the ticket's 60-second lifetime.
- `rate_limit_events`: the largest configured window, floored at one hour.
- Jobs: retained indefinitely, including tombstones, so a live authorization is never
  discarded. Deletion is a soft-delete tombstone set only once the escrow is terminal.

**Backup and restore.** The ledger is the record of truth for money movement, so a beta
must have a working recovery path. PACT does not take its own backups; recovery is a
provider operation.

Production runs on **Neon** (serverless PostgreSQL). Neon's *instant restore* — also
called point-in-time restore (PITR) — is a **built-in capability included on the Free
plan**, not a paid add-on: it retains a continuous history of changes for a **6-hour
window on the Free plan** (7 days on Launch, 30 days on Scale). Within that window you
can restore to any moment without any traditional backup automation. See Neon's
[Backups](https://neon.com/docs/manage/backups) and
[Instant restore](https://neon.com/docs/introduction/point-in-time-restore) documentation.

- **What this covers:** an accidental `DELETE`/`UPDATE`, a bad migration, or data
  corruption within the last 6 hours.
- **What it does not cover:** anything older than the history window, and loss of the
  whole Neon project/account. The 6-hour window is the binding limit of the free plan.
- **Longer retention (optional, still free):** a scheduled `pg_dump` to external object
  storage extends retention beyond 6 hours. Neon documents an S3 + GitHub Actions recipe;
  a free-tier object store works as the target. This is a complement to PITR, not a
  replacement.

**Restoring safely.** Never restore over the production branch. A Neon restore creates a
**new branch** (copy-on-write, so it costs no extra storage until it diverges), which can
be inspected and then promoted or discarded — production is untouched until you choose to
promote. After any restore or promotion:

1. Re-check `/api/health` (`status: ok`, `storage.durable: true`).
2. Reconcile any job that was `unresolved` at the time of the incident (§5).
3. Re-verify the hash chain on the affected jobs.

Verify the exact history window and a working restore on **your** Neon project in the Neon
console before inviting testers. Do not treat the existence of PITR as proof that a restore
succeeds — exercise it against a throwaway branch.

## 7. Incident response

| Symptom | Likely cause | Action |
| --- | --- | --- |
| health `status: degraded`, `storage.settlement_disabled: true` | durable store unreachable | Restore database connectivity. Settlement stays disabled (fail-closed) until it recovers; no action needed in the app. |
| webhook POST returns `503` | signature cannot be verified (no webhook id/secret) | Set the webhook credentials so verification is possible. The refusal is correct — an unverifiable webhook is never trusted. |
| job stuck `unresolved` | ambiguous capture/refund | `reconcile` with an admin token (§5). |
| guest creation returns `429` | per-IP guest cap hit | Expected under a burst; testers can wait for the window or use a registered account. |
| money route returns `429` | per-route limit hit | Expected; the limit is per route and shared across instances. |
| `paypal.is_live: true` | live host configured | Incident: stop the beta, fix `PAYPAL_ENVIRONMENT`/`PACT_PAYPAL_SANDBOX_ONLY`, redeploy. |

Rotating `PACT_AUTH_SECRET` invalidates every outstanding bearer token (clients re-mint);
guest sessions are re-provisioned on the next visit.

## 8. Rollback

Promote a previous Vercel deployment to roll back application code. Because migrations
are additive and the ledger is append-only per job, a rollback needs no down-migration:
an older build ignores columns it does not use, and a job left `unresolved` by an outage
is reconciled forward, never rolled back. See [`deployment.md`](deployment.md) → Rollback
considerations.

## 9. Known limitations (honest)

- **Sandbox only.** No real money is involved; the PayPal Sandbox rail is not a
  production payment relationship.
- **No operator console.** Reconciliation and token minting are CLI/API operations
  (`scripts/issue_token.py`, the `reconcile` route), not a UI.
- **No self-managed backups.** Recovery is delegated to the provider: Neon's built-in
  instant restore (PITR) on the Free plan, with a 6-hour history window (§6). Extending
  retention beyond 6 hours needs a scheduled `pg_dump` you configure.
- **No email delivery guarantee.** The email outbox exists, but delivery depends on an
  operator-configured provider.
- **The AI verifier is assistive.** It produces a verdict and evidence; it never
  authorizes, captures, or refunds (see [`ai-system.md`](ai-system.md)). A model outage
  falls back to the deterministic mock and is reported as `llm.is_mock: true`.

## Related

- Threat model and controls: [`security.md`](security.md)
- Stage-by-stage escrow: [`payment-lifecycle.md`](payment-lifecycle.md)
- Multi-instance safety: [`TOPOLOGY.md`](TOPOLOGY.md)
- Deploy and roll back: [`deployment.md`](deployment.md)
- Local setup and tests: [`development.md`](development.md)
- Status of every capability: [`implementation-status.md`](implementation-status.md)
