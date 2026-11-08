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
  summarise the project from one file. Their links are GitHub-absolute, so the same
  file reads correctly on GitHub and when served from the live site.
- [`project.json`](../project.json) at the repository root: concise, structured,
  machine-readable project facts (identity, problem, architecture, AI boundaries,
  PayPal integration, security controls, limitations, and the judging-criteria
  evidence). It is the compact sibling of [`metadata/project.json`](../metadata/project.json).
- [`AGENTS.md`](../AGENTS.md) with repository-wide instructions for coding agents.
- A descriptive documentation hierarchy under [`docs/`](README.md), each file with a
  clear `h1` and a place in [`knowledge-map.md`](knowledge-map.md).
- Machine-readable metadata under [`metadata/`](../metadata/project.json), with a
  consistent status vocabulary.
- Consistent product identity ("PACT") and one canonical description across every
  document.
- `scripts/check_docs.py` (run by the Docs CI workflow) validates that every JSON
  metadata file parses and that every GitHub-absolute link in `llms.txt` /
  `llms-full.txt` resolves to a real repository path.

## Website-level machine discovery (applied)

The production site now exposes the same machine-readable surface as the repository.
These were added in the LLM-discoverability change and are served as static files from
the build output (`frontend/public/` plus a small Vite plugin that copies the
canonical root files):

| Path | Content | Source of truth |
|---|---|---|
| `/llms.txt` | Compact LLM entry point | root [`llms.txt`](../llms.txt), copied verbatim |
| `/llms-full.txt` | Consolidated technical reference | root [`llms-full.txt`](../llms-full.txt), copied verbatim |
| `/project.json` | Structured project facts | root [`project.json`](../project.json), copied verbatim |
| `/robots.txt` | Crawler policy and sitemap pointer | [`frontend/public/robots.txt`](../frontend/public/robots.txt) |
| `/sitemap.xml` | Landing page and machine files | [`frontend/public/sitemap.xml`](../frontend/public/sitemap.xml) |

The three machine files are **copied from the repository root at build time** rather
than duplicated, so the deployed bytes are the same bytes under review on GitHub and
the two can never drift. `frontend/index.html` additionally carries a meta
description, a canonical link, Open Graph and Twitter cards, a
`schema.org` `SoftwareApplication` JSON-LD block, and a `<noscript>` fallback that
restates the product explanation in plain HTML so the initial response is meaningful
without JavaScript. `frontend/test/machine-files.test.js` pins all of this.

## LLM discovery: what is and is not guaranteed

- `llms.txt` / `llms-full.txt` are **a convention, not a standard**; there is no
  guarantee that any model or crawler consumes them. They make PACT's authoritative
  information easy to find and parse if a system looks for it.
- Creating these files — in the repository or on the site — does **not** cause
  ChatGPT, Claude, Google, or any other system to index or cite PACT. Indexing depends
  on each system's own crawling and on external references to the repository and site.
- The honest position: these files improve *discoverability*, not *indexation*. No
  indexing is claimed, because none has been verified.

## Owner actions (need a token scope the agent does not have)

- **Repository topics.** `GET`/`PUT /repos/{owner}/{repo}/topics` returned
  `403 Resource not accessible by integration` with the available token, which lacks
  the `administration` scope. The recommended topics below must be applied by the
  owner (Settings → General → Topics), or with a token that has that scope.
- **Devpost copy.** The public Devpost page still lists the backend as "Node.js"; the
  backend is FastAPI. The corrected copy is in
  [`devpost-submission-copy.md`](devpost-submission-copy.md) and must be applied by
  the owner, who holds the Devpost account.

### Recommended topics

```
escrow, paypal, payments, ai-agents, agentic-ai, fintech,
postgresql, fastapi, react, vercel, guest-first, transaction-security
```

Each is accurate to the implementation: the product is an escrow/payments layer on
PayPal, for agents, built with FastAPI + React + PostgreSQL and deployed on Vercel,
with guest-first onboarding and transaction-integrity guarantees. No tag is added
purely for traffic.

## Related

- Documentation index: [`README.md`](README.md)
- Machine-readable identity: [`metadata/project.json`](../metadata/project.json)
- Compact facts (also served at `/project.json`): [`project.json`](../project.json)
