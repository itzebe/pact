/**
 * Presentation-layer status resolution.
 *
 * The engine records a declined authorize as FAILED so a rejected attempt is never
 * mistaken for a hold. But when the rejection is `ORDER_NOT_APPROVED` the order is
 * still valid and the buyer can still approve it, so the escrow stays
 * AWAITING_APPROVAL (see `engine.approve_and_hold`). That pair is the only way
 * `failed` and `awaiting_approval` coexist, and it is not terminal: the run is
 * waiting on the buyer, not broken.
 *
 * Rendering the raw status put a red "failed" pill beside a live "Buyer approval
 * required" button, which reads as a demo that broke rather than one that is
 * paused. This maps that one combination to what it actually is.
 */
export function displayStatus(status, escrowState) {
  if (status === 'failed' && escrowState === 'awaiting_approval') {
    return 'awaiting_approval'
  }
  return status
}

/**
 * A single, honest reading of the payment state for the buyer.
 *
 * The whole product promise is that a payment never *looks* settled before PayPal
 * confirms a capture, so this is deliberately conservative: `settled` requires an
 * escrow state of `released`, and a mock capture is labelled as simulated rather
 * than presented as money. Everything ambiguous resolves to a state that asks for
 * a human (reconcile) rather than claiming success.
 *
 * Pure and data-in/data-out so it can be tested without a DOM.
 */
export function paymentStatus(job) {
  if (!job) return null
  const escrow = job.escrow ?? {}
  const state = escrow.state ?? 'none'
  const simulated = escrow.simulated === true || escrow.mode === 'mock'
  const amount = escrow.amount ?? job.agreed_price
  const currency = escrow.currency ?? job.currency ?? 'USD'
  const refunded = Number(escrow.refunded_amount ?? 0)

  const base = { simulated, amount, currency, orderId: escrow.order_id || '', captureId: escrow.capture_id || '' }

  switch (state) {
    case 'awaiting_approval':
      return {
        ...base,
        tone: 'held',
        headline: 'Waiting for your approval in PayPal',
        detail:
          'The order is created but nothing is held and no work starts until you approve it. Returning to this page does not by itself settle anything.',
        settled: false,
        action: 'approve',
      }
    case 'held':
      return {
        ...base,
        tone: 'held',
        headline: 'Payment held in escrow',
        detail:
          'PayPal has authorized the amount. The seller is committed; you have not been charged. It is captured only if the delivery passes verification.',
        settled: false,
        action: null,
      }
    case 'released':
      // A partial refund leaves the escrow `released`: some money moved back but the
      // capture is not fully reversed. Say so, rather than showing a plain "Paid".
      if (refunded > 0 && refunded < amount) {
        return {
          ...base,
          tone: 'released',
          headline: 'Partly refunded',
          detail: `${refunded} of ${amount} ${currency} has been refunded to the buyer; the seller keeps the remainder of the captured payment.`,
          settled: !simulated,
          action: null,
        }
      }
      return {
        ...base,
        tone: 'released',
        headline: simulated ? 'Simulated capture (no real payment)' : 'Paid — captured from escrow',
        detail: simulated
          ? 'This deployment ran against the in-memory PayPal double, so no money moved. It is shown as simulated on purpose.'
          : 'PayPal confirmed the capture. The seller has been paid from the authorized funds.',
        settled: !simulated,
        action: null,
      }
    case 'refunded':
      return {
        ...base,
        tone: 'voided',
        headline: 'Payment refunded',
        detail: 'The captured payment was reversed. The buyer has been made whole.',
        settled: false,
        action: null,
      }
    case 'voided':
      return {
        ...base,
        tone: 'voided',
        headline: 'Nothing charged',
        detail: 'The hold was released without a capture. The buyer was charged nothing.',
        settled: false,
        action: null,
      }
    case 'unresolved':
      return {
        ...base,
        tone: 'held',
        headline: 'Outcome unknown — needs reconciliation',
        detail:
          'PayPal did not confirm a terminal outcome, so this is recorded as neither paid nor returned. A retry could capture twice, so it must be reconciled against PayPal.',
        settled: false,
        action: 'reconcile',
      }
    case 'none':
    default:
      return {
        ...base,
        tone: 'neutral',
        headline: 'No payment yet',
        detail: 'No escrow has been opened for this settlement.',
        settled: false,
        action: null,
      }
  }
}
