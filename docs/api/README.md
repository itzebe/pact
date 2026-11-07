# PACT — API reference

The PACT HTTP API is served by FastAPI. This reference documents only endpoints that
are actually implemented in `backend/pact/api/routes.py` and
`backend/pact/api/auth_routes.py`. Interactive OpenAPI docs are available at
`/docs` on any running instance.

- **Base URL (production):** `https://pact-flame.vercel.app`
- **Content type:** `application/json`
- **Auth:** `Authorization: Bearer <token>`, or a guest session cookie (see below)

## Authentication

Two credential forms exist; a request uses whichever it has:

1. **Bearer token.** An HS256 JWT minted by `python -m scripts.issue_token <subject>
   <buyer|seller|admin>`, sent as `Authorization: Bearer <token>`.
2. **Guest session cookie.** Set by `POST /api/auth/guest`. A mutating request from
   the cookie must also send `X-CSRF-Token` with the token returned on session start.

Without a credential, every route except the public ones returns `401`. With no
`PACT_AUTH_SECRET` configured on the server, protected routes return `503`.

**Roles.** `buyer` (posts and drives jobs), `seller` (sees jobs it is party to),
`admin` (reconcile, delete, provider order). A buyer is scoped to its own jobs; a
non-owner gets `404`, not `403`.

## Error format

Errors are `{"detail": <string | object>}`. Common codes:

| Status | Meaning |
|---|---|
| `400` | Malformed input (e.g. a malformed webhook body, an unknown seller) |
| `401` | Missing/invalid credential, or a failed webhook signature |
| `403` | Authenticated but not permitted (e.g. a missing/invalid CSRF token) |
| `404` | Not found, or not visible to this caller |
| `409` | Conflicting state (job already running, nothing to refund, wrong state) |
| `413` | Request body exceeds the 64 KiB cap |
| `422` | Validation error (field bounds, unknown seller list, too many sellers) |
| `429` | Too many runs in flight, or a guest quota/rate limit |
| `502` | A provider (PayPal) error or an ambiguous outcome |
| `503` | Server not configured (no auth secret), or a shared store unavailable |
| `504` | The run exceeded its internal deadline |

## Rate limiting

- **Run concurrency:** bounded by `PACT_MAX_CONCURRENT_RUNS`; beyond it, `run`
  returns `429` with `Retry-After: 2`.
- **Guest sessions:** new workspaces are capped per client IP within a window
  (`429`); each workspace has a job quota (`429` on job creation).

---

## Health

### `GET /api/health`

Public. Configuration and settlement mode. Exposes no secret.

**Response 200**

```json
{
  "status": "ok",
  "paypal": {
    "configured": true,
    "environment": "sandbox",
    "mode": "sandbox",
    "require_real": false,
    "settlement_is_real": true,
    "webhook_verification": true,
    "sandbox_only": false
  },
  "llm": { "configured": true, "model": "openai/gpt-oss-20b @ https://api.groq.com/openai/v1", "is_mock": false },
  "environment": "production",
  "is_production": true,
  "auth": { "configured": true },
  "currency": "USD",
  "sellers": ["seller_atlas", "seller_nova", "seller_quill"],
  "storage": { "backend": "postgres", "durable": true }
}
```

`status` is `degraded` when the configured durable store could not be opened (then
`storage.settlement_disabled` is `true` and `storage.error` is a fixed
classification).

---

## Webhook

### `POST /api/paypal/webhook`

Public (PayPal cannot present a bearer token). Trust comes from the signature.

**Request:** the raw PayPal event JSON.
**Response:** `{"status": "ok", "outcome": "<applied|duplicate|ignored|unknown_job>"}`.

| Condition | Status |
|---|---|
| Applied / duplicate / ignored / unknown job | `200` |
| Malformed body | `400` |
| Signature verification unavailable, or the shared store is down | `503` |
| Signature verification failed | `401` |

---

## Guest sessions

### `POST /api/auth/guest`

Public. Provisions a guest workspace and sets the session cookie. `404` when guest
sessions are disabled on the deployment. Rate-limited per client IP.

**Request body (optional):** `{}` or `{"display_name": "…"}`.
**Response 201**

```json
{
  "type": "guest",
  "subject": "gst_…",
  "role": "buyer",
  "csrf_token": "…",
  "expires_in": 43200,
  "sandbox_buyer": { "email": "…", "password": "…", "note": "PayPal Sandbox test buyer — no real money is involved." }
}
```

`sandbox_buyer` is present only when the operator configured a sandbox buyer account
and the deployment is on the sandbox host.

### `GET /api/auth/guest`

Guest cookie required. Reports the current guest session. `404` if the caller is not
a guest.

### `POST /api/auth/guest/logout`

Public. Clears the guest cookie. `204`.

---

## Customer accounts (opt-in; disabled by default)

These routes return `404` unless `PACT_SELF_SERVICE_AUTH` is enabled. See
[`AUTH.md`](../AUTH.md) for the full model.

| Method | Path | Body | Response |
|---|---|---|---|
| `POST` | `/api/auth/register` | `{email, password (>=12), role (buyer\|seller), display_name?}` | `202 {detail}` |
| `POST` | `/api/auth/verify-email` | `{token}` | `{detail}` |
| `POST` | `/api/auth/login` | `{email, password}` | `{access_token, user}` + refresh cookie |
| `POST` | `/api/auth/refresh` | (refresh cookie) | `{access_token, user}` |
| `POST` | `/api/auth/logout` | (refresh cookie) | `204` |
| `GET` | `/api/auth/me` | — | the current principal |
| `POST` | `/api/auth/password/forgot` | `{email}` | `202 {detail}` |
| `POST` | `/api/auth/password/reset` | `{token, password (>=12)}` | `{detail}` |
| `POST` | `/api/auth/password/change` | `{current_password, new_password}` | `{detail}` |

---

## Jobs

### `POST /api/jobs` — buyer

Post a job. The buyer identity comes from the token, never the body.

**Request**

```json
{
  "title": "Write a haiku about escrow",
  "brief": "Produce a haiku about escrow and trust.",
  "acceptance_criteria": ["It is a haiku with three lines", "It mentions trust or escrow"],
  "budget_max": 300,
  "buyer_id": "ignored-and-overwritten",
  "seller_ids": ["seller_atlas", "seller_nova"]
}
```

| Field | Bounds |
|---|---|
| `title` | 3–200 chars |
| `brief` | 3–4000 chars |
| `acceptance_criteria` | 1–20 items, each 1–300 chars |
| `budget_max` | > 0 and <= 1,000,000 |
| `seller_ids` | up to 10, must be known sellers |

**Response 201:** the full job object (see below).

### `GET /api/jobs` — any

List jobs visible to the caller. Query `limit` (1–200, default 50).

**Response:** `{"count": n, "jobs": [<summary>, …]}`.

### `GET /api/jobs/{job_id}` — owner

Full job state. A non-owner gets `404`.

### `POST /api/jobs/{job_id}/run` — owner buyer

Run the full lifecycle (negotiate → hold → deliver → verify → settle). Query
`seller_ids` (comma-separated or repeated) to restrict sellers.

| Condition | Status |
|---|---|
| Job already running | `409` |
| Too many runs in flight | `429` (`Retry-After: 2`) |
| Run exceeded its deadline | `504` |
| Unknown seller requested | `400` |

**Response:** the full job object.

### `POST /api/jobs/{job_id}/approve` — owner buyer

Continue after the buyer approved the PayPal order. `409` if the job is not
`awaiting_approval` or the approval claim was lost.

### `POST /api/jobs/{job_id}/cancel` — owner buyer

Void any outstanding hold so the job can be deleted. `409` if the money has already
moved or the outcome is unknown; `502` if PayPal could not be reached (the job is
left unresolved).

### `POST /api/jobs/{job_id}/refund` — owner buyer

Refund a captured payment, in full or in part.

**Request (optional body):** `{"amount": 25.0, "reason": "…"}` (default: full refund).
**Response:** the full job object. `409` for an over-large amount or a wrong state;
`502` for a provider error or an ambiguous outcome.

### `POST /api/jobs/{job_id}/reconcile` — admin

Resolve an `unresolved` job by reading PayPal. `409` if the job is not unresolved.

### `DELETE /api/jobs/{job_id}` — admin

Tombstone a fully resolved job. `409` if the escrow is not terminal (cancel or
reconcile first). `204` on success.

### `GET /api/jobs/{job_id}/ledger` — owner

**Response**

```json
{ "job_id": "job_…", "entries": [ { "seq": 0, "kind": "job_created", "actor": "…", "summary": "…", "detail": {}, "prev_hash": "", "hash": "…" } ], "chain_valid": true, "entry_count": 24 }
```

### `POST /api/jobs/{job_id}/stream-ticket` — owner

Exchange a bearer token for a single-job SSE ticket.

**Response:** `{"ticket": "…", "expires_in": 60}`.

### `GET /api/jobs/{job_id}/stream` — owner

Server-sent events of the job's ledger. Authenticate with `?ticket=<ticket>` (from
the endpoint above) or a bearer header. Events: `ledger` (an entry) and `done`
(a terminal status).

### `GET /api/jobs/{job_id}/paypal-order` — admin

The provider's own order record, with identity redacted. `409` if the job has no
order or the deployment is simulated.

---

## Ledger and reputation

### `GET /api/reputation` — any

**Response:** `{"agents": [ { "agent_id": "seller_atlas", "score": 0.5, "settled": 0, "voided": 0, "failed": 0, "volume": 0 } ]}`.

### `GET /api/ledger` — any (scoped)

Settlement decisions visible to the caller, newest first. Query `limit` (1–500,
default 200).

**Response:** `{"count": n, "entries": [ { "job_id", "job_title", "seq", "at", "kind", "actor", "summary", "hash", "detail" } ]}`.

---

## The job object

`_job_full` returns:

| Field | Meaning |
|---|---|
| `id`, `title`, `status` | Identity and job status |
| `seller_id`, `agreed_price`, `agreed_terms`, `currency` | The agreement |
| `escrow_state`, `escrow_mode`, `simulated`, `settlement_is_real` | Payment state and honesty flags |
| `verified` | Whether verification passed (`null` if not yet verified) |
| `brief`, `acceptance_criteria`, `budget_max`, `buyer_id` | The job definition |
| `messages`, `offers`, `delivery`, `verification` | The negotiation and outcome |
| `escrow` | `{provider, mode, simulated, settlement_is_real, state, order_id, approval_url, authorization_id, authorization_status, capture_id, capture_status, refund_id, refund_status, refunded_amount, amount, currency}` |
| `ledger`, `ledger_chain_valid`, `ledger_entries` | The audit trail and its integrity |
| `created_at` | Creation time |

## Related

- Authentication in depth: [`AUTH.md`](../AUTH.md)
- The lifecycle these endpoints drive: [`payment-lifecycle.md`](../payment-lifecycle.md)
- Machine-readable endpoint index: [`metadata/api.json`](../../metadata/api.json)
