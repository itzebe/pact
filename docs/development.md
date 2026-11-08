# PACT — development

A reproducible guide to running PACT locally and running its test suites. Every
command here was executed while writing this documentation; the test counts quoted
are the real ones.

## Prerequisites

- **Python 3.11+** (the backend declares `requires-python = ">=3.11"`).
- **Node 18+** (the frontend uses Vite 6; CI uses Node 20).
- **[uv](https://docs.astral.sh/uv/)** (recommended) or plain `pip`.
- **PostgreSQL** (optional locally, required to run the full suite and required in
  production).
- No PayPal account or API key is required to run the product: without credentials
  the escrow runs against an in-memory double, clearly labelled "Simulated PayPal".

## Installation

```bash
git clone https://github.com/itzebe/pact
cd pact
```

## Environment setup

```bash
cp .env.example .env
# Generate the API signing secret and put it in .env as PACT_AUTH_SECRET:
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Set at minimum:

```bash
PACT_ENV=development
PACT_AUTH_SECRET=<the generated value>
```

Everything else is optional for local development. See
[`.env.example`](../.env.example) for the full annotated list and
[`deployment.md`](deployment.md) for what production requires.

## Backend

```bash
cd backend
uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
uvicorn pact.main:app --reload --port 8000
```

The API is now at `http://localhost:8000`. Check it:

```bash
curl -s http://localhost:8000/api/health | jq '{status, paypal: .paypal.mode, storage}'
```

On a fresh checkout with no credentials this reports `paypal.mode: "mock"` and
`llm.is_mock: true`, which is the expected, honestly-labelled no-credentials path.

### Mint a token

```bash
python -m scripts.issue_token demo-buyer buyer
```

Use it as `Authorization: Bearer <token>`. A `buyer` may post and drive jobs; `admin`
may reconcile and delete.

## Frontend

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

On a deployment with guest sessions enabled the UI provisions a session
automatically. Locally you can paste an operator token into the connect screen, or
set `PACT_GUEST_SESSIONS=true` on the backend to exercise the guest path.

> **npm note.** Very recent npm versions block install scripts by default; Vite needs
> esbuild's binary. If `npm run build` complains, run `npm install-scripts approve
> esbuild` once.

## Database initialization

There is no migration step: the application creates its tables on first use.

```bash
createdb pact
export DATABASE_URL=postgresql://localhost/pact
```

Restart the backend and `/api/health` reports `storage.backend: "postgres"`,
`durable: true`.

## Running the tests

### Backend (filesystem store)

```bash
cd backend && source .venv/bin/activate
python -m pytest tests/ -q
```

Expected (verified in this repository): **516 passed, 103 skipped**. The skips are
the PostgreSQL integration tests, which skip themselves when no database is
reachable — a skip is not a pass.

### Backend (full, with PostgreSQL)

```bash
cd backend && source .venv/bin/activate
uv pip install -e ".[dev,postgres]"
export DATABASE_URL=postgresql://localhost/pact
python -m pytest tests/ -q
```

Expected: **613 passed, 6 skipped**. Supply a *separate* transaction-mode pooler
endpoint (`PGBOUNCER_URL`) to run those last 6 as well; with it reachable the suite
reports **619 passed, 0 skipped**. The CI job provides one.

### Frontend

```bash
cd frontend
npm test             # Node's built-in runner
npm run build        # production bundle
```

Expected: **58 passed** and a successful build.

### Lint

```bash
cd backend && python -m pyflakes pact/ scripts/
```

Expected: clean.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Protected routes return `503` | `PACT_AUTH_SECRET` is unset (fail closed) | Set it and restart |
| Badge says "Simulated PayPal" | No PayPal credentials found | Set `PAYPAL_CLIENT_ID`/`SECRET` and restart |
| Sellers quote identical prices | The deterministic mock is in use | Set `LLM_API_KEY` |
| `/api/health` shows `durable: false` | No `DATABASE_URL`, or the data dir is not writable | Set `DATABASE_URL` |
| Postgres tests all skip | No reachable database or missing `postgres` extra | `uv pip install -e ".[dev,postgres]"` and set `DATABASE_URL` |
| `npm run build` fails on esbuild | npm blocks install scripts | `npm install-scripts approve esbuild` |
| `ModuleNotFoundError: jwt` | A dependency declared in one manifest but missing from another | Ensure root `pyproject.toml`, `requirements.txt`, and `backend/pyproject.toml` agree (CI checks this) |

## Project conventions

- The backend uses FastAPI + Pydantic v2; keep new request bodies as bounded Pydantic
  models.
- Money-path changes need a test that exercises the real path (the suite stubs only
  the network boundary for the PayPal client).
- Follow [`AGENTS.md`](../AGENTS.md) for the invariants and the prohibited shortcuts,
  and [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the pull-request expectations.

## Related

- Architecture: [`architecture.md`](architecture.md)
- Deployment: [`deployment.md`](deployment.md)
- Documentation checks: [`README.md`](README.md)
