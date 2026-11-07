# ADR 0004 — Session advisory lock on a direct endpoint

- **Status:** Accepted
- **Date:** 2026-11-02 (PRs #18, #20)

## Context

PACT runs on Vercel, where a serverless platform starts many function instances. Two
instances could try to capture the same PayPal authorization at the same time, which
would double-charge the buyer. In-process locking (`threading.RLock`) only protects
one process, so the settlement claim must be serialised **across instances**.

The application database is Neon, whose `DATABASE_URL` is a **pooled** endpoint
(PgBouncer in transaction mode). That is correct for ordinary request/response
queries, but wrong for a session-scoped lock: a transaction-mode pooler hands the
server connection back after every transaction, so a session lock taken through it is
released between statements and silently stops protecting the settlement.

Two prior incidents made this concrete: a production outage from sending
`statement_timeout` as a libpq startup parameter (which a managed pooler rejects;
PR #18), and the discovery that the settlement lock was being taken through the
pooler (PR #20).

## Decision

On PostgreSQL, serialise the settlement transition with a **session advisory lock**
taken on a **direct (unpooled) connection**:

- The lock key is a stable SHA-256-derived 64-bit value of the job id (never the
  salted builtin `hash`, which would differ per process).
- The connection comes from `DatabasePool.direct`, which uses
  `DATABASE_URL_UNPOOLED` when set, or a Neon-style direct host derived from the
  pooled `DATABASE_URL` (the pooled host with `-pooler` removed).
- `direct()` verifies the endpoint with a live probe and refuses one that behaves like
  a pooler.
- `statement_timeout` is applied with `SET LOCAL` inside a transaction per checkout on
  the pooled path, and as a session `SET` on the direct path.

## Rationale

A session lock held on a genuine, dedicated session is released only when the session
ends, so it holds across the external PayPal call and across instances. Re-reading the
authoritative row inside the lock means the transition is decided on current state,
not stale state.

## Consequences

- **Positive.** Two instances cannot both capture the same authorization. The guard
  cannot silently degrade, because a wrong endpoint is refused rather than used.
- **Negative.** The settlement path needs a direct database endpoint, which some
  providers expose only separately; the code derives it where possible and documents
  the fallback.
- **Neutral.** The filesystem store keeps a `threading.RLock`, which is correct only
  for a single instance — which is exactly why production requires PostgreSQL.

## Alternatives considered

- **Pooled connection for the lock.** Rejected: a transaction-mode pooler releases a
  session lock between statements.
- **A database row as a lock (`SELECT … FOR UPDATE`).** Workable, but the advisory
  lock is simpler and releases automatically when the session ends, which is safer for
  a crashed worker.
- **An external lock service.** Rejected as an unnecessary dependency for a guarantee
  the database can provide.
