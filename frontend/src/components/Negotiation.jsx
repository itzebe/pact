import { Money } from './primitives'

const ROLE = {
  buyer: { name: 'Buyer agent', tone: 'text-paypal-bright', border: 'border-paypal/30', bg: 'bg-paypal-deep/20' },
  seller: { name: 'Seller agent', tone: 'text-held', border: 'border-held/25', bg: 'bg-held-dim/40' },
}

/**
 * The negotiation as a round-by-round transcript. Each round is a block so the
 * reader can see the price move, which is the part that makes the agents feel
 * like they are actually bargaining rather than emitting one number.
 */
export default function Negotiation({ job }) {
  const messages = job?.messages ?? []
  if (!messages.length) {
    return (
      <p className="px-4 py-6 text-xs text-paper-faint">
        No negotiation yet. Run the job to invite sellers to quote.
      </p>
    )
  }

  const rounds = [...new Set(messages.map((m) => m.round))].sort((a, b) => a - b)
  const budget = Number(job.budget_max)

  return (
    <div className="divide-y divide-ink-800">
      {rounds.map((round) => {
        const inRound = messages.filter((m) => m.round === round)
        return (
          <div key={round} className="px-4 py-3">
            <div className="mb-2.5 flex items-center gap-2">
              <span className="label">Round {round}</span>
              <span className="rule flex-1" />
              <span className="text-2xs text-paper-faint tnum">
                budget {new Intl.NumberFormat('en-US', { style: 'currency', currency: job.currency }).format(budget)}
              </span>
            </div>
            <ul className="space-y-2">
              {inRound.map((m, i) => {
                const role = ROLE[m.role] ?? ROLE.seller
                const isBuyer = m.role === 'buyer'
                const accepted = isBuyer && /^Accepted/.test(m.text)
                return (
                  <li
                    key={`${round}-${i}`}
                    className={`enter flex gap-3 rounded-lg border ${role.border} ${role.bg} px-3 py-2`}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-2">
                        <span className={`text-2xs font-medium ${role.tone}`}>
                          {isBuyer ? role.name : m.agent_id}
                        </span>
                        {accepted && (
                          <span className="chip bg-released-dim text-released ring-1 ring-released/40">accepted</span>
                        )}
                      </div>
                      <div className="mt-0.5 text-sm text-paper">{m.text}</div>
                      {m.rationale && (
                        <div className="mt-1 text-2xs leading-relaxed text-paper-faint">{m.rationale}</div>
                      )}
                    </div>
                    {m.offer != null && (
                      <div className="shrink-0 text-right">
                        <div className="label">{isBuyer ? 'counter' : 'ask'}</div>
                        <Money value={m.offer} currency={job.currency} className="text-sm text-paper" />
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          </div>
        )
      })}
    </div>
  )
}
