import { test } from 'node:test'
import assert from 'node:assert/strict'

import tailwind from '../tailwind.config.js'

// WCAG 2.2 SC 1.4.3: normal body text needs >= 4.5:1. The palette must clear that
// on every surface the text is actually placed on. This pins the fix for the
// measured defect: `paper.faint` was #6b7488, which scored 3.67–4.25:1 and failed
// an axe audit on ~30 nodes. A regression here silently reintroduces the failure.

const { ink, paper } = tailwind.theme.extend.colors

const SURFACES = {
  'ink-950': ink[950],
  'ink-900': ink[900],
  'ink-850': ink[850],
  'ink-800': ink[800],
}

function relativeLuminance(hex) {
  const c = hex.replace('#', '')
  const [r, g, b] = [0, 2, 4]
    .map((i) => parseInt(c.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(fg, bg) {
  const a = relativeLuminance(fg)
  const b = relativeLuminance(bg)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

test('body text tokens clear WCAG AA (4.5:1) on every surface', () => {
  const text = { paper: paper.DEFAULT, dim: paper.dim, faint: paper.faint }
  for (const [name, fg] of Object.entries(text)) {
    for (const [sName, bg] of Object.entries(SURFACES)) {
      const ratio = contrast(fg, bg)
      assert.ok(
        ratio >= 4.5,
        `${name} on ${sName} is ${ratio.toFixed(2)}:1 — below the 4.5:1 minimum`,
      )
    }
  }
})

test('the three-step text hierarchy is preserved (faint is distinct from dim)', () => {
  // Fixing contrast must not collapse the hierarchy into one flat tone.
  assert.notEqual(paper.faint, paper.dim)
  assert.ok(
    relativeLuminance(paper.faint) < relativeLuminance(paper.dim),
    'faint must stay dimmer than dim',
  )
})

test('negative control: the previous faint value fails, so the test would catch a revert', () => {
  const previousFaint = '#6b7488'
  const ratio = contrast(previousFaint, ink[900])
  assert.ok(ratio < 4.5, `expected the old #6b7488 to fail; it scored ${ratio.toFixed(2)}:1`)
})
