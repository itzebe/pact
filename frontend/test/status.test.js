import { test } from 'node:test'
import assert from 'node:assert/strict'

import { displayStatus } from '../src/lib/status.js'

test('a declined authorize awaiting approval reads as awaiting approval, not failed', () => {
  // The production state: the engine records FAILED for a rejected authorize, but
  // the order is intact and the buyer can still approve it.
  assert.equal(displayStatus('failed', 'awaiting_approval'), 'awaiting_approval')
})

test('a genuine failure is left alone', () => {
  assert.equal(displayStatus('failed', 'none'), 'failed')
  assert.equal(displayStatus('failed', 'voided'), 'failed')
})

test('a real awaiting-approval run is unchanged', () => {
  assert.equal(displayStatus('awaiting_approval', 'awaiting_approval'), 'awaiting_approval')
})

test('every other status passes through untouched', () => {
  const passthrough = [
    ['open', 'none'],
    ['negotiating', 'none'],
    ['agreed', 'none'],
    ['escrowed', 'held'],
    ['delivered', 'held'],
    ['verifying', 'held'],
    ['settled', 'released'],
    ['voided', 'voided'],
    ['refunded', 'refunded'],
    ['unresolved', 'unresolved'],
  ]
  for (const [status, escrow] of passthrough) {
    assert.equal(displayStatus(status, escrow), status, `${status}/${escrow} must pass through`)
  }
})

test('negative control: the raw status is what the bug rendered', () => {
  // Without the mapping this is the string that reaches the pill, and the judge
  // sees a red "failed" beside a live "I approved - continue" button.
  const raw = 'failed'
  assert.notEqual(raw, displayStatus('failed', 'awaiting_approval'))
})
