# Changelog

All notable changes to PACT are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely; entries are
grouped by the pull request that delivered them, newest first, so each change is
traceable to the code and the review that produced it.

PACT has no tagged releases yet. Versions below are the pull-request milestones on
the `master` branch. The latest merge on `master` is **PR #25**
(`4ce1faf`, 2026-11-03).

## [Unreleased]

### Added — canonical documentation system (this change)

- Root `AGENTS.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, `llms.txt`,
  `llms-full.txt`.
- A `docs/` set: overview, architecture, payment-lifecycle, database, security,
  ai-system, deployment, development, faq, glossary, knowledge-map,
  implementation-status, and an `api/` reference.
- A `metadata/` directory of machine-readable JSON with source references.
- `docs/evidence/` and `docs/architecture-decisions/`.
- GitHub Actions documentation checks (`.github/workflows/docs.yml`).

Documentation only; no application behaviour changed.

## 2026-11-03 — PR #25: final narrated demo video

### Added
- The final narrated walkthrough (`docs/judge-demo/pact-demo-final-narrated.mp4`)
  and its build scripts.

## 2026-11-03 — PR #24: hackathon submission package

### Added
- `docs/HACKATHON_SUBMISSION.md` — field-by-field Devpost submission copy.

## 2026-11-03 — PR #23: judge-facing walkthrough and preserved evidence

### Added
- A judge walkthrough, production screenshots, and
  `docs/judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md`.

## 2026-11-03 — PR #21: zero-friction guest sessions

### Added
- **Guest (judge) sessions.** A first visit provisions a real, isolated buyer
  workspace and sets an `HttpOnly; Secure; SameSite=Lax` session cookie scoped to
  `/api`. Mutating requests must echo a CSRF token. Guests are ordinary buyers —
  never admins — so every ownership check already isolates them. New workspaces are
  capped per client IP and each workspace has a job quota.
- Startup **refuses to run when guest sessions are enabled and PayPal resolves to a
  live host**: anonymous guests may drive sandbox money, never real money.

### Fixed
- PR #22: keep the header on one line on narrow screens.

## 2026-11-02 — PR #20: settlement lock on a direct endpoint

### Fixed
- The settlement advisory lock was being taken through a transaction-mode pooler,
  which releases a session lock between statements. The lock now runs on a *direct*
  (unpooled) endpoint, derived from a Neon-style `-pooler` host or set explicitly as
  `DATABASE_URL_UNPOOLED`, and verified with a live probe before use.

## 2026-11-02 — PR #18/#19: statement_timeout as a transaction-local SET

### Fixed
- A production outage caused by sending `statement_timeout` as a libpq startup
  parameter, which a managed pooler rejects. It is now applied with `SET LOCAL`
  inside a transaction per checkout (`SET` on the direct session).

## 2026-11-02 — PR #17: customer authentication and shared pooling

### Added
- An opt-in customer-authentication subsystem: Argon2id passwords, 15-minute access
  tokens, single-use rotating refresh tokens (reuse revokes the family), a
  `token_version` for coarse revocation, and a crash-safe transactional-email outbox.
- A bounded per-instance PostgreSQL connection pool.
- Money-bearing routes require a verified, active account; an unreachable account
  store fails those routes closed with `503`.

## 2026-11-02 — PR #16: webhooks, refund API, recovery

### Added
- **Verified PayPal webhook ingestion** (`POST /api/paypal/webhook`): signature
  verified through PayPal's own verify endpoint, event ids claimed exactly once in a
  shared store, only escrow-relevant event types acted on.
- **Refund API** (`POST /api/jobs/{id}/refund`): reverse a captured payment in full
  or in part, capped at the captured amount, idempotent per operation.
- A read-only PayPal order inspection route (`GET /api/jobs/{id}/paypal-order`,
  admin, redacted).
- An honest frontend payment panel with bounded polling.

### Fixed
- Close a crash-window refund double-count; do not double-count a refund PACT's API
  already recorded.
- Fix webhook capture/refund not persisting on the PostgreSQL path.

## 2026-11-01 — PR #15: require PostgreSQL in production

### Changed
- Production startup now refuses to run without `DATABASE_URL`, because a
  multi-instance deployment would otherwise fall back to a process-local ticket
  registry and a non-durable local ledger.

## 2026-11-01 — PR #14: pre-launch security hardening

### Added
- Authentication on every money route (bearer tokens, HS256), bounded run
  concurrency, honest settlement reporting, and scale fixes.
- CI: backend with PostgreSQL 16 and PgBouncer, frontend, and a Docker image job.

### Fixed
- Five pre-launch security/reliability findings from review.
- Share SSE ticket consumption through PostgreSQL so a captured ticket cannot be
  replayed against another instance.

## 2026-10-26 — PR #11/#12/#13: hackathon submission, demo video

### Added
- Demo video cuts and the evidence segment, plus the initial submission docs.

## 2026-10-25 — PR #1–#10: initial build and early hardening

### Added
- The core product: FastAPI backend, React/Vite frontend, hand-written PayPal
  `Orders v2` escrow client, negotiation engine, verifier, hash-chained ledger,
  reputation, and a Vercel deployment with durable, instance-safe persistence.
- A read-only PayPal order inspection route (#4).

### Fixed
- Fail closed when a model verdict is malformed — a string boolean such as
  `"passed": "false"` no longer releases funds (#3).
- Send `payment_source` at the order level so buyer approval works (#5).
- Stop an order body being parsed as an authorization or capture (#7).
- Send the buyer back from PayPal, and let a retry after approval succeed (#8).
- Close on an affordable quote when the model never emits an acceptance (#9).
- Stop showing a retryable buyer-approval state as a failed settlement (#10).
- Correct four settlement and durability defects (Postgres reputation loss, phantom
  seller penalty, wedged jobs, broken fresh-database tests) (#2).

## 2026-10-23 — initial commit

- `76f2c3b` — "Pact: settlement infrastructure for the agent economy on PayPal
  escrow".

[Unreleased]: https://github.com/itzebe/pact/compare/master...HEAD
