import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API host is injected at build time so the same bundle works locally and
// when the frontend is served from a preview URL in a hosted demo.
const target = process.env.VITE_API_PROXY || 'http://localhost:8000'

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
    plugins: [react()],
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
