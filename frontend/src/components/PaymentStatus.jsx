import { paymentStatus } from '../lib/status'
import { Money } from './primitives'

/**
 * The buyer-facing payment panel: one clear reading of where the money is.
 *
 * This exists because the escrow record (ids and amounts) answers "what PayPal
 * objects exist", which is the auditor's question, not the buyer's. The buyer needs
 * "has my money moved, and what am I waiting on" — and, critically, must never be
 * told a payment succeeded before PayPal confirms a capture. The copy is driven by
 * `paymentStatus`, which is the single place that decides what "settled" means.
 *
 * It follows PACT's existing visual language: the same panel/chip primitives, the
 * held/released/voided tones, and the same restraint about colour.
 */

const TONE = {
  neutral: {
    ring: 'ring-ink-700',
    bg: 'bg-ink-850',
    text: 'text-paper-dim',
    dot: 'bg-paper-faint',
  },
  held: { ring: 'ring-held/40', bg: 'bg-held-dim/40', text: 'text-held', dot: 'bg-held' },
  released: {
    ring: 'ring-released/40',
    bg: 'bg-released-dim/40',
    text: 'text-released',
    dot: 'bg-released',
  },
  voided: { ring: 'ring-voided/40', bg: 'bg-voided-dim/40', text: 'text-voided', dot: 'bg-voided' },
}

const ICON = {
  approve: (
    <path d="M12 2v4M12 18v4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8M2 12h4M18 12h4M4.9 19.1l2.8-2.8M16.3 7.7l2.8-2.8" />
  ),
  held: <path d="M12 2l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V5l7-3z" />,
  released: <path d="M20 6L9 17l-5-5" />,
  voided: <path d="M18 6L6 18M6 6l12 12" />,
  unresolved: <path d="M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z" />,
}

function iconFor(status) {
  if (status.action === 'approve') return ICON.approve
  if (status.action === 'reconcile') return ICON.unresolved
  if (status.tone === 'released') return ICON.released
  if (status.tone === 'voided') return ICON.voided
  return ICON.held
}

export default function PaymentStatus({ job, busy, onApprove, onReconcile, approvalUrl }) {
  const status = paymentStatus(job)
  if (!status) return null
  const tone = TONE[status.tone] ?? TONE.neutral

  return (
    <div className={`panel overflow-hidden ring-1 ${tone.ring}`}>
      <div className={`flex items-start gap-3 px-4 py-3.5 ${tone.bg}`}>
        <span
          className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ${tone.bg} ${tone.text}`}
          aria-hidden="true"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            {iconFor(status)}
          </svg>
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className={`text-sm font-semibold ${tone.text}`}>{status.headline}</h2>
            {status.amount != null && (
              <Money
                value={status.amount}
                currency={status.currency}
                className={`text-sm font-medium ${tone.text}`}
              />
            )}
          </div>
          <p className="mt-1 text-2xs leading-relaxed text-paper-dim">{status.detail}</p>

          {(status.action === 'approve' || status.action === 'reconcile') && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {status.action === 'approve' && approvalUrl && (
                <a
                  href={approvalUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="chip ring-1 bg-paypal-deep/50 text-paypal-bright ring-paypal/40"
                >
                  Open PayPal approval
                </a>
              )}
              <button
                onClick={status.action === 'approve' ? onApprove : onReconcile}
                disabled={busy}
                aria-busy={busy}
                className="chip ring-1 bg-paypal-deep/50 text-paypal-bright ring-paypal/40 disabled:opacity-50"
              >
                {busy
                  ? status.action === 'approve'
                    ? 'Continuing…'
                    : 'Reconciling…'
                  : status.action === 'approve'
                    ? 'I approved — continue'
                    : 'Reconcile with PayPal'}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
