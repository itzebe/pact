# Security policy

PACT coordinates money movement through PayPal escrow. Security is a first-class
concern of the project, and the controls below are implemented in code and covered
by tests. This document explains how to report a vulnerability and summarises the
security posture. The full technical treatment is in
[`docs/security.md`](docs/security.md).

## Reporting a vulnerability

**Please do not open a public GitHub issue for a security problem.**

Report it privately by email to the maintainer of the repository
(see the GitHub profile at https://github.com/itzebe). Include:

- A description of the issue and its impact.
- Steps to reproduce, or a proof of concept.
- The commit or deployment you tested against.
- Any suggested mitigation.

You will get an acknowledgement, an assessment, and a coordinated disclosure
timeline. Please give a reasonable window to fix the issue before any public
disclosure.

## Scope

In scope:

- The API in this repository and the live deployment at
  https://pact-flame.vercel.app/.
- Authentication, authorization, session, and CSRF handling.
- The PayPal escrow lifecycle, webhook verification, and idempotency.
- The PostgreSQL persistence and cross-instance safety guarantees.

Out of scope:

- Denial of service by volume (this is a hackathon-scale deployment).
- The PayPal platform itself.
- Anything requiring a compromised PayPal merchant account.
- Findings that depend on running with an unsafe configuration the project
  explicitly refuses (for example enabling PayPal Live mode, which the code blocks).

## What the project already does

These controls are implemented and tested; see
[`docs/security.md`](docs/security.md) for the file-level detail and
[`metadata/security-controls.json`](metadata/security-controls.json) for the index.

- **Fail-closed authentication.** A missing `PACT_AUTH_SECRET` refuses every
  protected request with `503`; in production the process refuses to start.
- **Object-level authorization.** Every job access checks the caller is a party to
  it; a non-owner receives `404`, not `403`, so the API does not confirm existence.
- **Role separation.** `buyer`, `seller`, and `admin` are distinct; reconciliation,
  deletion, and provider-order inspection require `admin`.
- **Guest-session isolation.** A guest is an ordinary buyer with its own workspace;
  the session cookie is `HttpOnly`, `SameSite=Lax`, scoped to `/api`; mutating
  requests require a CSRF token.
- **Webhook verification.** PayPal webhook signatures are verified through PayPal's
  own verify endpoint; an unverifiable event changes nothing.
- **Exactly-once processing.** Webhook event ids are claimed once in a shared store;
  a duplicate is acknowledged and ignored.
- **Duplicate-capture prevention.** The authorization is claimed and persisted before
  the provider call, and the settlement transition is serialised per job.
- **Sandbox-only guard.** `PACT_PAYPAL_SANDBOX_ONLY` hard-refuses a live host, and
  startup refuses guest sessions combined with a live host.
- **Input bounds.** Request bodies are capped, criteria and budgets are bounded, and
  a non-finite budget is a `422` rather than a `500`.
- **Redaction.** Credentials and payer identity are masked from logs and responses.

## Honest limitations

- **No independent audit.** The controls above are implemented and tested but have
  not been reviewed by a third-party security firm, and PACT holds no security
  certification.
- **Sandbox only.** The live deployment moves no real money; a real-money deployment
  would need additional operational review (key management, monitoring, incident
  response).
- **Email and customer accounts are off by default.** Enabling them requires a
  verified sending domain and additional operational care.

## Safe harbour

Security research conducted in good faith against your own deployment, or against
the public demo within its documented scope, is welcome. Do not attack other users'
data, do not exfiltrate data you did not create, and do not degrade the service.
