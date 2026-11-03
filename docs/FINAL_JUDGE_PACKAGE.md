# Final judge package — PACT

Single entry point for a judge or an automated evaluator. Everything a judge needs to
understand, run, and verify PACT is linked from here.

**Repository:** <https://github.com/itzebe/pact> · **Live product:**
<https://pact-flame.vercel.app> · **License:** MIT · **Payment environment:** PayPal **Sandbox**

---

## 1. What PACT is

Two strangers agree to a job. One fears paying and receiving nothing; the other fears
delivering and never being paid. PACT holds the buyer's funds in PayPal **before work
starts**, has an independent AI verifier check the delivery against every acceptance
criterion, and releases or returns the money on that verdict — with a hash-chained record
of every step. A judge can use the whole thing without an account.

## 2. Read in this order

| # | Document | What it gives you |
| --- | --- | --- |
| 1 | [`../README.md`](../README.md) | The product, architecture, and how to run it |
| 2 | [`judge-demo/JUDGE_WALKTHROUGH.md`](judge-demo/JUDGE_WALKTHROUGH.md) | How to use PACT end to end |
| 3 | [`judge-demo/screenshots/README.md`](judge-demo/screenshots/README.md) | Production captures with an index |
| 4 | [`judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md`](judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md) | Provider ids, test results, CI — fresh vs historical |
| 5 | [`HACKATHON_SUBMISSION.md`](HACKATHON_SUBMISSION.md) | The submission copy |
| 6 | [`JUDGING_STRATEGY.md`](JUDGING_STRATEGY.md) | Rubric mapping and competitive positioning |
| 7 | [`TOPOLOGY.md`](TOPOLOGY.md) | Architecture / topology |
| 8 | [`judge-demo/FINAL_VIDEO_SCRIPT.md`](judge-demo/FINAL_VIDEO_SCRIPT.md) | Demo video script (+ storyboard, transcript, checklist) |
| 9 | [`judge-demo/CLEANUP_REPORT.md`](judge-demo/CLEANUP_REPORT.md) | Why verification records were preserved |

**Architecture note.** There is no separate `ARCHITECTURE.md`; [`TOPOLOGY.md`](TOPOLOGY.md)
is the architecture document, and the README carries the component overview. There is no
`JUDGE_EVIDENCE.md`, `evidence.json`, or LLM-judge FAQ in this repository; the evidence
lives in [`judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md`](judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md)
and [`DEMO_EVIDENCE.md`](DEMO_EVIDENCE.md).

## 3. Try it in one minute

1. Open <https://pact-flame.vercel.app>. A **guest workspace** is provisioned automatically
   — no sign-up, no form, no email.
2. Click the **Launch copy** preset, then **Post job and settle**.
3. Watch seller agents negotiate to an agreed price within the budget ceiling.
4. When the payment panel shows **"Waiting for your approval in PayPal"**, open the
   approval link and approve with the Sandbox test buyer shown in the panel.
5. Return and press **I approved — continue**. PACT authorizes the hold.
6. The verifier checks the delivery; a pass **captures** and the seller is paid. Inspect the
   **Ledger** tab. Use **Refund this payment** to reverse it through PayPal.

Or run it locally with no credentials:

```bash
docker build -t pact . && docker run -p 8000:8000 pact   # open http://localhost:8000
```

## 4. What has actually been verified

Everything below is separated so a fresh check is never confused with a historical record.

### 4.1 Current production checks (fresh)

| Check | Result |
| --- | --- |
| App loads | HTTP 200 at <https://pact-flame.vercel.app> |
| Health | `GET /api/health` → `status: ok` |
| Payment environment | `paypal.mode: sandbox`, `settlement_is_real: true`, `webhook_verification: true` |
| AI | `llm.configured: true`, `is_mock: false`, model `openai/gpt-oss-20b @ Groq` |
| Storage | `postgres`, durable |
| Guest access | `POST /api/auth/guest` → 201 |

### 4.2 Historical transaction evidence (recorded earlier, not re-issued)

Real PayPal **Sandbox** objects from a completed lifecycle:

- Order `60E54463RB1559702` → Authorization `0N5736387U1180942` → Capture `2YX75520SD675715C` → Refund `3UB477660D466340R`
- Corroborating capture `0MA99158MK626023P` with two partial refunds
- Earlier: order `9BU01766CS027323X`, authorization `7GB0704898318210B`, capture `05962303KN297374V`

These are **historical records**, not fresh live checks. Full detail and provenance in
[`judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md`](judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md)
and [`DEMO_EVIDENCE.md`](DEMO_EVIDENCE.md).

### 4.3 Automated tests (local, real PostgreSQL 17 + PgBouncer)

| Suite | Result |
| --- | --- |
| Backend `pytest tests/` | **554 passed, 1 skipped** (skip = mock verifier non-determinism) |
| `tests/test_settlement_pooling.py` | **13 passed** (PR #20 concurrency protection intact) |
| `pyflakes pact/ scripts/` | clean |
| Frontend `npm test` | **40 passed** |
| Frontend `npm run build` | success |

### 4.4 CI

The `CI` workflow runs Backend (PostgreSQL), Docker (production image), and Frontend jobs
on every PR. Recent runs are green; see the PR history on GitHub.

### 4.5 Local integration tests

The backend suite exercises the real PayPal client through its HTTP layer with only the
network boundary stubbed, plus API contract tests against the live FastAPI app.

### 4.6 Sandbox-only capabilities

- No real money moves anywhere. This is a requirement of the hackathon, not a limitation.
- Live-money mode is refused by a runtime guard; anonymous guests can never reach it.

## 5. Security posture (summary)

- Guest sessions: server-managed HttpOnly/Secure/SameSite cookies with CSRF tokens.
- Ownership enforced on the backend; a guest cannot read or mutate another guest's jobs.
- Guests get no operator admin, no global data access, no secret access, and no real-money
  authorization.
- Operator authentication is separate and unchanged.
- Idempotency and duplicate-capture protection; refund deduplication; settlement
  concurrency via a session advisory lock on a direct database endpoint.
- Webhook signature verification preserved.

## 6. Honest limitations

- **Sandbox only.** The demo never implies real payments.
- **Demo videos.** An **existing narrated 2:52 demo** exists at
  [`demo/pact-demo-final.mp4`](demo/pact-demo-final.mp4) (live app + PayPal Sandbox), but
  it predates the guest experience and refunds. A **new silent 58 s walkthrough** of the
  current product is at [`judge-demo/pact-walkthrough-silent.mp4`](judge-demo/pact-walkthrough-silent.mp4).
  The final narrated cut, the YouTube upload, and the Devpost submission are owner actions.
  See [`judge-demo/FINAL_VIDEO_CHECKLIST.md`](judge-demo/FINAL_VIDEO_CHECKLIST.md).
- **Verification is model-backed** and can occasionally fail a run (which correctly voids
  the hold).
- **No dispute/arbitration flow** and **no partial release** yet.
