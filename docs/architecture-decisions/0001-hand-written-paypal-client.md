# ADR 0001 — Hand-write the PayPal escrow client

- **Status:** Accepted
- **Date:** 2026-10 (initial build)

## Context

PACT's whole premise is *conditional* settlement: hold the buyer's funds, release them
only when a delivery passes verification, and return them otherwise. That requires
PayPal `Orders v2` with `intent=AUTHORIZE`, then a genuine two-way branch — `capture`
to release, `void` to return.

PayPal publishes an official **Agent Toolkit** for agent payments. The natural first
move would be to use it.

## Decision

Hand-write a small, purpose-built escrow client directly on PayPal's REST API
(`backend/pact/paypal/escrow.py`) rather than depend on the official toolkit.

## Rationale

The official toolkit **cannot express this product**. Its order payload builder
hard-codes `"intent": "CAPTURE"` (`python/paypal_agent_toolkit/shared/orders/payload_util.py`),
so it only performs immediate payment. There is no authorize call, no void, and
therefore no way to make settlement conditional on an outcome. Escrow required
building directly on the REST API.

## Consequences

- **Positive.** Full control of the authorize/capture/void lifecycle; the client is
  pinned to PayPal's published OpenAPI enums and the tests assert against those values
  rather than against assumptions; every call is idempotent via `PayPal-Request-Id`.
- **Negative.** PACT owns the client's correctness and must track PayPal API changes.
  This is mitigated by pinning behaviour to the published specification and by tests
  that exercise request construction, response parsing, and error classification
  through the HTTP layer (only the network boundary is stubbed).
- **Neutral.** A mock double with the same state machine (`paypal/mock.py`) lets the
  product run with no credentials, so the settlement logic under test is the real
  logic.

## Alternatives considered

- **Official Agent Toolkit.** Rejected: it hard-codes `intent=CAPTURE` and offers no
  void, so it cannot do escrow.
- **A third-party PayPal SDK.** Rejected: an additional supply-chain dependency for a
  narrow set of endpoints, and the same intent limitation applies to a CAPTURE-only
  wrapper.
