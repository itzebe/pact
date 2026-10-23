import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API host is injected at build time so the same bundle works locally and
// when the frontend is served from a preview URL in a hosted demo.
const target = process.env.VITE_API_PROXY || 'http://localhost:8000'

export default defineConfig({
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
})
