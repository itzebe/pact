import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

// `Cross-Origin-Opener-Policy: same-origin` severs the browsing-context group: a
// window this app opens with `window.open` no longer shares `window.opener`, and
// the new window can no longer post messages back. That is exactly the mechanism a
// popup-based PayPal integration relies on, so COOP is only safe here while the
// approval flow is a top-level redirect with no cross-window channel.
//
// These tests pin that invariant. If a future change introduces the PayPal JS SDK
// popup flow, `window.open`, or an `opener.postMessage` handshake, it fails here —
// which is the signal that COOP has to be revisited, not that the test is wrong.
//
// Verified behaviour (2026-11): the approval link is a plain anchor opened in a
// new tab with `rel="noreferrer"` (implicitly noopener), and the return from
// PayPal is handled purely from the `?job=` query string in App.jsx.

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = join(here, '..')
const srcRoot = join(frontendRoot, 'src')
const repoRoot = join(frontendRoot, '..')

function sourceFiles(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) out.push(...sourceFiles(full))
    else if (/\.(js|jsx)$/.test(entry)) out.push(full)
  }
  return out
}

test('the app never depends on a cross-window channel that COOP would sever', () => {
  // A window.opener reference, a postMessage handshake, or a window.open popup all
  // need the browsing-context group COOP breaks. None may exist while COOP is set.
  const patterns = [
    /window\.opener/,
    /\bpostMessage\b/,
    /window\.open\s*\(/,
  ]
  const offenders = []
  for (const file of sourceFiles(srcRoot)) {
    const text = readFileSync(file, 'utf8')
    if (patterns.some((re) => re.test(text))) offenders.push(file.replace(srcRoot, 'src'))
  }
  assert.deepEqual(
    offenders,
    [],
    `cross-window dependency found (revisit COOP): ${offenders.join(', ')}`,
  )
})

test('the PayPal approval link is a redirect anchor, not a scripted popup', () => {
  const panel = readFileSync(join(srcRoot, 'components', 'PaymentStatus.jsx'), 'utf8')
  // An anchor to the provider's approval_url is a top-level navigation. `noreferrer`
  // also implies `noopener`, so the opened tab cannot reach back into this one even
  // without COOP — the two protections agree rather than conflict.
  assert.match(
    panel,
    /href=\{approvalUrl\}/,
    'the approval link must point at the provider approval_url',
  )
  assert.match(panel, /target="_blank"/, 'the approval link opens in a new tab')
  assert.match(panel, /rel="noreferrer"/, 'the approval link must not hand the tab a referrer/opener')
  assert.doesNotMatch(panel, /window\.open/, 'the approval must not use a scripted popup')
})

test('the return from PayPal is handled from the query string, not from an opener', () => {
  const app = readFileSync(join(srcRoot, 'App.jsx'), 'utf8')
  // The order's return_url carries ?job=<id>; the app reads it on load. No message
  // is expected from the PayPal window, so nothing is lost when COOP isolates it.
  assert.match(app, /new URLSearchParams\(window\.location\.search\)/, 'the return is query-driven')
  assert.match(app, /params\.get\('job'\)/, 'the return reads the job id from the query')
})

test('the PayPal JS SDK is not loaded (there is no popup integration to break)', () => {
  const sdk = []
  for (const file of sourceFiles(srcRoot)) {
    const text = readFileSync(file, 'utf8')
    if (/paypal\.com\/sdk|paypalobjects\.com/.test(text)) sdk.push(file.replace(srcRoot, 'src'))
  }
  assert.deepEqual(sdk, [], `PayPal JS SDK referenced in: ${sdk.join(', ')}`)
})

test('the deployment actually sets COOP same-origin on SPA responses', () => {
  // The invariant above is only meaningful if COOP is really on. This reads the
  // Vercel SPA header rule, which is the header a browser receives for the app.
  const vercel = JSON.parse(readFileSync(join(repoRoot, 'vercel.json'), 'utf8'))
  const spaRule = (vercel.headers ?? []).find((rule) => rule.source === '/((?!api/).*)')
  assert.ok(spaRule, 'a SPA header rule must exist in vercel.json')
  const coop = spaRule.headers.find((h) => h.key === 'Cross-Origin-Opener-Policy')
  assert.equal(coop?.value, 'same-origin', 'the SPA must send COOP: same-origin')
})
