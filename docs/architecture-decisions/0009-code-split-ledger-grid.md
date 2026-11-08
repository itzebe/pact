# ADR 0009 — Code-split the ledger grid and keep AG Grid current

- **Status:** Accepted
- **Date:** 2026-11-08 (beta-readiness pass)

## Context

The frontend shipped as a single JavaScript chunk of about 1.14 MB (326 kB gzip).
AG Grid Community — used by exactly one view, the settlement ledger tab — was the
large majority of it, so every visitor downloaded the grid before the first paint,
whether or not they ever opened the ledger.

Separately, axe-core flagged one **critical** `aria-required-children` violation on
the ledger view. Root cause: AG Grid 33 renders a `role="grid"` element containing a
`role="presentation"` header viewport that carries `tabindex="-1"`. A focusable
element inside a `presentation` role violates the ARIA required-children rule, and
AG Grid 33 exposes no option to remove the attribute (`suppressHeaderFocus` did not).

## Decision

- **Code-split.** Load the ledger grid through a dynamic `import()` inside a small
  wrapper (`frontend/src/components/LedgerPanel.jsx`), reached only when the Ledger
  tab is opened. The wrapper renders a layout-matching skeleton so the first paint
  does not shift.
- **Upgrade AG Grid 33 → 36**, whose DOM omits the offending `tabindex` on the
  presentation viewport. Verified in the browser: sort and resize still work, the
  grid renders every row, and axe reports 0 violations on the ledger view.
- **Pin both invariants with a test** (`frontend/test/bundle-a11y.test.js`): no
  static `ag-grid` import outside the lazy module, the ledger reached only through
  `import()`, and `ag-grid-community` major ≥ 36. The test is dependency-free
  (string/regex over the source and `package.json`), so it adds nothing to install.

## Consequences

- The initial bundle drops from ~1.14 MB to ~190 kB (326 kB → 59 kB gzip); the grid
  becomes its own ~1.26 MB chunk, fetched on demand.
- The ledger view is accessible: 0 axe violations, no loss of keyboard focus (the
  `suppressHeaderFocus` workaround was removed once the upgrade made it unnecessary).
- A future downgrade of AG Grid, or a static import of the grid or of `Ledger`,
  fails the pinned test rather than silently reintroducing the regression.
- The upgrade is a major version bump; it was verified in the running app (job run →
  ledger tab → sort), not only by a green build.

## Alternatives considered

- **Remove the `tabindex` with a DOM shim after render.** Rejected: it mutates a
  third-party component's internals, is invisible to the library, and would break on
  the next AG Grid release without warning.
- **Replace AG Grid with a plain table.** Rejected: it would drop sort/resize and the
  hash-chain status rendering for no accessibility gain over upgrading.
- **Keep AG Grid 33 and document the violation as a known limitation.** Rejected once
  a supported upstream version fixed it; documenting a fixed defect as permanent would
  be inaccurate.
