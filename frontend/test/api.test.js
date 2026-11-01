import { test } from 'node:test'
import assert from 'node:assert/strict'

// A minimal localStorage + import.meta shim so the token resolution logic can be
// exercised outside Vite. The module reads `import.meta.env` at call time, so
// installing a global is enough.
class MemoryStorage {
  #map = new Map()
  getItem(k) {
    return this.#map.has(k) ? this.#map.get(k) : null
  }
  setItem(k, v) {
    this.#map.set(k, String(v))
  }
  removeItem(k) {
    this.#map.delete(k)
  }
}

globalThis.localStorage = new MemoryStorage()
globalThis.import = globalThis.import || {}

const { getToken, setToken, streamJob } = await import('../src/lib/api.js')

test('no token configured resolves to an empty string', () => {
  localStorage.removeItem('pact.token')
  assert.equal(getToken(), '')
})

test('a pasted token is stored and returned', () => {
  setToken('header.payload.signature')
  assert.equal(getToken(), 'header.payload.signature')
})

test('clearing the token removes it', () => {
  setToken('header.payload.signature')
  setToken('')
  assert.equal(getToken(), '')
})

test('a pasted token wins over any build-time default', () => {
  setToken('pasted.token.value')
  assert.equal(getToken(), 'pasted.token.value')
})

test('the stream URL carries a minted ticket, never the bearer token', async () => {
  // A real EventSource cannot set an Authorization header, so the credential has
  // to travel in the URL. It must be a short-lived ticket, not the real token.
  setToken('the-real-bearer-token')
  const urls = []
  let ticketRequests = 0

  globalThis.fetch = async (url, options) => {
    ticketRequests += 1
    assert.equal(options.headers.Authorization, 'Bearer the-real-bearer-token')
    return { ok: true, status: 200, json: async () => ({ ticket: 'short-lived-ticket', expires_in: 60 }) }
  }
  globalThis.EventSource = class {
    constructor(url) {
      urls.push(url)
    }
    addEventListener() {}
    close() {}
  }

  const unsubscribe = streamJob('job_123', {})
  await new Promise((resolve) => setTimeout(resolve, 0))

  assert.equal(ticketRequests, 1)
  assert.equal(urls.length, 1)
  assert.ok(urls[0].includes('ticket=short-lived-ticket'), urls[0])
  assert.ok(!urls[0].includes('the-real-bearer-token'), 'the real token must not be in the URL')
  unsubscribe()
})

test('a ticket is requested once even if the caller unsubscribes immediately', async () => {
  setToken('the-real-bearer-token')
  let opened = 0
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ ticket: 'short-lived-ticket', expires_in: 60 }),
  })
  globalThis.EventSource = class {
    constructor() {
      opened += 1
    }
    addEventListener() {}
    close() {}
  }

  const unsubscribe = streamJob('job_123', {})
  unsubscribe()
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(opened, 0, 'an unsubscribe before the ticket resolves must not open a stream')
})
