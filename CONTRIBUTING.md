# Contributing to PACT

Thanks for your interest. PACT is a small, focused project: conditional settlement
for agent work on PayPal escrow. Contributions that make it more correct, safer, or
better documented are welcome. Because PACT coordinates money movement, the bar for
a change to the settlement path is higher than for a UI tweak — read the relevant
section below before opening a pull request.

## Ground rules

- **Correctness over cleverness.** On the money path, a clear implementation with a
  test beats a terse one without.
- **Fail closed.** When something cannot be verified, refuse rather than assume
  success. This applies to auth, webhooks, and every provider call.
- **No fabricated evidence.** Do not claim behaviour you have not tested, and do
  not add a test that only proves the code compiles.
- **Keep changes focused.** One concern per pull request. Do not mix a refactor with
  a feature.
- **Sandbox only.** Never enable PayPal Live mode, and never weaken the
  sandbox-only guard.

## Getting set up

See [`docs/development.md`](docs/development.md) for the full, reproducible setup.
The short version:

```bash
git clone https://github.com/itzebe/pact && cd pact/backend
uv venv --python 3.11 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
export PACT_AUTH_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
uvicorn pact.main:app --reload --port 8000
```

## Before you open a pull request

Run the checks the project runs in CI:

```bash
# Backend (filesystem store; Postgres tests self-skip)
cd backend && python -m pytest tests/ -q

# Backend with a database (nothing skips)
DATABASE_URL=postgresql://localhost/pact python -m pytest tests/ -q

# Lint
python -m pyflakes pact/ scripts/

# Frontend
cd ../frontend && npm ci && npm test && npm run build
```

All of these must pass. A pull request that leaves a check failing will not be
merged; a pull request that weakens a test to pass will be closed.

## What a good change looks like

- **It is tested.** New behaviour has a test; a bug fix has a regression test that
  fails without the fix.
- **It respects the invariants** in [`AGENTS.md`](AGENTS.md) §4 (held means held,
  settled means paid, captured at most once, unknown is not failure, fail closed,
  simulated is never reported as real).
- **It is documented.** Update the relevant file under `docs/` and the matching
  `metadata/*.json`. Keep [`docs/implementation-status.md`](docs/implementation-status.md)
  honest.
- **It explains the why.** Commit messages are imperative and scoped to one
  concern. Non-obvious decisions carry a short comment explaining the reasoning.

## Areas that need care

| Area | Files | Why |
|---|---|---|
| Settlement | `backend/pact/core/settlement.py` | Decides whether money moves. |
| Postgres store | `backend/pact/core/dbstore.py`, `core/pool.py` | Cross-instance safety. |
| Auth | `backend/pact/api/auth.py`, `core/auth_store.py` | Access control and sessions. |
| Routes | `backend/pact/api/routes.py` | The public API surface. |
| PayPal client | `backend/pact/paypal/escrow.py` | Talks to the provider. |

Changes here need a test that exercises the real path, not a mock of the object
under test.

## Reporting a bug or a security issue

- Ordinary bugs and feature requests: open a GitHub issue with steps to reproduce,
  expected vs actual behaviour, and the commit you tested.
- **Security issues: do not open a public issue.** Follow
  [`SECURITY.md`](SECURITY.md) instead.

## Style

- **Python:** standard library conventions; type hints where they add clarity;
  comments only for genuinely non-obvious intent, invariants, or workarounds. The
  suite is checked with `pyflakes`.
- **JavaScript/React:** the existing functional-component style; keep state local;
  the frontend tests use Node's built-in runner (`node --test`).
- **Markdown:** one `h1` per file, descriptive headings, working relative links.
  Run the documentation checks described in [`docs/README.md`](docs/README.md).

## Code of conduct

Be respectful and constructive. Critique the work, not the person. Assume good
faith. Harassment or personal attacks are not tolerated and will be removed.

## License

By contributing you agree that your contribution is licensed under the project's
MIT license (see [`LICENSE`](LICENSE)).
