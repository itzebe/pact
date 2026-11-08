# PACT — controlled beta launch checklist

Prepared for review. **Not approved for distribution.** This is the operational plan
for a small, controlled beta of PACT against PayPal **Sandbox**. No testers are invited
and no access settings are changed by this document.

Read alongside [`beta-operations.md`](beta-operations.md) (how to run and recover a beta)
and [`security.md`](security.md) (the controls a beta depends on).

> **Sandbox only.** The beta runs against PayPal Sandbox. No real money moves. See the
> safety posture in [`beta-operations.md`](beta-operations.md) §1.

## 1. Pre-launch gates

Do not invite anyone until every gate is green. Each is checkable.

| Gate | Check | Pass condition |
| --- | --- | --- |
| Production up | `curl -s -o /dev/null -w '%{http_code}' https://pact-flame.vercel.app/` | `200` |
| Health ok | `curl -s https://pact-flame.vercel.app/api/health` | `status: "ok"` |
| Sandbox-only | same | `paypal.sandbox_only: true`, `paypal.is_live: false` |
| Real settlement path | same | `paypal.settlement_is_real: true` |
| Webhook verification | same | `paypal.webhook_verification: true` |
| Durable store | same | `storage.backend: "postgres"`, `storage.durable: true` |
| Shared rate limiting | same | `rate_limit.enabled: true`, `shared: true` |
| Real model | same | `llm.is_mock: false` |
| Guest sessions | `curl -s -X POST https://pact-flame.vercel.app/api/auth/guest -H 'Content-Type: application/json' -d '{}'` | `201` |
| Recovery path | provider console | Neon Free PITR history window visible (see §7 and [`beta-operations.md`](beta-operations.md) §6) |

## 2. Cohort

- **Size:** start with **3–5 testers**. Enough to exercise the flow, small enough to
  support each session personally.
- **Selection:** people comfortable with a Sandbox app who can follow a short script and
  report what they saw. No special access is granted — the app is guest-first.
- **Access:** none is provisioned. Testers open the public URL and are placed in an
  isolated guest workspace automatically. Do not change public access settings.
- **Cadence:** one cohort at a time. Do not open a second cohort until the first cohort's
  sessions are complete and any incidents are closed.

## 3. Sandbox-only onboarding (script for testers)

Give each tester exactly this:

1. Open <https://pact-flame.vercel.app/>. A private guest workspace is created for you
   automatically — there is no account to create.
2. Everything you do is visible **only** to your browser session. Your jobs, payments, and
   ledger are isolated from every other tester.
3. This is **PayPal Sandbox**. No real money moves. When the flow asks you to approve a
   payment, use the **sandbox buyer** email and password the app shows you on the approval
   step — do not use a real PayPal account.
4. Do not enter real payment details anywhere.

## 4. Test scenarios

Run these in order. Each maps to a real capability and a real safeguard.

| # | Scenario | Steps | Expected result |
| --- | --- | --- | --- |
| 1 | Guest session | Open the app | A guest workspace is provisioned; the header shows you are in a private guest workspace |
| 2 | Job creation | Create a job with a brief and acceptance criteria | Job appears in your workspace, status `draft`/`open` |
| 3 | Offer + agreement | Let a seller agent offer; accept the offer | An agreed price is recorded; job moves toward escrow |
| 4 | Payment authorization | Start the escrow run; at the approval step, approve with the **sandbox buyer** credentials the app provides | A PayPal **sandbox** order is created and approved; funds are **held** (authorized), not captured |
| 5 | Capture / settlement | Let the verifier check the delivery | On a passing verdict the hold is **captured**; the job is `released` with a real sandbox capture id |
| 6 | Settlement verification | Open the Ledger tab | The hash chain verifies; every step is recorded |
| 7 | Refund | Refund the captured payment (full or partial) | The refund is recorded with a sandbox refund id; the refunded amount is capped at the captured amount |
| 8 | Isolation | (Two testers) try to open the other's job by URL | `404` — cross-guest access is refused |

Notes for the facilitator:

- Scenario 4 is the only step that needs an interactive PayPal login; the app surfaces the
  sandbox buyer credentials at that step. A run that stops at `awaiting_approval` before
  you approve is **correct** — it is waiting for the buyer.
- If a run's outcome is genuinely unknown, the job goes `unresolved` and is **not** retried
  blindly; resolve it with `reconcile` (see [`beta-operations.md`](beta-operations.md) §5).

## 5. How testers report problems

- **Where:** a single shared channel you control (for example a support email or a chat
  channel). State it here before launch: `<channel to be set by the operator>`.
- **What to include:** the job id (if any), what you did, what you expected, what happened,
  a screenshot, and the approximate time (UTC).
- **What never to include:** the sandbox buyer password in a public place, or anything that
  looks like a real credential.
- Acknowledge every report. If it is a money-path issue (authorization, capture, refund,
  unresolved), treat it as an incident (§8).

## 6. Operational signals to monitor

`GET /api/health` is the primary signal (see [`beta-operations.md`](beta-operations.md) §3).

| Signal | Watch for |
| --- | --- |
| `status` | anything other than `ok` → settlement is disabled (fail-closed) |
| `storage.durable` | `false` → stop the beta; the ledger is not guaranteed durable |
| `rate_limit.shared` | `false` → a multi-instance deployment is under-counting |
| `paypal.is_live` | `true` → incident; the beta is pointed at live money |
| `paypal.webhook_verification` | `false` → webhooks cannot be authenticated |
| `llm.is_mock` | `true` → no model key; the deterministic mock verifier is running |

Also watch the Vercel deployment for error-rate or function-invocation anomalies, and the
Neon dashboard for the database's own health.

## 7. Investigating an unresolved transaction

A job is `unresolved` when a provider call's outcome is genuinely unknown (an ambiguous
capture or refund). PACT never guesses. Resolution is administrative:

```bash
TOKEN=$(cd backend && python -m scripts.issue_token ops-admin admin)
curl -s -X POST https://<deployment>/api/jobs/<job_id>/reconcile \
  -H "Authorization: Bearer $TOKEN"
```

`reconcile` only reads PayPal to establish what actually happened and never initiates a
capture. Full procedure and the recovery expectations are in
[`beta-operations.md`](beta-operations.md) §5–6.

## 8. Pausing the beta

Pause participation immediately on any of:

- `paypal.is_live: true`, or any evidence of a real-money transaction.
- `storage.durable: false`, or `status: degraded` that does not recover.
- A confirmed money-path defect (double capture, incorrect refund, wrong settlement).
- A security issue: cross-guest data exposure, an authorization bypass, or a CSRF failure.

How to pause:

1. Stop inviting testers and tell the current cohort to stop.
2. Promote the last known-good Vercel deployment to roll back if the fault is in a recent
   release (see [`deployment.md`](deployment.md) → Rollback considerations).
3. Record what happened, the affected job ids, and the timeline.
4. Fix on a branch, verify, and only then resume. Do not patch production directly.

## 9. Launch materials prepared (for review only)

- This checklist.
- [`beta-operations.md`](beta-operations.md) — runbook.
- [`deployment.md`](deployment.md) — deploy/rollback and environment.
- [`api/README.md`](api/README.md) — the API a tester or integrator may call.

**Nothing here has been sent to any tester. No public access setting has been changed.**
