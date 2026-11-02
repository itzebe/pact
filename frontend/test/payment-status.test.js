import { test } from 'node:test'
import assert from 'node:assert/strict'

import { paymentStatus } from '../src/lib/status.js'

const job = (escrow, extra = {}) => ({
  status: 'escrowed',
  agreed_price: 120,
  currency: 'USD',
  escrow: { amount: 120, currency: 'USD', state: 'none', ...escrow },
  ...extra,
})

test('an awaiting-approval order is never shown as paid', () => {
  const s = paymentStatus(job({ state: 'awaiting_approval', order_id: 'ORDER-1' }))
  assert.equal(s.settled, false)
  assert.equal(s.action, 'approve')
  assert.match(s.headline, /approval/i)
})

test('a held authorization is not a payment', () => {
  const s = paymentStatus(job({ state: 'held', authorization_id: 'AUTH-1' }))
  assert.equal(s.settled, false)
  assert.match(s.headline, /held/i)
})

test('a released escrow with a real capture is settled', () => {
  const s = paymentStatus(job({ state: 'released', mode: 'sandbox', simulated: false, capture_id: 'CAP-1' }))
  assert.equal(s.settled, true)
  assert.match(s.headline, /Paid/i)
})

test('a simulated release is NOT reported as a real payment', () => {
  const s = paymentStatus(job({ state: 'released', mode: 'mock', simulated: true, capture_id: 'MOCK-CAP-1' }))
  assert.equal(s.settled, false, 'a mock capture must never read as paid')
  assert.equal(s.simulated, true)
  assert.match(s.headline, /Simulated/i)
})

test('a voided hold reports nothing charged', () => {
  const s = paymentStatus(job({ state: 'voided' }))
  assert.equal(s.settled, false)
  assert.match(s.headline, /Nothing charged/i)
})

test('a refunded capture is not a success', () => {
  const s = paymentStatus(job({ state: 'refunded', capture_id: 'CAP-1' }))
  assert.equal(s.settled, false)
  assert.match(s.headline, /refunded/i)
})

test('an unresolved outcome asks for reconciliation, not a retry', () => {
  const s = paymentStatus(job({ state: 'unresolved' }))
  assert.equal(s.settled, false)
  assert.equal(s.action, 'reconcile')
  assert.match(s.detail, /reconcil/i)
})

test('no escrow is a neutral state', () => {
  const s = paymentStatus(job({ state: 'none' }))
  assert.equal(s.settled, false)
  assert.equal(s.tone, 'neutral')
})

test('a missing job yields null rather than throwing', () => {
  assert.equal(paymentStatus(null), null)
  assert.equal(paymentStatus(undefined), null)
})

test('the amount falls back to the agreed price when escrow has none', () => {
  const s = paymentStatus({ status: 'open', agreed_price: 250, currency: 'USD', escrow: { state: 'none' } })
  assert.equal(s.amount, 250)
})

test('a settled job never exposes settled=true for a mock, for any state', () => {
  for (const state of ['awaiting_approval', 'held', 'voided', 'refunded', 'unresolved', 'none']) {
    const s = paymentStatus(job({ state, mode: 'mock', simulated: true }))
    assert.equal(s.settled, false, `${state} must not be settled`)
  }
})

test('a partial refund reads as partly refunded, not plain paid', () => {
  const s = paymentStatus(job({ state: 'released', mode: 'sandbox', simulated: false,
    capture_id: 'CAP-1', refunded_amount: 40 }))
  assert.match(s.headline, /partly refunded/i)
  assert.equal(s.settled, true, 'the remaining capture is still real money')
  assert.match(s.detail, /40/)
})

test('a full refund on a released escrow is not shown as partly refunded', () => {
  const s = paymentStatus(job({ state: 'released', mode: 'sandbox', simulated: false,
    capture_id: 'CAP-1', refunded_amount: 120 }))
  assert.doesNotMatch(s.headline, /partly refunded/i)
})
