# PACT documentation

This directory is the canonical technical documentation for PACT. It is organised
so that a reader (human or machine) can start at the top and follow links to
whatever depth they need. The navigation map that connects the sections is
[`knowledge-map.md`](knowledge-map.md).

If you are new to PACT, read in this order: [`overview.md`](overview.md) →
[`architecture.md`](architecture.md) → [`payment-lifecycle.md`](payment-lifecycle.md)
→ [`api/README.md`](api/README.md).

## Product and concept

| Document | What it covers |
|---|---|
| [`overview.md`](overview.md) | Problem, philosophy, personas, user journeys, boundaries, limitations. |
| [`glossary.md`](glossary.md) | Definitions of every term used across the docs. |
| [`faq.md`](faq.md) | Grounded answers to common questions. |
| [`implementation-status.md`](implementation-status.md) | What is implemented, verified, partial, or planned. |

## Technical

| Document | What it covers |
|---|---|
| [`architecture.md`](architecture.md) | System architecture, components, data flow, diagrams. |
| [`payment-lifecycle.md`](payment-lifecycle.md) | Every escrow stage, its triggers, safeguards, and verification. |
| [`database.md`](database.md) | Schema, migrations, isolation, integrity, retention. |
| [`security.md`](security.md) | Controls, threat model, verification. |
| [`ai-system.md`](ai-system.md) | The AI roles, their inputs/outputs, and their boundaries. |
| [`api/README.md`](api/README.md) | The full HTTP API reference. |
| [`AUTH.md`](AUTH.md) | Authentication and authorization in depth. |
| [`TOPOLOGY.md`](TOPOLOGY.md) | Multi-instance safety analysis. |

## Operations

| Document | What it covers |
|---|---|
| [`deployment.md`](deployment.md) | Vercel, PostgreSQL, environment variables, verification. |
| [`beta-operations.md`](beta-operations.md) | Running a controlled Sandbox beta: readiness checklist, signals, incident response. |
| [`development.md`](development.md) | Reproducible local setup and test execution. |
| [`discoverability.md`](discoverability.md) | Repository metadata, topics, and LLM discoverability. |

## Evidence and history

| Document | What it covers |
|---|---|
| [`evidence/README.md`](evidence/README.md) | Verified milestones, releases, test and deployment evidence. |
| [`architecture-decisions/README.md`](architecture-decisions/README.md) | Architecture decision records. |
| [`DEMO_EVIDENCE.md`](DEMO_EVIDENCE.md) | What each demo capture does and does not establish. |
| [`judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md`](judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md) | Preserved verification evidence. |

## Machine-readable index

Structured metadata with source references lives at the repository root in
[`metadata/`](../metadata/project.json):

- [`metadata/project.json`](../metadata/project.json) — identity, stack, status.
- [`metadata/features.json`](../metadata/features.json) — feature inventory.
- [`metadata/api.json`](../metadata/api.json) — endpoint inventory.
- [`metadata/architecture.json`](../metadata/architecture.json) — component map.
- [`metadata/integrations.json`](../metadata/integrations.json) — external services.
- [`metadata/security-controls.json`](../metadata/security-controls.json) — controls.
- [`metadata/evidence.json`](../metadata/evidence.json) — test and deployment evidence.

## Documentation checks

The documentation is validated in CI by
[`.github/workflows/docs.yml`](../.github/workflows/docs.yml):

- every JSON file under `metadata/` parses;
- every relative Markdown link resolves to a file that exists;
- the required root documentation files are present;
- the required `docs/` files are present.

Run the same checks locally:

```bash
python scripts/check_docs.py
```
