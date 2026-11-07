# ADR 0003 — Fail closed on unsafe configuration

- **Status:** Accepted
- **Date:** 2026-10 to 2026-11 (initial build and PR #14, #15)

## Context

PACT holds and moves money. The dangerous failure mode is not a crash — it is a
deployment that *appears* to work while silently doing the wrong thing: serving
requests with no authentication, reporting a simulated settlement as a real payment,
or losing the ledger on a cold start. Each of these would be worse than an outage,
because it would be invisible.

## Decision

Refuse to start, or refuse a request, when a required control is missing — rather
than degrade to an unsafe default. Concretely:

- No `PACT_AUTH_SECRET` → every protected request is `503`; production refuses to
  start.
- Production without `DATABASE_URL` → refuse to start (no non-durable ledger, no
  process-local ticket registry).
- Production with no PayPal credentials and the mock enabled → refuse.
- Guest sessions enabled with a live PayPal host → refuse.
- An unsupported signing algorithm → refuse.
- A configured-but-unreachable shared store → refuse (webhook `503`, tickets refused).
- An unverifiable webhook → refuse and change nothing.

## Rationale

A missing control means "not configured", not "open". Failing closed turns a silent,
dangerous misconfiguration into a loud, immediate one that an operator must fix. It
also makes the safe path the default path, so a hurried deployment cannot accidentally
serve money routes without authentication.

## Consequences

- **Positive.** The dangerous failure modes are impossible to reach by accident. The
  deployment either works correctly or refuses to run.
- **Negative.** A misconfigured deployment is unusable rather than partially usable.
  This is intentional; the fix is to configure it, and the error message says which
  setting is missing.
- **Neutral.** `validate_runtime()` centralises the checks, so they are visible in one
  place and covered by tests (`tests/test_security.py`,
  `tests/test_deployment_safety.py`).

## Alternatives considered

- **Degrade gracefully (warn and continue).** Rejected: a warning is easy to miss and
  the consequence is a silent wrong behaviour (unauthenticated money routes, a fake
  settlement). Not acceptable for a money system.
