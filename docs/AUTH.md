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
refused. Where no `DATABASE_URL` is configured the registry is process-local,
which is correct only because such a deployment is single-instance by
construction. If a shared store is configured but unreachable, every ticket is
refused rather than accepted by a local fallback no other instance can see.

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

## What this does not cover

Token issuance is out of band: the operator mints tokens and hands them to the
right principals. There is no self-service signup, no refresh flow, and no
per-token revocation list; a token is valid until it expires (30 days by
default). Rotating `PACT_AUTH_SECRET` invalidates every outstanding token at
once, which is the intended kill switch.
