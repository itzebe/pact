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
