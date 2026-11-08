# Changelog

All notable changes to PACT are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely; entries are
grouped by the pull request that delivered them, newest first, so each change is
traceable to the code and the review that produced it.

PACT has no tagged releases yet. Versions below are the pull-request milestones on
the `master` branch. The latest merge on `master` is **PR #34**
(`fb69db8`, 2026-11-08), which records the PR #33 release; the latest **code release**
is **PR #33** (`41958ab`, 2026-11-08), deployed to production at that commit.

## 2026-11-08 — PR #33: beta-readiness hardening, two release-blocking fixes, and documentation to the shipped state

Deployed to production at `41958ab`; the live alias now serves all eight hardening
headers and the 190 kB code-split initial bundle (measured live, 2026-11-08).

### Fixed — the production build could silently ship React's development bundle

- `frontend/vite.config.js` now forces `NODE_ENV=production` for the `build`
  command. Vite passes the ambient `NODE_ENV` through to esbuild, and React selects
  its development entry point unless `NODE_ENV` is exactly `production`. A build run
  where `NODE_ENV=development` is exported (a common shell setting, and a possible
  CI/Vercel project variable) emitted **411 kB / 116 kB gzip** for the initial chunk
  instead of **190 kB / 59 kB gzip** — ~2.2x the JavaScript, plus React's
  development-only warnings and the DevTools hook — with no error and no failing
  test. The pin makes the artefact identical wherever the build runs. Pinned by
  `frontend/test/bundle-a11y.test.js`.

### Added — regression coverage for the response-hardening and COOP invariants

- `backend/tests/test_security.py` now proves the `413` body-cap rejection and both
  CORS preflight answers (listed origin allowed, unlisted origin refused) carry the
  hardening headers — the outermost middleware must wrap a response produced by an
  inner middleware, not just one produced by a route.
- `frontend/test/coop-paypal.test.js` pins the `Cross-Origin-Opener-Policy:
  same-origin` compatibility invariant: the app has no `window.opener`,
  `postMessage`, or `window.open` dependency and does not load the PayPal JS SDK, so
  COOP cannot break the approval flow (a top-level redirect link with
  `rel="noreferrer"`, with the return read from the `?job=` query). A future popup
  integration fails this test, which is the signal to revisit COOP.

### Added — response hardening headers

- Every response now carries `X-Content-Type-Options: nosniff`, `X-Frame-Options:
  DENY`, a framing/embedding-only `Content-Security-Policy` (`frame-ancestors
  'none'; object-src 'none'; base-uri 'self'`), `Referrer-Policy:
  strict-origin-when-cross-origin`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`
  and `X-Permitted-Cross-Domain-Policies`. An outermost ASGI middleware
  (`SecurityHeadersMiddleware`) attaches them so error responses carry them too; the
  statically-served SPA document gets the same set from `vercel.json` (HSTS is
  already sent by the Vercel edge).
  Closes clickjacking of the guest's approve/refund controls, MIME confusion on
  JSON/error bodies, and the job id leaking in the PayPal `Referer`. See
  `docs/architecture-decisions/0008-application-level-security-headers.md` and
  `tests/test_security.py`.

### Fixed — documentation corrected to the shipped state

- Corrected the stale "latest merge" facts in `CHANGELOG.md`, `docs/evidence/README.md`
  and `metadata/` (PR #32 / `530ced6`; production verified live at that commit,
  including `rate_limit.scope: postgres`). Added the missing PR #31 and PR #32
  changelog entries.
- Recorded that the ledger-grid accessibility limitation was resolved by the AG Grid
  33 → 36 upgrade, and updated the AG Grid version references and the frontend test
  counts (46) and backend counts (512 / 609 / 615) to the freshly measured values.
- Recorded the frontend code-splitting as shipped (initial bundle ~190 kB; AG Grid a
  lazy ~1.26 MB chunk) instead of "planned". See
  `docs/architecture-decisions/0009-code-split-ledger-grid.md`.

### Fixed — an unknown `/api` path was answered with the SPA shell (single-container image)

- In the single-container image the SPA is mounted at `/` and its fallback returned
  `index.html` for any extension-less path, so a mistyped or unknown `/api/...`
  route (and the bare `/api`) was served the HTML shell with a `200` instead of a
  JSON `404`. A client parsing that response as JSON fails confusingly, and the API
  surface silently claims routes that do not exist. `_SPAStaticFiles` now re-raises
  any `404` for a path under `/api` so the API answer stays a JSON `404`. The Vercel
  deployment was already correct (it serves the SPA statically and routes `/api/*`
  to the function); this aligns the image with it. Pinned by
  `tests/test_spa_static.py::test_an_unknown_api_path_is_not_swallowed_by_the_spa_mount`.

### Fixed — the guest refund test was dead coverage, not a flake

- `tests/test_guests.py::test_a_guest_can_refund_its_own_settled_payment` skipped
  deterministically: on a cold reputation ledger every seller scores 0.5, so the
  deliberate bad actor (`seller_quill`) was always the cheapest, always won, and
  every run voided — the refund assertion never executed. The test now seeds a
  settled history for two competent sellers so selection is deterministic and the
  run reaches `released`, and asserts that state instead of skipping. Backend counts
  move accordingly: 516 / 613 / 619 passed (filesystem / PostgreSQL / + pooler),
  with the pooler configuration now skipping nothing.

## 2026-11-08 — PR #32: rate limit the money-bearing routes per identity

### Added — per-identity fixed-window rate limiting on the money routes

- Every route that can authorize, capture, void or refund a payment was
  authenticated and object-authorized, but nothing bounded how *often* one caller
  could reach it. A fixed-window limiter (`backend/pact/core/rate_limit.py`) now
  covers job creation, `run`, `approve`, `cancel`, `refund`, `reconcile` and the
  admin provider-order read. Exceeding it is a `429` with `Retry-After` and
  `X-RateLimit-*` headers.
- The identity is the signed token subject for a registered account, or the client
  address for a guest/anonymous caller. A **guest is keyed on its address, not its
  subject**, so minting fresh sessions from one address cannot reset the budget —
  matching the existing per-address guest-session cap.
- The counter is shared through PostgreSQL when a database is configured, so the
  limit is not spent once per instance; `GET /api/health` reports which
  (`rate_limit.scope`). A configured-but-unreachable store **refuses** rather than
  serving unbounded. Production refuses to start with the guard disabled.
- The shared counter is **serialised per identity**: `check` takes a
  transaction-scoped advisory lock on `(scope, key)` before counting, so two
  instances cannot both read an under-limit count and both record. This closed a
  race CI caught (an 8-way race on a limit of 5 allowed all 8).

*Tests:* `tests/test_rate_limit.py`, `tests/test_rate_limit_postgres.py` (real
PostgreSQL 17), and production-refusal cases in `tests/test_deployment_safety.py`.
*Docs:* `docs/security.md`, `metadata/security-controls.json` (control
`money_route_rate_limiting`).

## 2026-11-08 — PR #31: record the PR #30 release and correct stale release facts

### Changed — documentation

- Recorded the PR #30 release in the changelog and evidence index and corrected
  stale release facts across the docs and metadata.

## 2026-11-07 — PR #30: health reports the resolved PayPal environment (`is_live`)

### Changed — health reports the resolved PayPal environment (`is_live`)

- `GET /api/health` now returns `paypal.is_live`: `true` only when the configuration
  resolves to PayPal's production host. It disambiguates the deployment's real
  environment from `paypal.sandbox_only`, which is the (off-by-default) guard switch;
  `sandbox_only: false` on a safe sandbox deployment could previously be misread as
  "live money". See `docs/architecture-decisions/0007-health-reports-is-live.md`.
- An unrecognised `PAYPAL_ENVIRONMENT` (a typo such as `production`, an empty string)
  is now **refused at startup** in every environment, and `paypal.is_live` reports
  `true` for it, so an ambiguous configuration can never be presented as a safe
  sandbox one. The client only resolves a host for `sandbox`/`live`; previously any
  other value silently resolved to the sandbox host while reporting `is_live: false`.
- Documentation updated to explain the distinction: `docs/api/README.md`,
  `docs/security.md`, `docs/faq.md`, `docs/deployment.md`. Regression coverage added in
  `tests/test_paypal_sandbox_config.py`.

## 2026-11-07 — PR #29: wire the `PACT_PG_*` pool settings into `get_pool`

### Fixed
- `PACT_PG_POOL_MAX_SIZE`/`MIN_SIZE`/`TIMEOUT_SECONDS`/`MAX_IDLE_SECONDS` and
  `PACT_PG_STATEMENT_TIMEOUT_MS` were defined but never passed to the pool.
  `get_pool` now forwards them, an explicit kwarg overrides a setting, and
  `validate_runtime` rejects a value the driver would refuse.

## 2026-11-07 — PR #28: fix psycopg3 prepared-statement race

### Fixed
- A psycopg3 prepared-statement race behind a transaction-mode pooler
  (PgBouncer/Neon `-pooler`), where a named statement prepared on one pooled
  connection was re-used on another.

## 2026-11-07 — PR #27: fix WCAG 2.2 AA defects

### Fixed
- Contrast on `paper.faint` (`#6b7488` → `#9099ab`) and several sub-24 px tap targets.
- Added first-run orientation for the guest experience.

## 2026-11-07 — PR #26: canonical documentation system

### Added — canonical documentation system

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
