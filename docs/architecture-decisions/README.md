# Architecture decisions

Architecture decision records (ADRs) for PACT. Each record captures a decision that
shaped the system, the context that forced it, and the consequences. They are concise
and grounded in the code and the project's own history.

| ADR | Decision |
|---|---|
| [0001](0001-hand-written-paypal-client.md) | Hand-write the PayPal escrow client instead of using the official toolkit |
| [0002](0002-hash-chained-audit-ledger.md) | Make the audit ledger a per-job SHA-256 hash chain |
| [0003](0003-fail-closed-configuration.md) | Fail closed on unsafe configuration rather than degrade |
| [0004](0004-session-advisory-lock-on-direct-endpoint.md) | Serialise settlement with a session advisory lock on a direct endpoint |
| [0005](0005-exactly-once-webhooks.md) | Process webhooks exactly once in a shared store |
| [0006](0006-guest-first-zero-friction.md) | Guest-first onboarding as the default public experience |
| [0007](0007-health-reports-is-live.md) | Report the resolved PayPal environment as `is_live`, not only the guard flag |
| [0008](0008-application-level-security-headers.md) | Attach hardening headers in the application, not only at the edge |
| [0009](0009-code-split-ledger-grid.md) | Code-split the ledger grid and keep AG Grid current |

## Format

Each ADR has: **Status**, **Context**, **Decision**, **Consequences**, and where
relevant **Alternatives considered**. Status is one of *Accepted*, *Superseded*, or
*Deprecated*.
