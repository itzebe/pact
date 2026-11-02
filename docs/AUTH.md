# Authentication and authorization

Pact holds and releases money, so it does not serve anonymous callers. Every
route that reads or moves value requires a bearer credential, and the caller's
identity is *derived* from a verified signature rather than trusted from the
request.

## The model

A credential is a JSON Web Token (HS256 by default) signed with
`PACT_AUTH_SECRET`. It carries a `sub` (who) and a `role` (what they may do).
Three roles exist and they are not interchangeable:

| Role | May do |
|---|---|
| `buyer` | Post jobs, run them, approve the PayPal order, cancel, and read **their own** jobs. |
| `seller` | Read jobs they were invited to or won. Cannot drive a settlement. |
| `admin` | Everything: reconcile, delete, read any job, and read the provider order record. |

A buyer is scoped to the jobs they own (`job.buyer_id == token.sub`). A buyer who
requests another buyer's job gets `404`, not `403`, so the API does not confirm
that the job exists. Reconciliation, cancellation, and deletion are separated
from the buyer role because they can settle or unwind real money: a stolen buyer
token must not be enough to reconcile or delete a record.

The `buyer_id` field on `POST /api/jobs` is accepted for backward compatibility
but is **overwritten** from the token. A caller cannot post a job as someone else.

## Failing closed

If `PACT_AUTH_SECRET` is unset, every protected route returns `503` and the
startup log records a configuration error. The API does not fall back to
"unauthenticated is fine", because a missing secret means "not configured", not
"open". In production the process also refuses to start with a missing or short
secret, or with the mock escrow enabled.

## Customer accounts (self-service)

The operator-token model above is unchanged and remains the default. On top of it
there is an *optional* customer-authentication subsystem, disabled until an owner
enables it. It is off by default (`PACT_SELF_SERVICE_AUTH` unset → every
`/api/auth/register` request is a `404`) because it needs a verified email sending
domain before it is useful.

When enabled, a customer gets a real account and a session:

| Endpoint | Purpose | Auth |
|---|---|---|
| `POST /api/auth/register` | Create a buyer or seller account, queue a verification email | public (404 when disabled) |
| `POST /api/auth/verify-email` | Consume a single-use verification token | public |
| `POST /api/auth/login` | Email + password → 15-minute access token + refresh cookie | public, rate-limited |
| `POST /api/auth/refresh` | Rotate the refresh cookie | public (cookie) |
| `POST /api/auth/logout` | Revoke the refresh family and clear the cookie | public (cookie) |
| `POST /api/auth/password/forgot` | Queue a reset email | public |
| `POST /api/auth/password/reset` | Consume a single-use reset token | public |
| `GET /api/auth/me` | The current caller | **protected** |
| `POST /api/auth/password/change` | Change password, revoke all sessions | **protected** |

Design decisions that matter:

- **Argon2id** password hashing (64 MiB, t=3, p=1) with an optional server-side
  pepper (`PACT_PASSWORD_PEPPER`). The pepper is never hard-coded and is not a
  substitute for the per-hash salt Argon2 already includes.
- **Access tokens are short-lived** (15 minutes) and carry a `ver` claim: the
  account's `token_version`. A password reset, a suspension, or an explicit
  revocation bumps that version, so every outstanding access token is refused at
  the next sensitive operation — coarse, immediate revocation without a per-token
  list.
- **Refresh tokens are single-use and rotating.** Presenting a spent token is
  treated as theft: the whole refresh *family* is revoked, so a stolen token
  cannot keep a session alive. Rotation is one conditional `UPDATE`, so two
  instances racing resolve to exactly one winner.
- **The refresh token is an `HttpOnly; Secure; SameSite=Strict` cookie** scoped to
  `/api/auth`, never returned in a JSON body, so frontend JavaScript cannot read it
  and it is never attached to a money route.
- **Registration and password-reset responses are uniform.** The same response is
  returned whether or not the address exists, so neither endpoint is an
  enumeration oracle. Login returns one generic failure for every rejection reason.
- **Public registration cannot create an admin.** The request schema only accepts
  `buyer` or `seller`, and the service re-checks against an allow-list. The LLM
  seller personas (`PACT_SELLERS`) are reserved identities and cannot be registered.
- **The sensitive-operation gate.** The money-bearing routes (`run`, `approve`,
  `cancel`, `refund`) carry an additional `require_verified` dependency on top of
  the existing role check. It refuses (403) an unverified, suspended, or
  version-revoked customer, and returns **503 if the shared store is unreachable**
  — a sensitive operation fails closed. A *legacy operator token* whose `sub` has
  no customer row passes (it is pre-verified by construction); this is a narrow,
  deliberate exception that keeps the existing operator workflow working and
  bypasses only the email-verification requirement, never a role or ownership
  check.

## Transactional email

Verification and reset emails are queued into an outbox table, not sent inline.
A dispatcher claims due rows with `FOR UPDATE SKIP LOCKED` under a lease, so
concurrent dispatchers never claim the same message and a dispatcher that crashes
after claiming has its row reclaimed once the lease expires. The unique
`idempotency_key` makes a duplicate enqueue a no-op. Delivery state is explicit:
`queued → accepted → delivered | bounced`, where `accepted` is *not* the same as
`delivered` — only a provider webhook may assert delivery. Until a provider is
configured (`PACT_EMAIL_PROVIDER` + credentials), every message is recorded
honestly as `queued` and the dispatcher reports that no provider is configured; it
never claims an email was sent.

Drain the queue with `python -m scripts.dispatch_email` (add `--loop` to keep
going until a pass claims nothing). Run it on a schedule — a platform cron, a
worker, or by hand — once a provider is configured; messages queued while none
was configured stay pending and are delivered on the first drain afterwards.

## Minting a token

```bash
export PACT_AUTH_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
cd backend
python -m scripts.issue_token demo-buyer buyer
```

The command prints only the token, so it composes:

```bash
TOKEN=$(python -m scripts.issue_token demo-buyer buyer)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/jobs | jq
```

## Server-sent events

`EventSource` cannot set an `Authorization` header, so the credential must travel
in the URL. Sending the real token there would put a 30-day credential that can
move money into server logs, browser history, and any `Referer` header. Instead
the client calls `POST /api/jobs/{id}/stream-ticket` over a normal authenticated
request and receives a ticket that is scoped to that one job and expires in 60
seconds. Only that ticket goes in the `?ticket=` parameter. A non-browser client
may still pass a full token in the `Authorization` header directly to the stream.

A scoped ticket is refused on every other route, so a leaked stream URL is not a
reusable credential.

A ticket is also **single-use**, and that is decided in the shared database rather
than in the process that minted it. A replay that lands on another instance — the
normal case behind a load balancer — finds the ticket already consumed and is
refused. In production a shared database is mandatory: startup refuses to run
without `DATABASE_URL`, so a multi-instance deployment cannot silently fall back to
a process-local registry. In development, where no `DATABASE_URL` is configured, the
registry is process-local, which is correct only because such a deployment is
single-instance by construction. If a shared store is configured but unreachable,
every ticket is refused rather than accepted by a local fallback no other instance
can see.

## Threat model

| Threat | Mitigation |
|---|---|
| Anonymous use of the API | Every protected route requires a token; the router-level dependency means a new route is protected by default. |
| Forged identity (a caller claiming to be another buyer) | Identity comes from the signed token, never the body; a bad signature is rejected. |
| Reading or driving another party's settlement (IDOR) | Ownership is checked per job; a non-owner sees `404`. |
| A compromised buyer token escalating to admin | Reconciliation, deletion, and the provider-order view require a separate admin role. |
| A forged `Host` becoming a PayPal return target | `PACT_PUBLIC_URL` is canonical; a fallback host must match `PACT_ALLOWED_HOSTS` or no `return_url` is produced. |
| A stream URL leaking a money-moving credential | The stream takes a 60-second, single-job ticket, never the real token; a scoped ticket is refused everywhere else. |
| Replaying a captured stream ticket against another instance | Consumption is recorded in the shared database with an atomic insert-and-consume, so exactly one instance can redeem a ticket; an unreachable shared store fails closed. |
| One buyer reading every tenant's jobs and ledger | `/api/jobs` and `/api/ledger` are filtered by the caller's role and ownership. |
| An oversized body exhausting memory before validation | A 64 KB content-length cap is enforced before the body is parsed. |
| Duplicate capture from concurrent requests | The job is claimed atomically (a lease on the filesystem store, an advisory lock on Postgres) before any provider call. |
| Credential stuffing / password brute force | Login is rate-limited per identifier (8 failures / 15 min), and the throttle refuses even a correct password once tripped. |
| Account enumeration via register/reset | Both return an identical response whether or not the address exists. |
| A stolen refresh token being reused | Refresh tokens rotate; presenting a spent one revokes the whole family. |
| A revoked credential remaining valid for its full lifetime | Access tokens are 15 minutes and carry a `token_version`; a reset/suspension/revocation bumps it and the sensitive gate refuses the stale token immediately. |
| A public registration creating an administrator | The register schema accepts only `buyer`/`seller`, re-checked against an allow-list server-side. |
| A money route being driven by an unverified or suspended customer | `require_verified` refuses with 403, and fails closed (503) if the shared account store is unreachable. |
| A duplicated verification/reset email | A unique idempotency key per outbox message makes a duplicate enqueue a no-op. |
| An email bomb to one mailbox via repeated resend/reset | Outbound verification and reset messages are capped per address (5 per hour); the cap is checked before a message is queued, and every send still uses a fresh idempotency key so a legitimate resend inside the cap is never blocked. |

## What this does not cover

Token issuance for the *operator* model is out of band: the operator mints tokens
and hands them to the right principals. Operator tokens are valid until they expire
(30 days by default); rotating `PACT_AUTH_SECRET` invalidates every outstanding
token at once, which is the intended kill switch.

The **customer** subsystem is opt-in and, until an owner configures a transactional
email provider and a verified sending domain, self-service registration stays
disabled. Enabling it requires setting `PACT_SELF_SERVICE_AUTH=true` plus
`PACT_EMAIL_PROVIDER`/`PACT_EMAIL_API_KEY`/`PACT_EMAIL_FROM` and a `PACT_PUBLIC_URL`
that the verification links are built from. Until then no customer can register and
the operator path is the only way in — which is the safe default for a deployment
that cannot yet deliver a verification email.
