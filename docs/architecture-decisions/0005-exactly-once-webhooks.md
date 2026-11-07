# ADR 0005 — Process webhooks exactly once in a shared store

- **Status:** Accepted
- **Date:** 2026-11-02 (PR #16)

## Context

PayPal delivers webhooks **at least once**: the same event can arrive on a retry after
a timeout, on a redelivery after a `5xx`, or twice by accident. A webhook is also the
one place an *unauthenticated* party could try to change money state. Two properties
therefore have to hold: only an event whose signature PayPal itself confirms may be
applied, and an event may be applied **at most once**, across every instance.

A process-local set of seen event ids is correct for one process and wrong for a
serverless deployment: two instances receiving the same redelivery would each see an
empty local set and each apply it.

## Decision

1. Verify the signature through PayPal's own
   `POST /v1/notifications/verify-webhook-signature`, not a local HMAC computation.
2. Claim each event id exactly once in a **shared** store: a `processed_webhooks`
   table with a primary key on `event_id`, claimed with a single
   `INSERT … ON CONFLICT DO NOTHING RETURNING`.
3. A webhook never moves money on its own: it reads the provider, then records what
   the provider says, and only for escrow-relevant event types.
4. Fail closed: a configured-but-unreachable store refuses the event (`503`) rather
   than falling back to a process-local registry.

## Rationale

Server-side signature verification is authoritative and avoids the subtle failure mode
of a local HMAC computation that depends on the exact header set and body bytes PayPal
used — which is easy to get wrong in a way that fails open. A single atomic claim
statement makes exactly-once a property of the database, not of a process.

## Consequences

- **Positive.** A replayed event is a no-op; a forged event is refused; an ambiguous
  capture can be settled from the event's own data plus a confirming read, so no
  second capture is ever issued.
- **Negative.** Webhook processing needs the shared database. In development without a
  database, a process-local registry is used, which is honest only because such a
  deployment is single-instance.
- **Neutral.** Only the event id, type, and job id are persisted — never the raw body,
  which can carry payer identity. Events are retained for 30 days for deduplication,
  then pruned in bounded batches.

## Alternatives considered

- **Local HMAC verification.** Rejected: depends on exact header/body bytes and is easy
  to get wrong in a way that fails open.
- **A process-local seen-set.** Rejected: wrong across instances.
- **Retry-until-success with no dedupe.** Rejected: a redelivery would double-apply.
