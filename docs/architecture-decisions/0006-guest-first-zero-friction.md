# ADR 0006 — Guest-first onboarding as the default public experience

- **Status:** Accepted
- **Date:** 2026-11-03 (PR #21)

## Context

PACT holds and moves money, so its API refuses anonymous callers: every money route
requires a bearer credential. That is correct, but it makes the product hard to
*evaluate* — a judge or a curious visitor would have to mint a token before they could
see anything. A demo that requires setup before it shows anything is a demo most
people will not see.

## Decision

Make **guest sessions** the default public experience. On first visit, the server
provisions a real, isolated buyer workspace and sets an `HttpOnly`, `Secure`,
`SameSite=Lax` session cookie scoped to `/api`, so the product is the first screen and
there is nothing to register, paste, or verify.

Guardrails, because this exposes a money rail publicly:

- A guest is an ordinary `buyer` principal under a reserved, non-deliverable domain,
  so every existing ownership check already isolates it. There is no second code path
  that could disagree.
- A guest is never an `admin` and cannot reach operator routes.
- The session is cookie-only; a guest token presented as a bearer header is refused,
  which closes a path around the CSRF check.
- Mutating requests must echo a CSRF token returned on session start.
- New workspaces are capped per client IP within a window; each workspace has a job
  quota.
- Startup **refuses to run when guest sessions are enabled and PayPal resolves to a
  live host**: anonymous guests may drive sandbox money, never real money.

## Rationale

Isolation is *inherited* rather than bolted on: a guest is the same kind of principal
as any buyer, so the ownership checks that already exist are the isolation mechanism.
This is what makes the feature safe to expose, and it is why the guest path is not a
parallel implementation.

## Consequences

- **Positive.** The product is usable with zero friction; the demo is the first
  screen; the guest path exercises the real engine and the real PayPal client, so what
  a visitor sees is the actual product.
- **Negative.** A public deployment exposes a real (sandbox) payment rail to anonymous
  traffic; the rate limits and the sandbox-only guard bound that exposure.
- **Neutral.** The feature is **off by default** (`PACT_GUEST_SESSIONS` unset →
  `POST /api/auth/guest` returns `404`), so a deployment that does not want anonymous
  traffic does not serve it.

## Alternatives considered

- **Require a token for the demo.** Rejected: most visitors would not see the product.
- **A shared demo account.** Rejected: not isolated; one visitor's records would be
  visible to another.
- **A seeded, read-only demo.** Rejected: it would not exercise the real lifecycle and
  could drift from the product.
