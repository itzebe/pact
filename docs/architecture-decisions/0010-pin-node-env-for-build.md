# ADR 0010 — Pin `NODE_ENV=production` for the frontend build

- **Status:** Accepted
- **Date:** 2026-11-07 (final beta-readiness review)

## Context

`frontend/package.json` builds with `vite build`. Vite passes the ambient
`NODE_ENV` through to esbuild and to the `define` substitution, and React chooses
its entry point by `NODE_ENV`: `react/index.js` re-exports `react.production.js`
only when `NODE_ENV === 'production'`, and otherwise `react.development.js`.
`react-dom` and `scheduler` behave the same way.

Nothing in the repository sets `NODE_ENV` for a build. `npm run build`, the CI
"Build the production bundle" step, and the Docker/Vercel builds all invoke
`vite build` with whatever `NODE_ENV` the surrounding environment happens to
export. That is fine when it is unset (Vite defaults the *build* to production),
but a shell, a CI runner, or a project variable that exports `NODE_ENV=development`
silently switches the artefact:

| `NODE_ENV` at build | Initial chunk | gzip | React build |
|---|---|---|---|
| unset / `production` | 190.33 kB | 59.49 kB | production |
| `development` | 411.21 kB | 116.0 kB | development (DevTools hook + dev warnings) |

The failure is silent: the build exits 0, the bundle is valid, the app runs, and
no test failed — the tests run on source, not on the built artefact. The cost is
~2.2x the JavaScript on the first paint plus React's development-only code paths
in production. The build was reproduced both ways in the workspace container
(Node 24.22.1, Vite 6.4.5) while writing this record.

## Decision

`frontend/vite.config.js` exports a config *function* and, for the `build`
command only, sets `process.env.NODE_ENV = 'production'` before returning the
config. `vite dev` and `vite preview` are untouched, so a developer who exports
`NODE_ENV=development` still gets a development server; only a build is forced.

Pinned by `frontend/test/bundle-a11y.test.js` (a dependency-free assertion that
`vite.config.js` sets the variable for the build command).

## Consequences

- A build produces the same artefact wherever it runs, regardless of an ambient
  `NODE_ENV`. The bundle can no longer silently ship React development.
- The pin is at the tool boundary, not in a caller: every current and future
  build entry point (`npm run build`, Docker, Vercel, CI) is covered without
  editing each one.
- A test now fails if the pin is removed, so the regression cannot return
  unnoticed.

## Alternatives considered

- **Set `NODE_ENV=production` in each caller** (`package.json` script, Dockerfile,
  `vercel.json`, CI). Rejected: four places to keep in sync, and a new caller can
  forget it — the failure is silent, so it would not be noticed.
- **`vite build --mode production`.** Rejected: does not help. Vite's `mode` is
  independent of `NODE_ENV`; with `NODE_ENV=development` exported, `--mode
  production` still emitted the development React (verified).
- **Assert on the built bundle in CI** (fail if the DevTools marker is present).
  Not adopted as the fix, because the pin is cheaper and prevents the artefact
  rather than detecting it. The source-level test pins the pin instead.
