import { test } from 'node:test'
import assert from 'node:assert/strict'

// A localStorage shim, matching the pattern in api.test.js. The module resolves
// its token at call time, so installing the global before the import is enough.
class MemoryStorage {
  #map = new Map()
  getItem(k) { return this.#map.has(k) ? this.#map.get(k) : null }
  setItem(k, v) { this.#map.set(k, String(v)) }
  removeItem(k) { this.#map.delete(k) }
}
globalThis.localStorage = new MemoryStorage()

const { bootstrapSession, endGuestSession, setCsrf, setToken, api, streamJob } =
  await import('../src/lib/api.js')

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body }
}

test('a first visit creates a guest session with no credential supplied', async () => {
  setToken('')
  setCsrf('')
  const calls = []
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options })
    if (options.method === 'POST') {
      return jsonResponse(201, { type: 'guest', subject: 'gst_abc', csrf_token: 'csrf-1' })
    }
    return jsonResponse(401, { detail: 'no session' })
  }

  const session = await bootstrapSession()
  assert.equal(session.type, 'guest')
  assert.equal(calls.length, 2, 'a restore attempt then a create')
  assert.equal(calls[0].options.method ?? 'GET', 'GET')
  assert.equal(calls[1].options.method, 'POST')
  // The cookie must be sent and accepted cross-request.
  assert.equal(calls[1].options.credentials, 'include')
})

test('an existing session is restored without creating another', async () => {
  setToken('')
  setCsrf('')
  const calls = []
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options })
    return jsonResponse(200, { type: 'guest', subject: 'gst_abc', csrf_token: 'csrf-2' })
  }
  const session = await bootstrapSession()
  assert.equal(session.type, 'guest')
  assert.equal(calls.length, 1, 'a live cookie is reused, not replaced')
})

test('a stored operator token skips the guest path entirely', async () => {
  setToken('operator.jwt.value')
  let calls = 0
  globalThis.fetch = async () => {
    calls += 1
    return jsonResponse(200, {})
  }
  const session = await bootstrapSession()
  assert.equal(session.type, 'operator')
  assert.equal(calls, 0)
  setToken('')
})

test('a deployment with guest sessions disabled reports unavailable', async () => {
  setToken('')
  setCsrf('')
  globalThis.fetch = async () => jsonResponse(404, { detail: 'Not Found' })
  const session = await bootstrapSession()
  assert.equal(session.type, 'unavailable')
})

test('the sandbox buyer hint from the server is carried on the session', async () => {
  setToken('')
  setCsrf('')
  const hint = { email: 'sb@personal.example.com', password: 'pw' }
  globalThis.fetch = async () =>
    jsonResponse(200, { type: 'guest', subject: 'gst_x', csrf_token: 'csrf', sandbox_buyer: hint })
  const session = await bootstrapSession()
  assert.deepEqual(session.sandbox_buyer, hint)
})

test('a guest mutation carries the CSRF token; a read does not', async () => {
  setToken('')
  setCsrf('csrf-3')
  const seen = []
  globalThis.fetch = async (url, options = {}) => {
    seen.push({ url, headers: options.headers ?? {} })
    return jsonResponse(200, { jobs: [], agents: [] })
  }
  await api.jobs()
  await api.createJob({ title: 'x' })
  assert.equal(seen[0].headers['X-CSRF-Token'], undefined, 'a GET needs no CSRF token')
  assert.equal(seen[1].headers['X-CSRF-Token'], 'csrf-3')
  assert.equal(seen[1].headers.Authorization, undefined, 'a guest sends no bearer token')
})

test('an operator request is header-authenticated and carries no CSRF token', async () => {
  setToken('operator.jwt.value')
  setCsrf('stale-csrf')
  let headers = null
  globalThis.fetch = async (url, options = {}) => {
    headers = options.headers
    return jsonResponse(200, {})
  }
  await api.createJob({ title: 'x' })
  assert.equal(headers.Authorization, 'Bearer operator.jwt.value')
  assert.equal(headers['X-CSRF-Token'], undefined, 'CSRF applies only to the cookie path')
  setToken('')
})

test('a refund is sent to the refund route with a CSRF token', async () => {
  setToken('')
  setCsrf('csrf-4')
  let seen = null
  globalThis.fetch = async (url, options = {}) => {
    seen = { url, options }
    return jsonResponse(200, { escrow: { state: 'refunded' } })
  }
  await api.refundJob('job_9', { amount: 25 })
  assert.ok(seen.url.endsWith('/api/jobs/job_9/refund'))
  assert.equal(seen.options.method, 'POST')
  assert.equal(seen.options.headers['X-CSRF-Token'], 'csrf-4')
})

test('a guest stream opens without minting a ticket', async () => {
  setToken('')
  setCsrf('csrf-5')
  let ticketRequests = 0
  const urls = []
  globalThis.fetch = async () => {
    ticketRequests += 1
    return jsonResponse(200, { ticket: 'unused' })
  }
  globalThis.EventSource = class {
    constructor(url) { urls.push(url) }
    addEventListener() {}
    close() {}
  }
  const unsubscribe = streamJob('job_42', {})
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(ticketRequests, 0, 'the cookie authenticates the stream; no ticket is needed')
  assert.equal(urls.length, 1)
  assert.ok(urls[0].endsWith('/api/jobs/job_42/stream'), urls[0])
  assert.ok(!urls[0].includes('ticket='), 'no ticket in the guest stream URL')
  unsubscribe()
})

test('ending a session clears the in-memory CSRF token', async () => {
  setToken('')
  setCsrf('csrf-6')
  const methods = []
  globalThis.fetch = async (url, options = {}) => {
    methods.push(options.method ?? 'GET')
    return jsonResponse(204, null)
  }
  await endGuestSession()
  assert.deepEqual(methods, ['POST'])

  let headers = null
  globalThis.fetch = async (url, options = {}) => {
    headers = options.headers ?? {}
    return jsonResponse(200, {})
  }
  await api.createJob({ title: 'x' })
  assert.equal(headers['X-CSRF-Token'], undefined, 'a cleared session sends no stale CSRF token')
})
