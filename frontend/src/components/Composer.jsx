import { useState } from 'react'
import { Field, Spinner } from './primitives'

const PRESETS = [
  {
    id: 'copy',
    label: 'Launch copy',
    title: 'Landing page copy for a fintech launch',
    brief:
      'Write the hero headline, three feature blocks, and a call to action for the launch page of a small business payments product.',
    criteria: [
      'Hero headline is under 12 words',
      'Exactly three feature blocks, each with a heading and one sentence',
      'A single clear call to action',
    ],
    budget: 200,
  },
  {
    id: 'data',
    label: 'Data extraction',
    title: 'Extract a competitor pricing table',
    brief:
      'Produce a structured table of the published pricing tiers for three named competitors, with plan name, monthly price, and included seats.',
    criteria: [
      'One row per competitor plan',
      'Columns for plan name, monthly price and seat count',
      'A source note for each row',
    ],
    budget: 150,
  },
  {
    id: 'audit',
    label: 'Code review',
    title: 'Security review of an authentication module',
    brief:
      'Review a session-based authentication module and report concrete, actionable findings with severity and a suggested fix for each.',
    criteria: [
      'Each finding names the vulnerable line or function',
      'Each finding has a severity rating',
      'Each finding includes a concrete remediation',
    ],
    budget: 320,
  },
]

export default function Composer({ onRun, busy, health }) {
  const [preset, setPreset] = useState(PRESETS[0].id)
  const [title, setTitle] = useState(PRESETS[0].title)
  const [brief, setBrief] = useState(PRESETS[0].brief)
  const [criteria, setCriteria] = useState(PRESETS[0].criteria.join('\n'))
  const [budget, setBudget] = useState(String(PRESETS[0].budget))
  const [seller, setSeller] = useState('')

  const sellers = health?.sellers ?? []

  function applyPreset(id) {
    const p = PRESETS.find((x) => x.id === id)
    if (!p) return
    setPreset(id)
    setTitle(p.title)
    setBrief(p.brief)
    setCriteria(p.criteria.join('\n'))
    setBudget(String(p.budget))
  }

  function submit(event) {
    event.preventDefault()
    const list = criteria
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean)
    if (!title.trim() || !list.length || !(Number(budget) > 0)) return
    onRun({
      title: title.trim(),
      brief: brief.trim(),
      acceptance_criteria: list,
      budget_max: Number(budget),
      seller_ids: seller ? [seller] : null,
    })
  }

  return (
    <form onSubmit={submit} className="panel">
      <div className="panel-head">
        <span className="label">Post a job</span>
        <div className="flex gap-1">
          {PRESETS.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => applyPreset(p.id)}
              className={`chip ring-1 transition-colors ${
                preset === p.id
                  ? 'bg-paypal-deep/40 text-paypal-bright ring-paypal/40'
                  : 'bg-ink-850 text-paper-faint ring-ink-700 hover:text-paper-dim'
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      <div className="space-y-3.5 px-4 py-4">
        <Field label="Deliverable">
          <input className="field" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} />
        </Field>

        <Field label="Brief">
          <textarea
            className="field min-h-[72px] resize-y"
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
            maxLength={4000}
          />
        </Field>

        <Field label="Acceptance criteria" hint="one per line — the verifier checks each">
          <textarea
            className="field min-h-[84px] resize-y font-mono text-2xs leading-relaxed"
            value={criteria}
            onChange={(e) => setCriteria(e.target.value)}
          />
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Budget ceiling" hint={health?.currency ?? 'USD'}>
            <input
              className="field tnum"
              type="number"
              min="1"
              step="0.01"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
            />
          </Field>
          <Field label="Restrict sellers" hint="optional">
            <select className="field" value={seller} onChange={(e) => setSeller(e.target.value)}>
              <option value="">All sellers compete</option>
              {sellers.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <button type="submit" className="btn-primary w-full" disabled={busy}>
          {busy ? (
            <>
              <Spinner />
              Running the lifecycle…
            </>
          ) : (
            'Post job and settle'
          )}
        </button>

        <p className="text-2xs leading-relaxed text-paper-faint">
          Sellers compete on price. Funds are held in PayPal escrow before the work is produced, and released only
          if the delivery passes independent verification.
        </p>
      </div>
    </form>
  )
}
