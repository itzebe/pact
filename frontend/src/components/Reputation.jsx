import { Money } from './primitives'

function ScoreBar({ score }) {
  const pct = Math.round(score * 100)
  const tone = score >= 0.7 ? 'bg-released' : score >= 0.45 ? 'bg-held' : 'bg-voided'
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-20 overflow-hidden rounded-full bg-ink-700">
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-2xs tnum text-paper-dim">{score.toFixed(2)}</span>
    </div>
  )
}

/**
 * Reputation, derived only from settled outcomes.
 *
 * The score is a smoothed verified-delivery rate, and it feeds back into seller
 * selection. A seller with a poor record must be materially cheaper to win a
 * job, which is what makes the number economically meaningful rather than
 * decorative.
 */
export default function Reputation({ agents, onPickSeller }) {
  if (!agents?.length) {
    return (
      <p className="px-4 py-6 text-xs text-paper-faint">
        No settlement history yet. Reputation is earned from verified deliveries.
      </p>
    )
  }

  return (
    <ul className="divide-y divide-ink-800">
      {agents.map((a) => (
        <li key={a.agent_id} className="flex items-center justify-between gap-3 px-4 py-2.5">
          <div className="min-w-0">
            <div className="truncate text-xs text-paper">{a.agent_id}</div>
            <div className="mt-0.5 text-2xs text-paper-faint tnum">
              {a.settled} settled · {a.refunded} refunded
              {a.volume > 0 && ` · ${new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(a.volume)}`}
            </div>
          </div>
          <ScoreBar score={a.score} />
        </li>
      ))}
    </ul>
  )
}
