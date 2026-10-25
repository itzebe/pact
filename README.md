# Pact

**Live demo:** <https://pact-flame.vercel.app/> · **Source:** <https://github.com/itzebe/pact> · **Demo video:** [`docs/demo/pact-demo.mp4`](docs/demo/pact-demo.mp4) (2:25, real capture of the deployed app against PayPal Sandbox)

**Settlement infrastructure for the agent economy, built on PayPal escrow.**

AI agents can already negotiate and produce work. What they cannot do is *safely pay each other*, because there is no neutral party holding the money until the work is proven. Pact is that layer: agents negotiate a price, PayPal holds the buyer's funds in escrow, an independent verifier checks the delivery, and only then does the money move.

If verification fails, the hold is voided and the buyer is charged nothing.

```
brief ─▶ negotiate ─▶ agree ─▶ HOLD (PayPal authorize) ─▶ deliver ─▶ verify ─┬─▶ pass  ─▶ CAPTURE (release)
                                                                            └─▶ fail  ─▶ VOID    (return)
```

---

## Why this exists

Every serious "trust layer for agents" effort today — Circle, BNB Chain, ERC-8004, x402 — settles in crypto. That excludes the overwhelming majority of real commerce, which runs on fiat rails. Meanwhile the blocker to autonomous purchasing is not capability, it is that nobody will hand an agent a card and hope for the best.

PayPal already provides the exact primitive this needs. Orders v2 supports `intent=AUTHORIZE`, which **reserves** funds rather than taking them, and then offers a genuine two-way branch: `capture` to release, or `void` to return. That is escrow, and it works in the sandbox. Pact builds the missing protocol on top of it.

One detail that shapes the whole design: PayPal will not authorize an order until the buyer approves it (or a `payment_source` is supplied inline). Pact does not skip that step — it surfaces the order's `rel:payer-action` link, stops before any work is produced, and only continues once the buyer has approved. Funds are never claimed to be held on the strength of a created order.

---

## What it does

A buyer posts a job with a brief, a set of acceptance criteria, and a hard budget ceiling. Then:

1. **Seller agents compete.** Each quotes, the buyer counters, and the price converges over rounds. The budget is enforced in code, not requested of the model — an out-of-budget agreement is rejected by the engine regardless of what the LLM returns.
2. **Funds are held before work starts.** Pact creates a PayPal order with `intent=AUTHORIZE`, the buyer approves it, and Pact authorizes it. The seller now has committed funds to deliver against; the buyer has parted with nothing. Nothing is delivered until the hold is confirmed by PayPal.
3. **The seller produces the deliverable** it was contracted for.
4. **An independent verifier checks it** against each acceptance criterion, with per-criterion evidence recorded. Deterministic checks (artifact present, no evasion markers) are combined with model judgement. A pass requires both.
5. **Settlement releases or returns.** Pass → `capture`, and the seller's reputation improves. Fail → `void`, the buyer is charged nothing, and the seller's reputation drops.

The reputation score then **feeds back into seller selection**: a seller with a poor verified-delivery record must be materially cheaper to win the next job. So the market routes around unreliable sellers on its own — reputation is an economic force here, not a dashboard number.

### The payment invariants

These are the guarantees that protect real money, and each is covered by a test:

- **Held means held.** A hold is recorded only when PayPal reports the authorization in a holdable state. `DENIED` is a failure; an authorization that is already `CAPTURED` is flagged for reconciliation, never treated as a success.
- **Settled means paid.** A job is recorded as settled only when the capture status is `COMPLETED`. `PENDING`, `FAILED` and `DECLINED` are not settlement.
- **Captured at most once.** Settlement is serialised behind a lock and claims the authorization before the call, so concurrent requests cannot produce a duplicate capture. A second attempt is refused.
- **An unknown outcome is not a failure.** A timeout, a dropped connection, or a 5xx leaves the job `unresolved` and asks a human to reconcile. It is never retried automatically, because a blind retry is how double-charges happen.
- **Reconciliation asks PayPal, it does not guess.** `POST /api/jobs/{id}/reconcile` reads the authorization (and the order, if the authorize itself was lost) to establish what actually happened. `CAPTURED` settles the job using the real capture id recovered from PayPal and credits the seller (reversing the failure recorded while the outcome was unknown); an intact hold clears the in-flight marker so the job is retryable; an unreachable PayPal leaves the job `unresolved` and blocked, because an outage is not evidence that no money moved.
- **A void is confirmed.** After voiding, Pact re-reads the authorization and records whether PayPal confirmed it.
- **No capture without a passing verdict.** A failed or inconclusive verification never reaches the capture call.

---

## Quick start

You need Python 3.11+ and Node 18+. No PayPal account or API key is required to run it — see [Running without credentials](#running-without-credentials).

The deployed instance is live at <https://pact-flame.vercel.app/>: pick a preset, press **Post job and settle**, and the full lifecycle runs against PayPal Sandbox.

### 1. Backend

```bash
cd backend
uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"

cp ../.env.example ../.env      # fill in credentials, or leave blank
uvicorn pact.main:app --reload --port 8000
```

### 2. Frontend

```bash
cd frontend
npm install
npm run dev                     # http://localhost:5173
```

Open http://localhost:5173, pick a job preset, and press **Post job and settle**. The full lifecycle runs and streams into the UI as it happens.

### 3. Tests

The suite has two backends. Most tests run against the filesystem store and need
nothing extra. The durability and cross-process tests need a reachable Postgres
and the optional `postgres` dependency, and skip themselves otherwise.

```bash
cd backend && source .venv/bin/activate

# Development dependencies (pytest). Does NOT include the Postgres driver.
uv pip install -e ".[dev]"

# Filesystem-backed suite — the default, no database required.
# 218 passed, 16 skipped (the Postgres tests skip themselves).
python -m pytest tests/ -q
```

To run the complete suite, add the `postgres` extra and point the tests at a
database. The extra is a separate dependency group — `".[dev]"` alone leaves the
Postgres tests skipped:

```bash
# Add the Postgres driver (psycopg). Also covers the extra plus dev tooling:
uv pip install -e ".[dev,postgres]"

# Point the tests at any reachable Postgres, then run everything.
# 234 passed — nothing skipped.
createdb pact                                          # or a Neon/Supabase/RDS URL
export DATABASE_URL=postgresql://localhost/pact
python -m pytest tests/ -q
```

`DATABASE_URL` is the only variable these tests read; it is the same setting the
application uses for durable storage. Every setting, including `DATABASE_URL`, is
documented in [`.env.example`](.env.example). Omit it and the Postgres tests report
as skipped, which is expected — never a failure.

Frontend unit tests (Node's built-in runner, no extra dependency):

```bash
cd frontend && npm test
```

234 tests covering the escrow state machine, the payment-status invariants (holdable authorization, `COMPLETED`-only settlement, single capture, confirmed void), the buyer-approval flow, transport-failure classification, strict parsing of model-supplied verdicts (fail-closed against string booleans and self-contradicting passes), the budget guardrail, reputation feedback (including a concurrent lost-update guard with a negative control), reconciliation of ambiguous outcomes, the API contract, and hash-chain tamper detection. The real PayPal client is tested through its HTTP layer with only the network boundary stubbed, so request construction, response parsing, and error classification are exercised for real.

---

## Running without credentials

Pact is designed so a reviewer can clone it and see the complete lifecycle with **zero setup**.

| Missing | Behaviour |
|---|---|
| `PAYPAL_CLIENT_ID` / `SECRET` | Escrow runs against an in-memory double that enforces the same state machine. Every id is prefixed `MOCK-` and the UI labels it **Simulated PayPal** so it can never be mistaken for real money movement. |
| `LLM_API_KEY` | Agents fall back to a deterministic policy engine. The UI labels it **Mock agents**. |

Both fallbacks are deliberately loud. A simulated settlement must never be confused with a real one.

To require real credentials and refuse the mock path, set `PACT_ALLOW_MOCK_PAYPAL=false`. To make missing credentials a hard startup failure on a deployment where settlement must be real, set `PACT_REQUIRE_REAL_PAYPAL=true` — otherwise a production deploy with no credentials would settle against the in-memory double and report a `settled` job for which no money ever moved.

---

## Using real PayPal Sandbox

1. Create a free account at [developer.paypal.com](https://developer.paypal.com/dashboard/).
2. Go to **Apps & Credentials → Sandbox** and copy the Client ID and Secret.
3. Put them in `.env`:

```bash
PAYPAL_CLIENT_ID=your_sandbox_client_id
PAYPAL_CLIENT_SECRET=your_sandbox_secret
PAYPAL_ENVIRONMENT=sandbox
```

4. Restart the backend. The UI badge switches from **Simulated PayPal** to **PayPal Sandbox**, and every settlement creates real objects on `api-m.sandbox.paypal.com`.

5. Run a job. Because PayPal requires the buyer to approve an order before it can be authorized, the run stops at **Buyer approval required** and shows the order's approval link. Open it, approve with a sandbox buyer account, then click **I approved — continue**. Pact authorizes the order, produces and verifies the delivery, and settles.

In simulated mode there is no buyer to click, so the double auto-approves and the whole lifecycle runs in one request. That is the only difference between the two paths — the state machine, the guards, and the ledger are the same code.

You can verify the money movement in the sandbox merchant account's Activity page — a captured authorization shows up as a completed payment, and a voided one does not.

### The AI layer

Pact speaks to any OpenAI-compatible endpoint, so the model is a deployment choice rather than a hard-coded vendor:

```bash
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1     # or Groq, Together, OpenRouter, Ollama, vLLM...
LLM_MODEL=gpt-4o-mini
```

---

## Architecture

```
backend/pact/
├── paypal/
│   ├── escrow.py      PayPal Orders v2 escrow client: AUTHORIZE → capture | void
│   └── mock.py        In-memory double with the same state machine
├── core/
│   ├── models.py      Job, ledger with SHA-256 hash chaining
│   ├── negotiation.py Multi-round bargaining; budget guardrail; risk-adjusted selection
│   ├── settlement.py  Verifier + escrow release/refund decisions
│   ├── engine.py      The orchestrator: one call drives the whole lifecycle
│   ├── llm.py         Provider-agnostic LLM access + deterministic fallback
│   └── store.py       Disk-backed job store and reputation ledger
└── api/routes.py      REST + server-sent events
```

### Why the escrow client is hand-written

PayPal publishes an official **Agent Toolkit**, and Pact could have used it. It cannot express this product. The toolkit hard-codes `"intent": "CAPTURE"` in its order payload builder (`python/paypal_agent_toolkit/shared/orders/payload_util.py`), so it only does immediate payment — there is no authorize call, no void, and therefore no way to make settlement conditional on an outcome. Escrow required building directly on the REST API.

Every call is idempotent via `PayPal-Request-Id`, so a retried request cannot double-charge.

The client's behaviour is pinned to PayPal's published OpenAPI specifications, and the test suite asserts against those enum values rather than against assumptions:

| Fact | Source |
|---|---|
| `intent` is `CAPTURE` or `AUTHORIZE` | `checkout_payment_intent` |
| Authorization states are `CREATED, CAPTURED, DENIED, PARTIALLY_CAPTURED, VOIDED, PENDING` | `authorization_status` |
| Capture states are `COMPLETED, DECLINED, PARTIALLY_REFUNDED, PENDING, REFUNDED, FAILED` | `capture_status` |
| The buyer must approve before authorization | Orders v2 authorize endpoint |
| A fully captured payment cannot be voided | Payments v2 void endpoint |

### The audit ledger

Every state transition appends to a hash-chained ledger. Each entry stores the hash of the one before it, so the record is tamper-evident: editing any historical row breaks the chain, and the UI verifies it live.

```
{"seq": 10, "kind": "escrow_order_created", "actor": "paypal",
 "summary": "PayPal order 5O190127TN364715T created (intent=AUTHORIZE)",
 "prev_hash": "7810a552…", "hash": "112dfc52…"}
```

This is what makes the output defensible as a financial record rather than an application log — an auditor can recompute the chain and prove nothing was altered after settlement.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Configuration and settlement mode |
| `POST` | `/api/jobs` | Post a job |
| `POST` | `/api/jobs/{id}/run` | Run the full lifecycle |
| `POST` | `/api/jobs/{id}/approve` | Continue after the buyer approves the PayPal order |
| `POST` | `/api/jobs/{id}/reconcile` | Resolve an `unresolved` job by reading PayPal |
| `GET` | `/api/jobs/{id}` | Full job state |
| `GET` | `/api/jobs/{id}/ledger` | Audit trail + chain validity |
| `GET` | `/api/jobs/{id}/stream` | Server-sent events, live |
| `GET` | `/api/reputation` | Agent reputation |
| `GET` | `/api/ledger` | Every settlement decision, newest first |

Interactive docs at `http://localhost:8000/docs`.

---

## Deploying to Vercel

Vercel Functions have a read-only filesystem with ephemeral `/tmp`, and several
instances may serve the same job. Two things follow, and both are handled:

- **Persistence must be external.** Set `DATABASE_URL` to a Postgres URL
  (Vercel Postgres, Neon, Supabase, and so on). Jobs and the reputation ledger
  then live in the database. Without it the app still runs, but `/api/health`
  reports `"durable": false` and the ledger is lost on cold start.
- **Settlement must be instance-safe.** On Postgres the store takes a session
  advisory lock per job and re-reads the authoritative row inside it, so two
  instances cannot both capture the same authorization. The filesystem store
  cannot do this; its `threading.RLock` only protects one process.

The repository is already configured — `vercel.json` builds the frontend and
routes `/api/*` to the FastAPI function in `api/index.py`.

1. Import the repository at [vercel.com/new](https://vercel.com/new). Leave the
   framework preset as **Other**; `vercel.json` supplies the commands.
2. Create a Postgres database and add its pooled connection string as the
   `DATABASE_URL` environment variable for Production, Preview, and Development.
3. Add `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` (sandbox) and, optionally,
   `LLM_API_KEY` and `LLM_MODEL`. Without PayPal credentials the escrow runs
   against the in-memory double and the UI labels it "Simulated PayPal".
4. Deploy, then confirm the wiring:

```bash
curl -s https://<your-deployment>/api/health | jq '{paypal: .paypal.mode, storage}'
```

`storage.backend` should read `postgres` and `storage.durable` should be `true`.
The tables are created on first use, so no migration step is needed.

### Verifying the serverless path locally

The Postgres tests are skipped unless a database is reachable, so the default
suite stays dependency-free. They also need the optional `postgres` extra
(`uv pip install -e ".[dev,postgres]"`). Point them at a database to exercise
durability, the read-only-filesystem path, and the cross-process capture race:

```bash
createdb pact
DATABASE_URL=postgresql://localhost/pact pytest tests/test_postgres_store.py -v
```

---

## Tools used

| Tool | How it is used |
|---|---|
| **PayPal Orders v2** | The settlement rail. `intent=AUTHORIZE` to hold, `POST /authorize`, then `/capture` to release or `POST /v2/payments/authorizations/{id}/void` to return. Idempotent via `PayPal-Request-Id`. |
| **PayPal Sandbox** | The environment the demo runs against, so the money movement is real API traffic without real money. |
| **AG Grid** | The settlement ledger grid in the UI — sortable, resizable audit records with hash-chain status. |
| **LLM (OpenAI-compatible)** | Three distinct agent roles: seller quoting, buyer countering, and independent verification. Any compatible provider works. |

---

## Notes and limits

- Jobs and reputation persist to `backend/data/` as JSON, so a restart does not lose settlement history.
- Sandbox authorizations expire after a few days, which is the natural window for a delivery to be verified.
- The verifier's deterministic checks and model judgement are combined with AND, so a persuasive-but-empty delivery cannot pass. See `EVASION_MARKERS` in `core/settlement.py`.
- Reputation is smoothed toward 0.5 for thin histories, so a new agent is not treated as proven and one lucky settlement does not outrank a consistent record.

### Troubleshooting

**`npm run build` fails complaining about esbuild's binary.** Very recent npm versions (11.21+) block
package install scripts by default. Vite needs esbuild's binary, so approve it once:

```bash
npm install-scripts approve esbuild
```

On npm 10 and earlier this does not occur — install scripts run by default.

**Settlement completes but the UI badge says "Simulated PayPal".** No credentials were found, so the
escrow ran against the in-memory double. Set `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` in `.env` and
restart the backend. The badge is the authoritative indicator of which path ran — a simulated settlement
is always labelled as such.

**Agents quote identical prices and never negotiate.** Without `LLM_API_KEY` the deterministic fallback
engine is in use, which is intentionally predictable. Set a key to get genuine model-driven bargaining.

## License

MIT — see [LICENSE](LICENSE).
