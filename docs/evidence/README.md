# PACT — evidence

This directory records the verifiable evidence behind PACT's claims: project
milestones, merged pull requests, release history, deployment references, test
verification, and the boundaries of what the demo establishes. It exists so a reader
can check claims rather than trust them.

The machine-readable version is [`metadata/evidence.json`](../../metadata/evidence.json).

## Verified milestones

| Date | Milestone | Reference |
|---|---|---|
| 2026-10-23 | Initial commit — "Pact: settlement infrastructure for the agent economy on PayPal escrow" | `76f2c3b` |
| 2026-10-25 | Deploy to Vercel with durable, instance-safe persistence | PR #1 |
| 2026-10-25 | Fail closed on a malformed model verdict | PR #3 |
| 2026-11-01 | Pre-launch security hardening; CI with PostgreSQL + PgBouncer | PR #14 |
| 2026-11-01 | Require PostgreSQL in production | PR #15 |
| 2026-11-02 | PayPal webhooks, refund API, recovery | PR #16 |
| 2026-11-02 | Customer authentication and shared connection pooling | PR #17 |
| 2026-11-02 | Fix production outage (statement_timeout startup parameter) | PR #18 |
| 2026-11-02 | Settlement lock on a direct endpoint | PR #20 |
| 2026-11-03 | Zero-friction guest sessions | PR #21 |
| 2026-11-03 | Judge-facing walkthrough and preserved evidence | PR #23 |
| 2026-11-03 | Hackathon submission package | PR #24 |
| 2026-11-03 | Final narrated demo video | PR #25 (`4ce1faf`) |

All 25 pull requests are merged into `master`; there are no open pull requests.
Verified via the GitHub API at documentation time.

## Release history

PACT has **no tagged releases**. The `master` branch is the release. The latest
merged change on `master` is PR #25 (`4ce1faf`, 2026-11-03). See
[`CHANGELOG.md`](../../CHANGELOG.md) for the change history.

## Deployment references

- **Live application:** <https://pact-flame.vercel.app/>
- **Health endpoint:** <https://pact-flame.vercel.app/api/health>
- **Host:** Vercel (Python function `api/index.py` + static SPA)
- **Database:** PostgreSQL (Neon-compatible)
- **Payments:** PayPal Sandbox

Live health verified on 2026-11-07:

```json
{
  "status": "ok",
  "paypal": { "configured": true, "environment": "sandbox", "mode": "sandbox", "settlement_is_real": true, "webhook_verification": true },
  "llm": { "configured": true, "is_mock": false, "model": "openai/gpt-oss-20b @ https://api.groq.com/openai/v1" },
  "storage": { "backend": "postgres", "durable": true }
}
```

## Test verification records

Commands run on 2026-11-07 in the workspace container (Python 3.11.17, Node
24.22.1, PostgreSQL 17):

| Suite | Command | Result |
|---|---|---|
| Backend (filesystem) | `python -m pytest tests/ -q` | **465 passed, 94 skipped** |
| Backend (PostgreSQL) | `DATABASE_URL=… python -m pytest tests/ -q` | **553 passed, 6 skipped** |
| Backend (PostgreSQL + PgBouncer) | `DATABASE_URL=… PGBOUNCER_URL=… python -m pytest tests/ -q` | **558 passed, 1 skipped** |
| Frontend unit | `npm test` | **43 passed** |
| Frontend build | `npm run build` | success |
| Lint | `python -m pyflakes pact/ scripts/` | clean |

The skips are environment-gated, not failures:

- `tests/test_guests.py:166` — the deterministic mock did not settle this run
  (environment-dependent).
- `tests/test_settlement_pooling.py` (4 tests) — need a distinct transaction-mode
  pooler endpoint (`PGBOUNCER_URL`), which the CI job provides.

## Demo instructions

1. Open <https://pact-flame.vercel.app/>. A guest workspace is provisioned
   automatically (no account).
2. Pick a job preset and press **Post job and settle**.
3. Follow the run: negotiation → hold → (buyer approval on the real sandbox path) →
   delivery → verification → settlement. The ledger streams live.
4. Re-verify the ledger hash chain, and on a captured job issue a refund.

See [`DEMO_SCRIPT.md`](../DEMO_SCRIPT.md) and
[`judge-demo/JUDGE_WALKTHROUGH.md`](../judge-demo/JUDGE_WALKTHROUGH.md) for a
step-by-step script.

## Screenshots and visual evidence

Production screenshots are under [`judge-demo/screenshots/`](../judge-demo/screenshots/)
(with its own [`README`](../judge-demo/screenshots/README.md)), and the demo evidence
images are under [`demo/evidence/`](../demo/evidence/). These are the project's own
captures; see the next section for their boundaries.

## Evidence explaining security claims

The security claims in [`docs/security.md`](../security.md) are backed by the tests
listed in [`metadata/security-controls.json`](../../metadata/security-controls.json).
Each control names its source file and the test that verifies it. No independent
security audit has been performed.

## What the evidence does and does not establish

Preserved in [`DEMO_EVIDENCE.md`](../DEMO_EVIDENCE.md) and
[`judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md`](../judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md):

- The demo videos and screenshots show a real run of the deployed product against
  PayPal **Sandbox**. They establish that the flow works end to end on the sandbox.
- They do **not** establish real-money settlement, an independent security audit, or
  that every feature works in every condition.
- A passing local test does not prove production behaviour; a successful deployment
  does not prove every feature works. Both distinctions are kept explicit.

## Related

- Change history: [`CHANGELOG.md`](../../CHANGELOG.md)
- Implementation status: [`implementation-status.md`](../implementation-status.md)
- Architecture decisions: [`architecture-decisions/README.md`](../architecture-decisions/README.md)
