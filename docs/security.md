# PACT — security

PACT coordinates money movement, so its security model is the reason it is safe to
expose publicly on a sandbox deployment. This document describes the controls that
are actually implemented, the threat model they address, and how each control is
verified. It does **not** claim an independent audit or certification; none has been
performed.

The machine-readable index of these controls is
[`metadata/security-controls.json`](../metadata/security-controls.json).

## Security principles

1. **Fail closed.** A missing control refuses the request rather than allowing it. A
   missing auth secret refuses every protected request; an unverifiable webhook
   changes nothing; an unreachable shared store refuses rather than degrades.
2. **Derive, do not trust.** Identity comes from a verified token signature, never
   from a request body. Object-level access is checked on every access, not only at
   the route.
3. **Sandbox is not money.** The client is pinned to the PayPal Sandbox host, and a
   guard can hard-refuse a live host. A simulated capture is flagged and never
   reported as real.
4. **Never leak.** Credentials, connection strings, and payer identity are redacted
   from logs and responses.

## Controls

### Authentication

- **Bearer tokens.** HS256 JWTs signed with `PACT_AUTH_SECRET`, minted by
  `scripts/issue_token.py`. The signing algorithm is restricted to an allowlist
  (`HS256`, `HS384`, `HS512`); a misconfigured `PACT_AUTH_ALGORITHM` fails at
  startup and per request rather than being consulted.
- **Fail closed.** With no secret configured, `require_principal` returns `503` for
  every protected route. Production refuses to start with a missing or short secret.
- **Roles.** `buyer`, `seller`, `admin` are distinct and not interchangeable.
  Reconciliation, deletion, and provider-order inspection require `admin`.
- **Money routes require a verified account** when customer auth is enabled;
  an unreachable account store fails those routes closed with `503`.

*Source:* `backend/pact/api/auth.py`, `backend/pact/config.py`
(`validate_runtime`, `ALLOWED_AUTH_ALGORITHMS`).
*Tests:* `tests/test_security.py` (garbage token, wrong-secret token, expired token,
non-bearer scheme, unknown role, unsupported algorithm).

### Authorization (object-level)

- Every access to a job checks that the caller is a party to it
  (`assert_can_access_job`). A non-owner receives `404`, not `403`, so the API does
  not confirm that another user's job exists.
- The `buyer_id` field on `POST /api/jobs` is accepted for compatibility but
  **overwritten** from the token, so a caller cannot post a job as someone else.
- The global ledger is scoped to the caller (a buyer sees their own jobs, a seller
  jobs they are party to, an admin everything).

*Source:* `backend/pact/api/routes.py` (`_job_for`, `_owned_job`, `_visible_jobs`),
`backend/pact/api/auth.py` (`assert_can_access_job`).
*Tests:* `tests/test_security.py` (cross-buyer read/run/delete, list scoping, ledger
scoping, seller scoping, buyer-cannot-reconcile, body `buyer_id` ignored).

### Guest-session isolation

- A guest is an ordinary `buyer` stored under a reserved domain; isolation is
  inherited from the ownership checks, with no second code path to disagree.
- The session cookie is `HttpOnly`, `Secure`, `SameSite=Lax`, scoped to `/api`,
  host-only (no `Domain`), so it cannot leak to a sibling subdomain or be read by
  JavaScript.
- A guest session is **cookie-only**: a guest token presented as a bearer header is
  refused, which closes a path around the CSRF check.
- New workspaces are capped per client IP within a window; each workspace has a job
  quota.

*Source:* `backend/pact/core/guests.py`, `backend/pact/api/auth_routes.py`,
`backend/pact/api/auth.py` (`_principal_from_guest_cookie`).
*Tests:* `tests/test_guests.py`.

### CSRF protection

- A request authenticated from the guest **cookie** must, on a state-changing method
  (`POST`/`PUT`/`PATCH`/`DELETE`), echo the session's CSRF token in `X-CSRF-Token`.
- The token is returned in a JSON body on session start and kept in memory by the
  client (never `localStorage`), so a cross-origin page cannot read it.
- A header-authenticated request is exempt because a browser cannot set an
  `Authorization` header cross-origin at all.

*Source:* `backend/pact/api/auth.py` (`_enforce_csrf`, `CSRF_HEADER`),
`backend/pact/core/guests.py` (`csrf_matches`).
*Tests:* `tests/test_guests.py`.

### Webhook signature verification

- `POST /api/paypal/webhook` is unauthenticated by necessity (PayPal cannot present
  a bearer token); trust comes entirely from the signature.
- The signature is verified through PayPal's own
  `POST /v1/notifications/verify-webhook-signature`, not a local HMAC computation
  (which is easy to get subtly wrong in a way that fails open).
- An event that cannot be verified is refused and changes nothing. Verification is
  unavailable without a `PAYPAL_WEBHOOK_ID` and credentials, so such a deployment
  refuses every event with `503`.

*Source:* `backend/pact/paypal/escrow.py` (`verify_webhook_signature`,
`can_verify_webhooks`), `backend/pact/api/routes.py` (`paypal_webhook`).
*Tests:* `tests/test_webhook_route.py`, `tests/test_webhooks.py`,
`tests/test_webhook_persistence.py`, `tests/test_webhook_postgres.py`.

### Duplicate capture/refund prevention

- The authorization is claimed (`IN_FLIGHT_CAPTURE`) and **persisted before** the
  provider call, so a second attempt sees a capture in progress.
- The whole transition is serialised on the per-job settlement guard; on PostgreSQL
  the guard is a session advisory lock held on a direct connection across instances.
- Refunds are capped at the captured amount (checked before the provider call) and
  idempotent per operation via a persisted `refund_sequence`; a provider refund id
  already recorded is not counted twice.
- The authorization and capture claims are reclaimed only by a conditional
  `UPDATE … WHERE …`, so a reclaim is one statement.

*Source:* `backend/pact/core/settlement.py`, `backend/pact/core/dbstore.py`
(`settlement_claim`), `backend/pact/core/pool.py` (`direct`).
*Tests:* `tests/test_settlement_pooling.py`, `tests/test_refund.py`,
`tests/test_refund_route.py`, `tests/test_settlement_webhook_race.py`.

### Payment-state integrity

- A hold is recorded only for a holdable authorization status; a `COMPLETED` capture
  is the only settlement; a void is confirmed by re-reading the authorization.
- An ambiguous outcome is never retried automatically; the job is `unresolved` and
  must be reconciled by reading PayPal.
- A model verdict is parsed strictly (`as_bool` fails closed), so a malformed
  verdict cannot release funds.

*Source:* `backend/pact/core/settlement.py`, `backend/pact/core/llm.py` (`as_bool`).
*Tests:* `tests/test_state_machine.py`, `tests/test_lifecycle.py`,
`tests/test_verdict_coercion.py`, `tests/test_ambiguous_settlement.py`,
`tests/test_reconciliation_reputation.py`.

### Secret handling and redaction

- `.env` is git-ignored; only `.env.example` with empty values is tracked.
- `/api/health` never exposes the auth secret, the webhook id, or the connection
  string — only fixed, credential-free classifications.
- A raw driver error can embed the connection string; `classify_storage_error` maps
  it to a fixed label and the full error stays in the server log.
- Provider payloads returned to a client are redacted of email local parts.

*Source:* `backend/pact/core/store.py` (`redact_credentials`,
`classify_storage_error`), `backend/pact/api/routes.py` (`_redact`, `health`).

### Input validation

- All request bodies are Pydantic models with explicit bounds (title/brief length,
  criterion count and length, seller count, budget ceiling).
- The request body is capped at 64 KiB, enforced on real received bytes (so a
  chunked body cannot bypass a `Content-Length` check).
- A non-finite budget is a `422`, not a `500`.
- Identifiers are validated against a strict pattern before use.

*Source:* `backend/pact/main.py` (`LimitRequestBodyMiddleware`),
`backend/pact/api/routes.py` (`JobCreate`, `IDENTIFIER`).
*Tests:* `tests/test_security.py` (oversized body, oversized chunked body, too many
criteria, non-finite budget).

### Fail-closed behaviour

- Missing auth secret → `503` on protected routes; production refuses to start.
- Production without `DATABASE_URL` → refuse to start (no non-durable ledger, no
  process-local ticket registry).
- Guest sessions + live PayPal host → refuse to start.
- Mock settlement in production → refuse.
- A configured-but-unreachable shared store → refuse (webhook `503`, ticket refused).
- An unsupported auth algorithm → refuse.

*Source:* `backend/pact/config.py` (`validate_runtime`), `backend/pact/main.py`
(`announce_configuration`).
*Tests:* `tests/test_security.py` (`test_runtime_validation_flags_*`),
`tests/test_deployment_safety.py`.

### Sandbox-only guard

- `PACT_PAYPAL_SANDBOX_ONLY` refuses a configuration that resolves to PayPal's live
  host, in every environment (a developer laptop with live credentials is exactly the
  accident being prevented).
- `PACT_GUEST_SESSIONS` with a live host is refused at startup.
- `PAYPAL_ENVIRONMENT` must be exactly `sandbox` or `live`. Any other value (a typo
  such as `production`, a stray space, an empty string) is refused at startup in every
  environment, because the client only resolves a host for those two values. Without
  this check an unrecognised value would silently talk to the sandbox host while
  `is_live` reported `false` — an invalid configuration presented as a safe one.

The guard is a **switch, not a status**: it is off by default, so
`GET /api/health` reporting `paypal.sandbox_only: false` does **not** mean the process
is pointed at live PayPal. The authoritative indicator is `paypal.is_live`, which is
`true` only when the configuration resolves to PayPal's production host (either
`PAYPAL_ENVIRONMENT=live` or a `PAYPAL_BASE_URL` override that is not a loopback or
sandbox host), and is treated as `true` for any unrecognised `PAYPAL_ENVIRONMENT` so
an ambiguous configuration is never presented as a safe sandbox one. A correct sandbox
deployment reads `environment: "sandbox"`, `is_live: false`, `mode: "sandbox"` and
`sandbox_only: false`.

*Source:* `backend/pact/config.py` (`PayPalSettings.is_live`, `PAYPAL_ENVIRONMENTS`,
`validate_runtime`), `backend/pact/api/routes.py` (`health`).
*Tests:* `tests/test_paypal_sandbox_config.py`
(`test_sandbox_only_false_does_not_mean_live_money`,
`test_the_health_surface_reports_whether_it_resolves_to_a_live_host`,
`test_an_unrecognised_environment_is_not_presented_as_sandbox`,
`test_an_unrecognised_environment_is_refused_at_startup`).

## Threat model

| # | Threat | Attack surface | Mitigation | Verification |
|---|---|---|---|---|
| 1 | Anonymous caller reads/moves money | All `/api/*` money routes | Router-level `require_principal`; fail closed without a secret | `tests/test_security.py` (anonymous refused, `503` without secret) |
| 2 | One buyer reads another's job | `GET /api/jobs/{id}`, list, ledger | Object-level ownership check; `404` not `403` | `tests/test_security.py` (cross-buyer read, list scoping) |
| 3 | Impersonate another buyer | `POST /api/jobs` body | `buyer_id` overwritten from the token | `tests/test_security.py` (`buyer_id` ignored) |
| 4 | A stolen buyer token reconciles/deletes | `reconcile`, `delete`, `paypal-order` | Admin-only routes, separate from buyer | `tests/test_security.py` (buyer cannot reconcile/delete/read order) |
| 5 | Forged webhook moves money | `POST /api/paypal/webhook` | PayPal signature verification; unverifiable → refuse | `tests/test_webhook_route.py` |
| 6 | Replayed webhook double-applies | Repeated event delivery | Event id claimed once in a shared store | `tests/test_webhook_persistence.py`, `test_webhook_postgres.py` |
| 7 | Replayed SSE ticket on another instance | `GET /api/jobs/{id}/stream?ticket=` | Single-use ticket consumed in a shared store | `tests/test_security.py`, `test_ticket_store_postgres.py` |
| 8 | Duplicate capture double-charges | `run` / webhook / retry | In-flight claim persisted before the call; settlement guard (session lock) | `tests/test_settlement_pooling.py`, `test_settlement_webhook_race.py` |
| 9 | Duplicate refund over-refunds | `POST /api/jobs/{id}/refund` | Captured-amount cap; per-operation idempotency; provider refund id dedupe | `tests/test_refund.py`, `test_refund_route.py` |
| 10 | Blind retry after a timeout double-charges | Ambiguous provider outcome | Classified `AmbiguousOutcome`; job `unresolved`; reconcile reads PayPal | `tests/test_ambiguous_settlement.py`, `test_reconciliation_reputation.py` |
| 11 | CSRF drives a money route on a guest | Guest cookie requests | CSRF token on mutating methods; cookie-only guest auth | `tests/test_guests.py` |
| 12 | Guest session drives real money | Guest + live PayPal | Startup refuses guest sessions against a live host | `tests/test_security.py`, `test_deployment_safety.py` |
| 13 | Forged `Host` redirects the buyer | `return_url` construction | `PACT_PUBLIC_URL` wins; else allowed-host allowlist | `backend/pact/api/routes.py` (`_public_base`) |
| 14 | Memory-exhaustion via a huge body | Any POST | 64 KiB cap on received bytes | `tests/test_security.py` |
| 15 | Credential leak in logs/responses | Health, logs, errors | Fixed classifications; redaction helpers | `backend/pact/core/store.py`, `routes.py` |
| 16 | Model output releases funds wrongly | Verifier verdict | Deterministic checks AND model judgement; strict fail-closed parse | `tests/test_verdict_coercion.py` |
| 17 | Unbounded resource use | `run` concurrency | Bounded semaphore; run deadline; `429`/`504` | `tests/test_security.py` (excess runs refused, overrun abandoned) |
| 18 | Non-durable ledger in production | Missing `DATABASE_URL` | Production refuses to start without a database | `tests/test_security.py`, `test_deployment_safety.py` |

## Security testing

The controls above are covered by the backend suite. The dedicated security file is
`backend/tests/test_security.py` (the `test_*` cases referenced throughout this
document), supported by `test_deployment_safety.py`, `test_guests.py`,
`test_webhook_route.py`, `test_refund_route.py`, and the PostgreSQL integration
tests. See [`implementation-status.md`](implementation-status.md) for current
counts.

## What is not claimed

- **No third-party security audit** and no certification (e.g. SOC 2, PCI). None has
  been performed.
- **No real-money deployment has been operated.** The demo is Sandbox; a live
  deployment would need additional operational controls (key management, monitoring,
  incident response) that are out of scope for this repository.
- **No formal verification** of the verifier; it is heuristic by nature.

## Related

- Reporting a vulnerability: [`SECURITY.md`](../SECURITY.md)
- Auth in depth: [`AUTH.md`](AUTH.md)
- Multi-instance safety: [`TOPOLOGY.md`](TOPOLOGY.md)
