import { test } from 'node:test'
import assert from 'node:assert/strict'

class MemoryStorage {
  #map = new Map()
  getItem(k) { return this.#map.has(k) ? this.#map.get(k) : null }
  setItem(k, v) { this.#map.set(k, String(v)) }
  removeItem(k) { this.#map.delete(k) }
}

globalThis.localStorage = new MemoryStorage()

const { pollJob } = await import('../src/lib/api.js')

const jobAt = (state) => ({
  id: 'job_1',
  status: state === 'released' ? 'settled' : 'escrowed',
  escrow: { state, amount: 120, currency: 'USD' },
})

test('polling stops as soon as the escrow state is terminal', async () => {
  let calls = 0
  const states = ['held', 'held', 'released']
  globalThis.fetch = async () => {
    const body = jobAt(states[Math.min(calls, states.length - 1)])
    calls += 1
    return { ok: true, status: 200, json: async () => body }
  }
  const seen = []
  const last = await pollJob('job_1', {
    attempts: 6,
    intervalMs: 1,
    isDone: (j) => j.escrow.state === 'released',
    onUpdate: (j) => seen.push(j.escrow.state),
  })
  assert.equal(last.escrow.state, 'released')
  assert.equal(calls, 3, 'must stop once terminal, not burn every attempt')
  assert.deepEqual(seen, ['held', 'held', 'released'])
})

test('polling is bounded when the state never becomes terminal', async () => {
  let calls = 0
  globalThis.fetch = async () => {
    calls += 1
    return { ok: true, status: 200, json: async () => jobAt('held') }
  }
  await pollJob('job_1', { attempts: 3, intervalMs: 1, isDone: (j) => j.escrow.state === 'released' })
  assert.equal(calls, 3, 'a non-terminal state must not poll forever')
})

test('a transient fetch error does not lose the last known reading', async () => {
  let calls = 0
  globalThis.fetch = async () => {
    calls += 1
    if (calls === 2) throw new Error('network blip')
    return { ok: true, status: 200, json: async () => jobAt('held') }
  }
  const last = await pollJob('job_1', { attempts: 3, intervalMs: 1 })
  assert.ok(last, 'a failed poll must not blank the panel')
  assert.equal(last.escrow.state, 'held')
})
