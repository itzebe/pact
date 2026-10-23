import { Money } from './primitives'

function Tick({ passed }) {
  return (
    <span
      className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full ${
        passed ? 'bg-released-dim text-released' : 'bg-voided-dim text-voided'
      }`}
    >
      <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5">
        {passed ? <path d="M20 6L9 17l-5-5" /> : <path d="M18 6L6 18M6 6l12 12" />}
      </svg>
    </span>
  )
}

/**
 * The verification verdict, which is the moment the money moves. Showing the
 * per-criterion evidence is deliberate: the whole claim is that release is
 * conditional on a checked outcome, so the check has to be visible.
 */
export default function Verification({ job }) {
  const v = job?.verification
  if (!v) {
    return <p className="px-4 py-6 text-xs text-paper-faint">The delivery has not been verified yet.</p>
  }

  return (
    <div>
      <div
        className={`flex items-center justify-between border-b px-4 py-3 ${
          v.passed ? 'border-released/25 bg-released-dim/30' : 'border-voided/25 bg-voided-dim/30'
        }`}
      >
        <div className="flex items-center gap-2.5">
          <span
            className={`flex h-6 w-6 items-center justify-center rounded-md ${
              v.passed ? 'bg-released/20 text-released' : 'bg-voided/20 text-voided'
            }`}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
              {v.passed ? <path d="M20 6L9 17l-5-5" /> : <path d="M18 6L6 18M6 6l12 12" />}
            </svg>
          </span>
          <div>
            <div className={`text-sm font-semibold ${v.passed ? 'text-released' : 'text-voided'}`}>
              {v.passed ? 'Verification passed' : 'Verification failed'}
            </div>
            <div className="text-2xs text-paper-faint">
              {v.passed
                ? 'Escrow released to the seller'
                : 'Hold voided — the buyer was charged nothing'}
            </div>
          </div>
        </div>
        <div className="text-right">
          <div className="label">Score</div>
          <div className="text-sm font-semibold tnum text-paper">{v.score.toFixed(2)}</div>
        </div>
      </div>

      <ul className="divide-y divide-ink-800">
        {(v.criteria ?? []).map((c, i) => (
          <li key={i} className="flex gap-3 px-4 py-2.5">
            <Tick passed={c.passed} />
            <div className="min-w-0">
              <div className="text-xs text-paper">{c.criterion}</div>
              <div className="mt-0.5 text-2xs leading-relaxed text-paper-faint">{c.evidence}</div>
            </div>
          </li>
        ))}
      </ul>

      {v.summary && (
        <p className="border-t border-ink-800 px-4 py-3 text-2xs leading-relaxed text-paper-dim">{v.summary}</p>
      )}

      {job.delivery && (
        <details className="border-t border-ink-800 px-4 py-3">
          <summary className="cursor-pointer text-2xs font-medium text-paper-dim hover:text-paper">
            View delivered artifact ({job.delivery.artifact.length.toLocaleString()} characters)
          </summary>
          <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg border border-ink-700 bg-ink-850 p-3 font-mono text-2xs leading-relaxed text-paper-dim">
            {job.delivery.artifact}
          </pre>
        </details>
      )}
    </div>
  )
}

/**
 * The escrow record: the specific PayPal objects that moved the money. These
 * identifiers are what an auditor or a support agent would need, so they are
 * shown verbatim rather than summarised.
 */
export function EscrowRecord({ job }) {
  const e = job?.escrow
  if (!e) return null

  const rows = [
    ['Provider', e.provider === 'paypal' ? 'PayPal Orders v2' : e.provider],
    ['Mode', e.mode === 'mock' ? 'Simulated (no credentials)' : `${e.mode} environment`],
    ['Order', e.order_id],
    ['Authorization', e.authorization_id],
    ['Capture', e.capture_id],
  ].filter(([, v]) => v)

  return (
    <div className="panel">
      <div className="panel-head">
        <span className="label">Escrow record</span>
        <Money value={e.amount} currency={e.currency} className="text-sm font-medium text-paper" />
      </div>
      <dl className="divide-y divide-ink-800">
        {rows.map(([k, v]) => (
          <div key={k} className="flex items-baseline justify-between gap-4 px-4 py-2">
            <dt className="text-2xs text-paper-faint">{k}</dt>
            <dd className="truncate font-mono text-2xs text-paper-dim" title={v}>
              {v}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}
