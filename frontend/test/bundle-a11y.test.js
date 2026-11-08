import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

// Two invariants that are easy to break silently and expensive to notice:
//
//  1. AG Grid (the single largest dependency) must stay out of the initial
//     bundle. It is only needed on the Ledger tab, so it is reached through a
//     dynamic import in components/LedgerPanel.jsx. A static import anywhere
//     else pulls ~950 kB back into the entry chunk for every visitor.
//
//  2. ag-grid-community must stay on a version that renders the grid without the
//     axe `aria-required-children` (critical) violation. v33 hardcodes a
//     `tabindex="-1"` on a `role="presentation"` header viewport inside the
//     `role="grid"` element, which has no public option to remove; v36 drops it.
//     Downgrading silently reintroduces the violation.

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = join(here, '..')
const srcRoot = join(frontendRoot, 'src')

function sourceFiles(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) out.push(...sourceFiles(full))
    else if (/\.(js|jsx)$/.test(entry)) out.push(full)
  }
  return out
}

test('ag-grid is not imported statically outside the lazily-loaded Ledger module', () => {
  const offenders = []
  for (const file of sourceFiles(srcRoot)) {
    if (file.endsWith(join('components', 'Ledger.jsx'))) continue
    const text = readFileSync(file, 'utf8')
    // A static `import ... from 'ag-grid...'` keeps the library in the entry chunk.
    if (/^\s*import\s[^\n]*from\s+['"]ag-grid/m.test(text)) {
      offenders.push(file.replace(srcRoot, 'src'))
    }
  }
  assert.deepEqual(offenders, [], `static ag-grid import(s) found: ${offenders.join(', ')}`)
})

test('the Ledger component is reached only through a dynamic import', () => {
  const offenders = []
  for (const file of sourceFiles(srcRoot)) {
    if (file.endsWith(join('components', 'LedgerPanel.jsx'))) continue
    const text = readFileSync(file, 'utf8')
    // `import Ledger from './components/Ledger'` (static) would defeat the split.
    if (/^\s*import\s[^\n]*from\s+['"][^'"]*\/Ledger['"]/m.test(text)) {
      offenders.push(file.replace(srcRoot, 'src'))
    }
  }
  assert.deepEqual(offenders, [], `static Ledger import(s) found: ${offenders.join(', ')}`)

  const panel = readFileSync(join(srcRoot, 'components', 'LedgerPanel.jsx'), 'utf8')
  assert.match(panel, /lazy\(\s*\(\)\s*=>\s*import\(['"]\.\/Ledger['"]\)\s*\)/,
    'LedgerPanel must load Ledger with a dynamic import()')
})

test('ag-grid-community is on a major version without the presentation-tabindex violation', () => {
  const pkg = JSON.parse(readFileSync(join(frontendRoot, 'package.json'), 'utf8'))
  const range = pkg.dependencies['ag-grid-community']
  const major = Number(range.match(/\d+/)?.[0] ?? 0)
  assert.ok(major >= 36,
    `ag-grid-community ${range} reintroduces the axe aria-required-children violation; keep >= 36`)
})

test('the production build pins NODE_ENV so it never ships React development', () => {
  // Vite passes the ambient NODE_ENV through to esbuild/define, and React selects
  // its development entry point unless NODE_ENV is exactly "production". A build
  // run where NODE_ENV=development is exported then emits ~2.2x the JavaScript
  // with React's dev warnings and DevTools hook. vite.config.js pins it for the
  // build command; this fails if that pin is removed.
  const config = readFileSync(join(frontendRoot, 'vite.config.js'), 'utf8')
  assert.match(
    config,
    /command\s*===\s*['"]build['"]\s*\)\s*process\.env\.NODE_ENV\s*=\s*['"]production['"]/,
    'vite.config.js must set NODE_ENV=production for the build command',
  )
})
