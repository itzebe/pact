# ADR 0007 — Report the resolved PayPal environment as `is_live`, not only the guard flag

- **Status:** Accepted
- **Date:** 2026-11-07 (PR #30)

## Context

`GET /api/health` is the public, unauthenticated surface an operator (or a judge)
reads to learn what a deployment actually is. It published `paypal.sandbox_only`, the
value of the `PACT_PAYPAL_SANDBOX_ONLY` switch.

That switch is **off by default**. On the production deployment it is unset, so health
reported `paypal.sandbox_only: false` at the same time as `paypal.environment:
"sandbox"`. A reader who took `sandbox_only: false` at face value could conclude the
deployment was pointed at live PayPal, which it was not. The field answers "is the
live-host guard armed?", not "is this process live?" — two different questions, and
the health body gave no field for the second.

## Decision

Add `paypal.is_live` to the health payload: `true` only when the configuration
resolves to PayPal's production host (either `PAYPAL_ENVIRONMENT=live`, or a
`PAYPAL_BASE_URL` override that is not a loopback/sandbox host — the same
`PayPalSettings.is_live` the sandbox guard already uses). Keep `sandbox_only` so the
guard state stays observable, and document the distinction in `docs/api/README.md`,
`docs/security.md` and `docs/faq.md`.

`is_live` is derived, not configured: it cannot drift from the host the client will
actually call, because the client and the field read the same property.

### Follow-up (same PR): fail closed on an unrecognised environment

Writing `is_live` as `environment == "live"` exposed a second ambiguity. The client
only resolves a host for `sandbox` and `live`; any other value — `production`, a typo,
a stray space, an empty string — fell through to the **sandbox** host while `is_live`
reported `false`. That is precisely the failure this ADR is about: an invalid
configuration presented as a safe sandbox one. `is_live` is now `environment !=
"sandbox"` (so an unknown value reads as live), and `validate_runtime` refuses any
`PAYPAL_ENVIRONMENT` outside `{sandbox, live}` at startup, in every environment, unless
an explicit `PAYPAL_BASE_URL` names the host directly. See
`PAYPAL_ENVIRONMENTS` in `backend/pact/config.py`.

## Consequences

- A correct sandbox deployment now reads `environment: "sandbox"`, `is_live: false`,
  `mode: "sandbox"`, `sandbox_only: false` — unambiguous.
- A live deployment would read `is_live: true`, which is the value an operator or
  monitoring probe should alert on.
- The field is a boolean derived from configuration only; it exposes no credential,
  host detail, or secret, so the health surface stays safe to publish.
- Regression coverage is pinned in `tests/test_paypal_sandbox_config.py`
  (`test_sandbox_only_false_does_not_mean_live_money`,
  `test_the_health_surface_reports_whether_it_resolves_to_a_live_host`,
  `test_is_live_tracks_the_configured_environment`,
  `test_an_unrecognised_environment_is_not_presented_as_sandbox`,
  `test_an_unrecognised_environment_is_refused_at_startup`,
  `test_the_two_known_environments_are_accepted`).

## Alternatives considered

- **Rename or repurpose `sandbox_only`.** Rejected: the guard's own state is useful to
  see, and renaming a published field breaks consumers.
- **Default `PACT_PAYPAL_SANDBOX_ONLY` to `true`.** Rejected as the fix here: it would
  change the guard's semantics (the guard is deliberately opt-in per
  ADR 0003), and a status field is still needed for the case where the guard is off.
  Whether to arm the guard in production is a separate, operator-level decision.
