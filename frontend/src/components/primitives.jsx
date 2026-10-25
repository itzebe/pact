import { money, shortHash } from '../lib/api'

const ESCROW_TONE = {
  none: { text: 'text-paper-faint', bg: 'bg-ink-800', ring: 'ring-ink-700', label: 'No escrow' },
  awaiting_approval: {
    text: 'text-held',
    bg: 'bg-held-dim',
    ring: 'ring-held/40',
    label: 'Awaiting approval',
  },
  held: { text: 'text-held', bg: 'bg-held-dim', ring: 'ring-held/40', label: 'Funds held' },
  released: { text: 'text-released', bg: 'bg-released-dim', ring: 'ring-released/40', label: 'Released' },
  voided: { text: 'text-voided', bg: 'bg-voided-dim', ring: 'ring-voided/40', label: 'Voided' },
  refunded: { text: 'text-voided', bg: 'bg-voided-dim', ring: 'ring-voided/40', label: 'Refunded' },
  unresolved: {
    text: 'text-held',
    bg: 'bg-held-dim',
    ring: 'ring-held/40',
    label: 'Unresolved',
  },
}

export function EscrowBadge({ state }) {
  const tone = ESCROW_TONE[state] ?? ESCROW_TONE.none
  return (
    <span className={`chip ring-1 ${tone.bg} ${tone.ring} ${tone.text}`}>
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      {tone.label}
    </span>
  )
}

export function ModeBadge({ mode }) {
  if (!mode) return null
  const simulated = mode === 'mock'
  return (
    <span
      className={`chip ring-1 ${
        simulated ? 'bg-ink-800 text-paper-faint ring-ink-600' : 'bg-paypal-deep/40 text-paypal-bright ring-paypal/40'
      }`}
      title={
        simulated
          ? 'Simulated PayPal: no credentials configured, so the escrow lifecycle runs against an in-memory double.'
          : 'Live PayPal Sandbox: real orders, authorizations and captures on api-m.sandbox.paypal.com.'
      }
    >
      {simulated ? 'Simulated PayPal' : 'PayPal Sandbox'}
    </span>
  )
}

export function StatusPill({ status }) {
  const tones = {
    open: 'bg-ink-800 text-paper-dim ring-ink-600',
    negotiating: 'bg-paypal-deep/40 text-paypal-bright ring-paypal/40',
    agreed: 'bg-paypal-deep/40 text-paypal-bright ring-paypal/40',
    awaiting_approval: 'bg-held-dim text-held ring-held/40',
    escrowed: 'bg-held-dim text-held ring-held/40',
    delivered: 'bg-held-dim text-held ring-held/40',
    verifying: 'bg-held-dim text-held ring-held/40',
    settled: 'bg-released-dim text-released ring-released/40',
    voided: 'bg-voided-dim text-voided ring-voided/40',
    refunded: 'bg-voided-dim text-voided ring-voided/40',
    unresolved: 'bg-held-dim text-held ring-held/40',
    failed: 'bg-voided-dim text-voided ring-voided/40',
  }
  return (
    <span className={`chip ring-1 capitalize ${tones[status] ?? tones.open}`}>
      {status.replace(/_/g, ' ')}
    </span>
  )
}

export function Field({ label, hint, children }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {hint && <span className="ml-2 text-2xs text-paper-faint">{hint}</span>}
      <div className="mt-1.5">{children}</div>
    </label>
  )
}

export function Stat({ label, value, sub, tone = 'text-paper' }) {
  return (
    <div className="rounded-lg border border-ink-700 bg-ink-850 px-3.5 py-3">
      <div className="label">{label}</div>
      <div className={`mt-1 text-lg font-semibold tnum ${tone}`}>{value}</div>
      {sub && <div className="mt-0.5 text-2xs text-paper-faint">{sub}</div>}
    </div>
  )
}

/**
 * The lifecycle as a horizontal stepper. This is the spine of the interface:
 * a judge should be able to read the state of the settlement at a glance.
 */
const STEPS = [
  { key: 'negotiating', label: 'Negotiate', kinds: ['negotiation_started'] },
  { key: 'agreed', label: 'Agree', kinds: ['agreement'] },
  { key: 'escrowed', label: 'Hold escrow', kinds: ['escrow_held'] },
  { key: 'delivered', label: 'Deliver', kinds: ['delivery'] },
  { key: 'verifying', label: 'Verify', kinds: ['verification_result'] },
  { key: 'settled', label: 'Settle', kinds: ['escrow_released', 'escrow_voided'] },
]

export function Lifecycle({ job }) {
  const kinds = new Set((job?.ledger ?? []).map((e) => e.kind))
  const status = job?.status
  const failed = status === 'voided' || status === 'refunded' || status === 'failed'
  // An unresolved settlement is neither a success nor a failure: the run stopped
  // without a confirmed outcome, so the last step must not read as complete.
  const unresolved = status === 'unresolved'
  const reachedIndex = STEPS.reduce((acc, s, i) => (s.kinds.some((k) => kinds.has(k)) ? i : acc), -1)
  const done = status === 'settled' || failed

  return (
    <ol className="flex flex-wrap items-center gap-1.5">
      {STEPS.map((step, i) => {
        const reached = i <= reachedIndex
        const isLast = i === STEPS.length - 1
        const tone = !reached
          ? 'border-ink-700 text-paper-faint'
          : isLast && failed
            ? 'border-voided/50 bg-voided-dim text-voided'
            : isLast && unresolved
              ? 'border-held/50 bg-held-dim text-held'
              : isLast && done
                ? 'border-released/50 bg-released-dim text-released'
                : 'border-paypal/40 bg-paypal-deep/30 text-paypal-bright'
        return (
          <li key={step.key} className="flex items-center gap-1.5">
            <span className={`chip border px-2 py-1 ${tone}`}>
              <span className="tnum opacity-60">{i + 1}</span>
              {step.label}
            </span>
            {!isLast && <span className={`text-2xs ${reached ? 'text-paypal/50' : 'text-ink-600'}`}>›</span>}
          </li>
        )
      })}
    </ol>
  )
}

export function Money({ value, currency = 'USD', className = '' }) {
  return <span className={`tnum ${className}`}>{money(value, currency)}</span>
}

export function Hash({ value, className = '' }) {
  if (!value) return <span className="text-paper-faint">—</span>
  return (
    <span className={`font-mono text-2xs text-paper-dim ${className}`} title={value}>
      {shortHash(value)}
    </span>
  )
}

export function Empty({ title, body }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
      <div className="mb-3 flex h-9 w-9 items-center justify-center rounded-lg border border-ink-700 bg-ink-850 text-paper-faint">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
          <rect x="3" y="4" width="18" height="16" rx="2" />
          <path d="M3 9h18" />
        </svg>
      </div>
      <div className="text-sm font-medium text-paper">{title}</div>
      {body && <div className="mt-1 max-w-sm text-xs leading-relaxed text-paper-faint">{body}</div>}
    </div>
  )
}

export function Spinner({ className = '' }) {
  return (
    <svg className={`animate-spin ${className}`} width="14" height="14" viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  )
}
