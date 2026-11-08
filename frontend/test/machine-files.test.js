import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

// The site's machine-readable surface (crawler files, structured data, and the
// no-JavaScript fallback) is easy to break silently: nothing in the app renders
// it, so a broken build still looks fine in a browser. These tests pin the
// plumbing that makes the deployed site readable by search engines and AI
// systems, and that keeps /llms.txt, /llms-full.txt and /project.json identical
// to the canonical repository files.

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = join(here, '..')
const repoRoot = join(frontendRoot, '..')

test('crawler files exist in the frontend public directory', () => {
  for (const name of ['robots.txt', 'sitemap.xml']) {
    assert.ok(
      existsSync(join(frontendRoot, 'public', name)),
      `frontend/public/${name} must exist so it ships in the build output`,
    )
  }
})

test('robots.txt points crawlers at the sitemap and keeps /api out of the index', () => {
  const robots = readFileSync(join(frontendRoot, 'public', 'robots.txt'), 'utf8')
  assert.match(robots, /^Sitemap:\s*https:\/\/pact-flame\.vercel\.app\/sitemap\.xml/m,
    'robots.txt must advertise the absolute sitemap URL')
  assert.match(robots, /^Disallow:\s*\/api\//m,
    'robots.txt must disallow the JSON API, which holds no indexable content')
})

test('sitemap.xml lists the landing page and the three machine files', () => {
  const sitemap = readFileSync(join(frontendRoot, 'public', 'sitemap.xml'), 'utf8')
  for (const path of ['/', '/llms.txt', '/llms-full.txt', '/project.json']) {
    assert.ok(
      sitemap.includes(`https://pact-flame.vercel.app${path}<`),
      `sitemap.xml must list https://pact-flame.vercel.app${path}`,
    )
  }
})

test('index.html carries the discovery metadata, structured data and a no-JS fallback', () => {
  const html = readFileSync(join(frontendRoot, 'index.html'), 'utf8')
  assert.match(html, /<meta\s+name="description"/, 'a meta description is required')
  assert.match(html, /<link\s+rel="canonical"/, 'a canonical link is required')
  assert.match(html, /property="og:title"/, 'an Open Graph title is required')
  // A parseable JSON-LD block, and it must be valid JSON.
  const ld = html.match(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/)
  assert.ok(ld, 'a JSON-LD structured-data block is required')
  const data = JSON.parse(ld[1])
  assert.equal(data['@type'], 'SoftwareApplication')
  assert.match(data.description, /Sandbox|escrow/i)
  // The fallback must exist and be non-trivial, so the raw HTML is meaningful.
  const noscript = html.match(/<noscript>([\s\S]*?)<\/noscript>/)
  assert.ok(noscript, 'a <noscript> fallback is required')
  assert.match(noscript[1], /conditional settlement for the agent economy/i)
  assert.match(noscript[1], /PayPal Sandbox/i)
  assert.ok(noscript[1].length > 1500, 'the fallback must carry the full explanation')
})

test('vite.config.js serves the canonical machine files from the repository root', () => {
  const config = readFileSync(join(frontendRoot, 'vite.config.js'), 'utf8')
  assert.match(config, /const CANONICAL_FILES = \['llms\.txt', 'llms-full\.txt', 'project\.json'\]/,
    'the canonical file list must name the three machine files')
  assert.match(config, /serveCanonicalMachineFiles\(\)/,
    'the canonical-files plugin must be registered')
  // The canonical copies must exist, or the build would ship nothing.
  for (const name of ['llms.txt', 'llms-full.txt', 'project.json']) {
    assert.ok(existsSync(join(repoRoot, name)), `${name} must exist at the repository root`)
  }
})

test('the root project.json is valid JSON with the required identity fields', () => {
  const project = JSON.parse(readFileSync(join(repoRoot, 'project.json'), 'utf8'))
  for (const field of ['name', 'one_line', 'description', 'repository_url', 'demo_url', 'license']) {
    assert.ok(project[field], `project.json must define "${field}"`)
  }
  assert.equal(project.payment_model.real_money, false,
    'project.json must state that no real money moves')
})
