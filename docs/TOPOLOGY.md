# Production topology: can PACT run more than one API instance?

This document answers one question: **if the API runs as more than one instance
behind a load balancer, are the security and money guarantees still true?** It
was written by inspecting the deployment configuration in this repository and the
live deployment, not from assumption. Every claim below names the file or the
command that establishes it.

## Verified environment (2026-10-31)

The claims below were re-verified in this session against a **real PostgreSQL 17**
instance (Debian 17.11) and a real **Docker** daemon, both started inside the
workspace — not assumed from notes:

- `docker info` succeeded only via `sudo`; the daemon was started with
  `sudo dockerd`. The production `Dockerfile` built (`docker build -t pact:verify .`)
  and the image started and answered `/api/health`.
- PostgreSQL was installed (`postgresql-17`; the CI service container pins 16, and
  the SQL used here is common to both) and the full suite ran against it.

An earlier note that "Docker is unavailable" was stale; it is available and the
image builds. The production platform remains Vercel (serverless, many instances),
for which the multi-instance guarantees below must hold.

## What actually runs

| Target | How it runs | Instances |
| --- | --- | --- |
| **Vercel (production)** | `api/index.py` is an ASGI `app` served by Vercel's Python runtime; `vercel.json` sets memory/timeout and rewrites `/api/*` to it | **Many.** A serverless platform starts a fresh function instance per concurrent request and per cold start. This is the target the multi-instance guarantees must hold for. |
| **Docker** | `Dockerfile` builds `python:3.11-slim` + `uvicorn pact.main:app` | One process per container, but a container is trivially scaled horizontally. |
| **Render** | `render.yaml` runs the Dockerfile as one web service with a 1 GB disk mounted at `/data` | One instance by configuration, not by design. |

The production deployment is Vercel, and `pact-flame.vercel.app/api/health` reports
`"storage":{"backend":"postgres","durable":true}`, so the live deployment is already
on the multi-instance-safe path.

## Is shared PostgreSQL available to all instances?

**Yes, and it is required.** Every instance reads one `DATABASE_URL` and connects to
the same database. This is not optional in production: the application fails its
startup validation if it is production and no shared database is configured, which
is what PR #15 ("Require PostgreSQL in production") added. The consequence is that
the job records, the reputation ledger, the consumed-ticket registry and the
webhook-event registry all live in one place every instance can see.

`settings.database_url` is the single switch. With it set, `pact.core.ticket_store`,
`pact.core.webhook_store` and `pact.core.store` each select their PostgreSQL
implementation:

```
ticket_store:  postgres  shared: True
webhook_store: postgres  shared: True
job_store:     postgres  durable: True
```

Without it (local development, a single container with a mounted volume) they fall
back to process-local implementations. That is honest only because such a deployment
*is* single-instance; the production check is what stops the fallback from ever
being used where it would be wrong.

The **customer-authentication store** (`pact.core.auth_store`) follows the same
switch: `PostgresAuthStore` when `DATABASE_URL` is set, `InMemoryAuthStore`
otherwise. The tables it owns — `users`, `email_verifications`,
`password_reset_tokens`, `refresh_tokens`, `login_attempts`, `email_outbox`,
`auth_events` — live in the shared database, so an account suspended on one instance
is refused on every instance, and a refresh token rotated by one instance is spent
everywhere.

## Connection pooling

Every PostgreSQL store draws connections from **one bounded pool per instance**
(`pact.core.pool`, keyed by DSN so the stores share it). The default cap is 5
connections (`PACT_PG_POOL_MAX_SIZE`), recycled after 300 s idle, with a 10 s
server-side `statement_timeout`. This bounds each instance's footprint; it is **not**
a global connection limit — the worst case across the fleet is
`active instances × max_size`, which the operator must budget against the database
plan.

One deliberate exception: `PostgresJobStore.settlement_claim` holds a **session**
advisory lock for the whole protected operation, so it uses a dedicated,
non-pooled connection that is closed (not returned) on exit. Returning a
session-locked connection to the pool would leak the lock into an unrelated request.
For the same reason a transaction-mode pooler in front of the database is unsafe
here — it would release the session lock between transactions and silently stop
protecting the settlement.

## Is ticket consumption atomic across instances?

**Yes.** A stream ticket is single-use because its `jti` is consumed against a shared
row:

```sql
INSERT INTO consumed_tickets (jti, expires_at)
VALUES (%s, %s)
ON CONFLICT (jti) DO UPDATE
    SET expires_at = EXCLUDED.expires_at
    WHERE consumed_tickets.expires_at <= now()
RETURNING jti
```

This is one statement. A brand-new id inserts and returns a row. A second attempt for
the *same, still-live* id hits the conflict, the `WHERE` makes the update a no-op, and
`RETURNING` yields nothing — so exactly one caller is admitted. Two instances racing
on the same new id serialise on the primary key; the loser re-evaluates the conflict
after the winner commits, sees a future `expires_at`, and is refused.

`pact/tests/test_ticket_store_postgres.py` proves this with four **separate OS
processes** racing for one ticket — not threads, not an in-memory double — and
asserts exactly one `WON`. The ticket lifetime is unchanged at 60 seconds
(`STREAM_TICKET_TTL`), and the consumed row is stored with that same expiry, so it
can never outlive the token it guards.

**Fail closed.** If `DATABASE_URL` is set but the database is unreachable,
`_select_ticket_store` returns an `UnavailableTicketStore` whose `consume` always
returns `False`. Every stream ticket is refused until the database is reachable.
There is deliberately no fallback to the process-local registry, because a replay
landing on another instance would then be accepted — the exact hole this closes. No
ticket value is ever logged; only the exception class name is.

## Does any process-local security state remain?

Nothing whose *decision* is security- or money-bearing is process-local. The
remaining in-process state is one of three kinds, none of which is load-bearing when
a shared store is present:

1. **Per-process caches and handles.** The module-level singletons
   (`ticket_store`, `webhook_store`, `job_store`) are one object per process, but the
   state they decide against is in PostgreSQL. The filesystem `JobStore` /
   `ReputationLedger` in-memory dicts exist only on the non-Postgres path.
2. **A thread lock that is always backed by a database lock.**
   `SettlementEngine._locks` is a per-job `threading.RLock`. A thread lock cannot
   guard across instances, so it is never the guard that matters: when the store can
   take a shared claim (`supports_claims`, true for the PostgreSQL store) the
   transition also runs inside `store.settlement_claim(job)`, a **PostgreSQL session
   advisory lock** that re-reads the authoritative row. The refund path added in this
   change reuses that same guard, so a refund and a capture on the same job — in any
   two instances — cannot both proceed.
3. **The single-instance fallbacks.** `InMemoryTicketStore`,
   `InMemoryWebhookStore` and `InMemoryAuthStore` are process-local *by design*, and
   are only ever selected when there is no `DATABASE_URL`, i.e. when the deployment
   is single-instance. A configured-but-unreachable database produces the refusing
   `UnavailableTicketStore`/`UnavailableAuthStore`, never the in-memory one. For the
   auth store specifically, an unreachable database makes the sensitive-operation
   gate return `503` (fail closed) rather than admitting an unverified caller.

There is no process-local *security decision* left. The only remaining process-local
state is a per-instance connection pool (bounded, described above), which holds no
authoritative data — every decision is made by a statement against the shared
database.

## Is worker recovery still safe across instances?

**Yes.** Recovery is expressed as conditional `UPDATE`s against the shared row, so it
is decided by the database, not by a local view:

- **Run claiming** — `claim_run` moves a job out of a runnable state with one
  conditional `UPDATE` that also sets a lease (`run_lease_expires_at`). Two instances
  cannot both claim the same job. A crashed worker's lease expires and the job becomes
  reclaimable; `awaiting_approval` is deliberately excluded from the reclaimable set
  because it is a paused state waiting on the buyer, not a dead worker.
- **Approval claiming** — `claim_approval` is keyed off the *escrow* state with a
  TTL, so a worker that dies between claiming and returning does not lock the buyer
  out, and two instances cannot both drive the approval transition.
- **Settlement / refund** — the PostgreSQL advisory lock plus a re-read of the
  authoritative row, as above.

The tests that exercise this (`tests/test_postgres_store.py`) run the race in four
real OS processes and assert the provider is reached exactly once. A negative-control
test proves the advisory lock is load-bearing by removing it and watching the race
fail.

## Summary

| Question | Answer |
| --- | --- |
| Shared PostgreSQL for all instances? | Yes — required in production |
| Ticket consumption atomic across instances? | Yes — one `INSERT … ON CONFLICT … RETURNING`, proven across OS processes |
| Remaining process-local security state? | None that is load-bearing; in-process locks are always backed by a database lock, and the process-local stores are single-instance-only |
| Worker recovery safe across instances? | Yes — conditional `UPDATE`s + lease + advisory lock |
| Behaviour if the shared store is down? | Fail closed: tickets and unclaimable webhooks are refused, never silently accepted locally |
