# ADR 0002 — A per-job SHA-256 hash-chained audit ledger

- **Status:** Accepted
- **Date:** 2026-10 (initial build)

## Context

PACT's output is meant to be defensible as a *financial record*, not an application
log. A settlement is a claim that money moved (or did not), with a reason. If the
record could be edited after the fact without detection, it would be worth little to
an auditor or to a party to a dispute.

## Decision

Every state transition appends a `LedgerEntry` to the job's ledger. Each entry stores
the SHA-256 hash of the previous entry and its own hash, computed over a canonical
JSON serialisation of its fields. `Job.verify_chain()` recomputes the chain, and
`GET /api/jobs/{id}/ledger` returns `chain_valid`.

## Rationale

A hash chain makes the record **tamper-evident**: editing any historical row changes
its hash and breaks every subsequent link. Recomputing the chain is cheap and can be
done by anyone with the record, so the guarantee does not depend on trusting the
server.

## Consequences

- **Positive.** The ledger is self-verifying and the UI verifies it live. Each entry
  names the PayPal order, authorization, capture, and refund ids and the reason for
  the transition, so the record explains *why* money moved.
- **Negative.** The ledger grows with every transition (bounded in practice by the
  short lifecycle). Entries must be appended in order and never mutated, which is a
  discipline the code enforces by construction.
- **Neutral.** The chain proves integrity, not authenticity; it does not replace the
  provider's own records, which reconciliation reads directly.

## Alternatives considered

- **An ordinary append-only log.** Rejected: append-only by convention, not by
  mathematics; a silent edit would be undetectable.
- **A blockchain or a signed Merkle tree.** Rejected as disproportionate: a single
  hash chain over a per-job record gives the needed guarantee with no external
  dependency.
