# PACT — GitHub discoverability

This document records the repository settings that improve public discovery, what has
been applied, and what a maintainer must apply manually. It distinguishes an
**applied** change from a **recommended** one; nothing here claims guaranteed
indexing by any search engine or LLM.

## Repository metadata

| Setting | Value | Status |
|---|---|---|
| Description | "Conditional settlement infrastructure for the agent economy, using PayPal authorization, independent verification, and reputation-aware market selection." | Applied |
| Homepage | <https://pact-flame.vercel.app> | Applied |
| License | MIT | Applied |
| Visibility | Public | Applied |
| Topics | see below | **Recommended** (needs `administration` scope on the token) |

### Recommended topics

Add these in **Settings → General → Topics** (or with a token that has the
`administration` scope):

```
escrow, payments, paypal, agentic-ai, ai-agents, fintech,
postgresql, fastapi, react, vercel, guest-first, transaction-security
```

Each is accurate to the implementation: the product is an escrow/payments layer on
PayPal, for agents, built with FastAPI + React + PostgreSQL and deployed on Vercel,
with guest-first onboarding and transaction-integrity guarantees. No tag is added
purely for traffic.

## In-repository discoverability

Applied:

- A root [`README.md`](../README.md) that explains PACT in the first screen, with a
  documentation map and working relative links.
- [`llms.txt`](../llms.txt) and [`llms-full.txt`](../llms-full.txt) at the repository
  root (the [llms.txt](https://llmstxt.org/) convention) so an AI system can find and
  summarise the project from one file.
- [`AGENTS.md`](../AGENTS.md) with repository-wide instructions for coding agents.
- A descriptive documentation hierarchy under [`docs/`](README.md), each file with a
  clear `h1` and a place in [`knowledge-map.md`](knowledge-map.md).
- Machine-readable metadata under [`metadata/`](../metadata/project.json), with a
  consistent status vocabulary.
- Consistent product identity ("PACT") and one canonical description across every
  document.

## LLM discovery: what is and is not guaranteed

- `llms.txt` / `llms-full.txt` are **a convention, not a standard**; there is no
  guarantee that any model or crawler consumes them. They make PACT's authoritative
  information easy to find and parse if a system looks for it.
- Creating these files does **not** cause ChatGPT, Claude, Google, or any other system
  to index or cite PACT. Indexing depends on each system's own crawling and on
  external references to the repository.
- The honest position: these files improve *discoverability*, not *indexation*.

## Optional website-level indexing (not applied)

The mission scope is the GitHub repository; the production website was not modified.
If the maintainer wants website-level machine discovery, the following are worth
considering as a separate, explicitly authorised change:

- `https://pact-flame.vercel.app/llms.txt` served with `Content-Type: text/plain`.
- `robots.txt` and a `sitemap.xml`.
- `schema.org` structured data (JSON-LD) on the landing page describing the software
  application.
- An `OpenAPI` document published at a stable URL (FastAPI already serves `/openapi.json`).

None of these are implemented here.

## Related

- Documentation index: [`README.md`](README.md)
- Machine-readable identity: [`metadata/project.json`](../metadata/project.json)
