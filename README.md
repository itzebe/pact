# Pact

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

---

## What it does

A buyer posts a job with a brief, a set of acceptance criteria, and a hard budget ceiling. Then:

1. **Seller agents compete.** Each quotes, the buyer counters, and the price converges over rounds. The budget is enforced in code, not requested of the model — an out-of-budget agreement is rejected by the engine regardless of what the LLM returns.
2. **Funds are held before work starts.** Pact creates a PayPal order with `intent=AUTHORIZE` and authorizes it. The seller now has committed funds to deliver against; the buyer has parted with nothing.
3. **The seller produces the deliverable** it was contracted for.
4. **An independent verifier checks it** against each acceptance criterion, with per-criterion evidence recorded. Deterministic checks (artifact present, no evasion markers) are combined with model judgement. A pass requires both.
5. **Settlement releases or returns.** Pass → `capture`, and the seller's reputation improves. Fail → `void`, the buyer is charged nothing, and the seller's reputation drops.

The reputation score then **feeds back into seller selection**: a seller with a poor verified-delivery record must be materially cheaper to win the next job. So the market routes around unreliable sellers on its own — reputation is an economic force here, not a dashboard number.

---

## Quick start

You need Python 3.11+ and Node 18+. No PayPal account or API key is required to run it — see [Running without credentials](#running-without-credentials).

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

```bash
cd backend && source .venv/bin/activate
python -m pytest tests/ -q
```

25 tests covering the escrow state machine, the budget guardrail, the settle-vs-refund branch, reputation feedback, and hash-chain tamper detection.

---

## Running without credentials

Pact is designed so a reviewer can clone it and see the complete lifecycle with **zero setup**.

| Missing | Behaviour |
|---|---|
| `PAYPAL_CLIENT_ID` / `SECRET` | Escrow runs against an in-memory double that enforces the same state machine. Every id is prefixed `MOCK-` and the UI labels it **Simulated PayPal** so it can never be mistaken for real money movement. |
| `LLM_API_KEY` | Agents fall back to a deterministic policy engine. The UI labels it **Mock agents**. |

Both fallbacks are deliberately loud. A simulated settlement must never be confused with a real one.

To require real credentials and refuse the mock path, set `PACT_ALLOW_MOCK_PAYPAL=false`.

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

PayPal publishes an official **Agent Toolkit**, and Pact could have used it. It cannot express this product. The toolkit hard-codes `"intent": "CAPTURE"` in its order payload builder, so it only does immediate payment — there is no authorize, no void, and therefore no way to make settlement conditional on an outcome. Escrow required building directly on the REST API.

Every call is idempotent via `PayPal-Request-Id`, so a retried request cannot double-charge.

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
| `GET` | `/api/jobs/{id}` | Full job state |
| `GET` | `/api/jobs/{id}/ledger` | Audit trail + chain validity |
| `GET` | `/api/jobs/{id}/stream` | Server-sent events, live |
| `GET` | `/api/reputation` | Agent reputation |
| `GET` | `/api/ledger` | Every settlement decision, newest first |

Interactive docs at `http://localhost:8000/docs`.

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
