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
| Recovery path | provider console | Neon Free PITR history window visible and a throwaway restore exercised (see §6 of [`beta-operations.md`](beta-operations.md)) |
| Reporting channel | §6 | a private destination is set and reachable |

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

## 4. Required tester journeys

Run these in order. Each maps to a real capability and a real safeguard. Journeys 1–10 are
the required journeys; journey 11 is the isolation check.

| # | Journey | Steps | Expected result |
| --- | --- | --- | --- |
| 1 | Create a job | Open the app; create a job with a brief and acceptance criteria | A guest workspace is provisioned; the job appears with status `draft`/`open` |
| 2 | Submit and review offers | Let the seller agents offer | Offers are listed against the job |
| 3 | Create and review an agreement | Accept an offer | An agreed price is recorded; the job moves toward escrow |
| 4 | Initiate payment authorization | Start the escrow run | A PayPal **sandbox** order is created; the job stops at `awaiting_approval` |
| 5 | Complete PayPal Sandbox buyer approval | At the approval step, approve using the **sandbox buyer** credentials the app shows | The authorization is **held** (funds reserved, not captured) |
| 6 | Capture payment | Let the verifier check the delivery | On a passing verdict the hold is **captured**; the job is `released` with a sandbox capture id |
| 7 | Verify settlement | Open the Ledger tab | The hash chain verifies; the capture is recorded as a settlement entry |
| 8 | Execute a refund | Refund the captured payment (full or partial) | A refund is recorded with a sandbox refund id; the amount is capped at the captured amount |
| 9 | Verify refund completion | Re-open the Ledger / job status | The refund is shown as a completed sandbox refund, not a pending claim |
| 10 | Review transaction history and status | Review the Ledger and the job status fields | Statuses are consistent; **nothing is shown as succeeded without its verified status** |
| 11 | Isolation check | With two testers, try to open the other's job by URL | `404` — cross-guest access is refused |

Notes for the facilitator:

- Journey 5 is the only step that needs an interactive PayPal login; the app surfaces the
  sandbox buyer credentials at that step. A run that stops at `awaiting_approval` before you
  approve is **correct** — it is waiting for the buyer.
- If a run's outcome is genuinely unknown, the job goes `unresolved` and is **not** retried
  blindly; resolve it with `reconcile` (see [`beta-operations.md`](beta-operations.md) §5).

## 5. Safety instructions for testers

Give these to every tester, in addition to the onboarding script (§3):

- **Never use real payment credentials.** Use only the sandbox buyer account the app shows
  you. This is PayPal Sandbox; no real money moves.
- **Never submit actual financial information** — no real card numbers, bank details, or
  PayPal passwords.
- **Never assume a transaction succeeded.** A step is done only when its status says so. If
  a status looks stuck, wrong, or ambiguous, treat it as a problem, not a success.
- **Report unexpected transaction states immediately** — especially a capture or refund that
  does not match what you did, or a job stuck in a state you did not expect.

## 6. How testers report problems

- **Where:** a single private channel the operator controls. **No safe channel exists yet** —
  the repository is public, so a GitHub issue would publish the report, and GitHub Private
  Vulnerability Reporting is disabled on the repository. See the proposal in §11. Until the
  operator sets a channel, the destination here is a placeholder:
  `<channel to be set by the operator>`.
- **What to include:** what you did, what you expected, what happened, the approximate time
  (UTC), and the job id if there is one.
- **What never to submit anywhere public:** the sandbox buyer password, any real credential,
  real financial information, personal data, or a full transaction/authorization id.
- **Escalation:** any money-path problem (authorization, capture, refund, `unresolved` state)
  is escalated to the operator immediately and handled as an incident (§10).

## 7. Operator monitoring

`GET /api/health` is the primary signal (see [`beta-operations.md`](beta-operations.md) §3).

| Area | What to watch |
| --- | --- |
| Application health | `status` other than `ok` → settlement is disabled (fail-closed) |
| Database availability | `storage.durable: false` → stop the beta; the ledger is not guaranteed durable; also watch the Neon dashboard |
| Failed or unresolved transactions | Any job in `unresolved` → reconcile (§9); a rising count is a pause condition |
| Capture and refund failures | Any capture/refund that errors or does not match the intent → incident |
| Webhook processing | `paypal.webhook_verification: false` → webhooks cannot be authenticated; watch for repeated or unprocessed webhooks |
| Authentication and authorization failures | Spikes in `401`/`403`; any cross-guest access that is not refused |
| Rate limiting and unexpected errors | `rate_limit.shared: false` → multi-instance under-counting; watch the Vercel deployment for error-rate anomalies |

## 8. Finalize before inviting

- [ ] Every pre-launch gate in §1 is green.
- [ ] The reporting destination in §6 is set to a real private channel.
- [ ] The cohort in §2 is named and briefed with §3 and §5.
- [ ] The operator has the `reconcile` command (§9) and the pause steps (§10) to hand.

## 9. Investigating an unresolved transaction

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

## 10. Pausing the beta

Pause participation immediately on any of:

- **Duplicate capture** — the same authorization captured more than once.
- **Incorrect refund amount** — a refund larger than the captured amount, or the wrong amount.
- **Incorrect ownership or access** — one tester sees another's job, or an authorization bypass.
- **Unverified settlement presented as successful** — a job shown `released`/settled without
  a verified capture.
- **Database integrity problem** — `storage.durable: false`, a failing hash chain, or a
  `status: degraded` that does not recover.
- **Any indication that PayPal Live has been enabled** — `paypal.is_live: true`, or any
  evidence of a real-money transaction.

How to pause:

1. Stop inviting testers and tell the current cohort to stop.
2. Promote the last known-good Vercel deployment to roll back if the fault is in a recent
   release (see [`deployment.md`](deployment.md) → Rollback considerations).
3. Record what happened, the affected job ids, and the timeline.
4. Fix on a branch, verify, and only then resume. Do not patch production directly.

## 11. Reporting-channel proposal (operator decision required)

No suitable private channel currently exists:

- The repository is **public**, so a GitHub issue would publish tester reports, job ids, and
  any accidental personal data.
- GitHub **Private Vulnerability Reporting is disabled** on the repository.
- [`SECURITY.md`](../SECURITY.md) routes security issues to the maintainer's GitHub profile
  email, which is fine for a coordinated security disclosure but is not a lightweight channel
  for everyday beta feedback.

Smallest practical options (the operator picks one; nothing is created here):

1. **Enable GitHub Private Vulnerability Reporting** (repository Settings → Security →
   Private vulnerability reporting). Free, private, already tied to the repo.
2. **A private channel the operator already runs** (a private chat, forum, or DM). Simplest,
   and keeps reports off GitHub entirely. Set the destination in §6.
3. **A dedicated private inbox** for beta feedback, separate from the security address.

**Recommendation:** option 2 for day-to-day feedback, plus option 1 for anything that looks
like a security issue. The operator must set the destination in §6 before inviting testers.

## 12. Launch materials prepared (for review only)

- This checklist.
- [`beta-operations.md`](beta-operations.md) — runbook.
- [`deployment.md`](deployment.md) — deploy/rollback and environment.
- [`api/README.md`](api/README.md) — the API a tester or integrator may call.
- The tester invitation package — see [`beta-invitation.md`](beta-invitation.md).

**Nothing here has been sent to any tester. No public access setting has been changed.**
