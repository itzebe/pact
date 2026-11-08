import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { cpSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = join(here, '..')

// The API host is injected at build time so the same bundle works locally and
// when the frontend is served from a preview URL in a hosted demo.
const target = process.env.VITE_API_PROXY || 'http://localhost:8000'

// Machine-readable project facts must not drift between the repository and the
// site. These files have a single canonical copy at the repository root; this
// serves/copies that exact copy, so the deployed bytes are the same bytes under
// review on GitHub. Duplicating them under `public/` would let the two diverge.
const CANONICAL_FILES = ['llms.txt', 'llms-full.txt', 'project.json']

function serveCanonicalMachineFiles() {
  const readCanonical = (name) => {
    const source = join(repoRoot, name)
    if (!existsSync(source)) {
      throw new Error(`canonical ${name} not found at ${source}`)
    }
    const text = readFileSync(source, 'utf8')
    // Parse the JSON now so a malformed canonical file fails the build rather
    // than shipping an unparseable /project.json.
    if (name.endsWith('.json')) JSON.parse(text)
    return text
  }
  return {
    name: 'pact-canonical-machine-files',
    // Dev and preview: answer the canonical paths from the repository root.
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const path = (req.url || '').split('?')[0]
        const name = path.startsWith('/') ? path.slice(1) : path
        if (CANONICAL_FILES.includes(name)) {
          res.setHeader(
            'Content-Type',
            name.endsWith('.json') ? 'application/json; charset=utf-8' : 'text/plain; charset=utf-8',
          )
          res.end(readCanonical(name))
          return
        }
        next()
      })
    },
    // Build: copy each canonical file into the output verbatim.
    writeBundle(options) {
      const outDir = options.dir || join(here, 'dist')
      mkdirSync(outDir, { recursive: true })
      for (const name of CANONICAL_FILES) {
        cpSync(join(repoRoot, name), join(outDir, name))
      }
    },
  }
}

export default defineConfig(({ command }) => {
  // A build must compile React in production mode regardless of the ambient
  // NODE_ENV. Vite keeps whatever NODE_ENV it is handed, and React's development
  // entry point is selected when NODE_ENV is not exactly "production" — so an
  // environment that happens to export NODE_ENV=development silently ships the
  // dev build: ~2.2x the JavaScript, plus React's development-only warnings and
  // the DevTools hook. Pinning it here makes the artefact the same wherever the
  // build runs (`vite dev` is unaffected — only the build command is forced).
  if (command === 'build') process.env.NODE_ENV = 'production'

  return {
    plugins: [react(), serveCanonicalMachineFiles()],
    server: {
      host: '0.0.0.0',
      port: 5173,
      proxy: { '/api': { target, changeOrigin: true } },
    },
    preview: {
      host: '0.0.0.0',
      port: 4173,
      proxy: { '/api': { target, changeOrigin: true } },
    },
  }
})
