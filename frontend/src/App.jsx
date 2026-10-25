import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, money, streamJob } from './lib/api'
import { displayStatus } from './lib/status'
import {
  Empty,
  EscrowBadge,
  Lifecycle,
  ModeBadge,
  Money,
  Stat,
  StatusPill,
} from './components/primitives'
import Composer from './components/Composer'
import Negotiation from './components/Negotiation'
import Verification, { EscrowRecord } from './components/Verification'
import Ledger from './components/Ledger'
import Reputation from './components/Reputation'

const TABS = [
  { id: 'negotiation', label: 'Negotiation' },
  { id: 'verification', label: 'Verification' },
  { id: 'ledger', label: 'Ledger' },
]

export default function App() {
  const [health, setHealth] = useState(null)
  const [jobs, setJobs] = useState([])
  const [job, setJob] = useState(null)
  const [reputation, setReputation] = useState([])
  const [tab, setTab] = useState('negotiation')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [live, setLive] = useState(false)
  const [liveEntries, setLiveEntries] = useState([])
  const unsubRef = useRef(null)

  const refreshLists = useCallback(async () => {
    try {
      const [j, r] = await Promise.all([api.jobs(), api.reputation()])
      setJobs(j.jobs)
      setReputation(r.agents)
    } catch (e) {
      setError(e.message)
    }
  }, [])

  useEffect(() => {
    ;(async () => {
      try {
        setHealth(await api.health())
        await refreshLists()
      } catch (e) {
        setError(e.message)
      }
    })()
  }, [refreshLists])

  // Tear the stream down on unmount so a closed tab cannot leak a connection.
  useEffect(() => () => unsubRef.current?.(), [])

  const openJob = useCallback(
    async (id) => {
      unsubRef.current?.()
      try {
        const full = await api.job(id)
        setJob(full)
        setLiveEntries(full.ledger ?? [])
        setTab('negotiation')
      } catch (e) {
        setError(e.message)
      }
    },
    [],
  )

  // Returning from PayPal. The order's return_url carries ?job=<id>, so the buyer
  // lands back on their own settlement with the "I approved — continue" action in
  // reach, rather than on an empty page having to find the job again. The query is
  // cleared so a reload does not re-trigger this.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const returned = params.get('job')
    if (!returned) return
    const cancelled = params.get('cancelled')
    window.history.replaceState({}, '', window.location.pathname)
    openJob(returned)
    if (cancelled) {
      setNotice('You cancelled the PayPal approval. Nothing was held, so nothing was charged.')
    }
  }, [openJob])

  const run = useCallback(
    async (payload) => {
      setBusy(true)
      setError(null)
      unsubRef.current?.()
      try {
        const created = await api.createJob(payload)
        setJob({ ...created, ledger: [], messages: [], offers: [] })
        setLiveEntries([])
        setTab('negotiation')
        setLive(true)

        // Follow the ledger as it is written, then reconcile with the final record.
        unsubRef.current = streamJob(created.id, {
          onEntry: ({ entry }) => setLiveEntries((prev) => [...prev, entry]),
          onDone: async () => {
            setLive(false)
            try {
              const final = await api.job(created.id)
              setJob(final)
              setLiveEntries(final.ledger ?? [])
            } catch { /* the run finished; a refresh miss is not fatal */ }
            await refreshLists()
          },
          onError: () => setLive(false),
        })

        await api.runJob(created.id, payload.seller_ids)
        const final = await api.job(created.id)
        setJob(final)
        setLiveEntries(final.ledger ?? [])
        await refreshLists()
      } catch (e) {
        setError(e.message)
      } finally {
        setLive(false)
        setBusy(false)
      }
    },
    [refreshLists],
  )

  const approve = useCallback(async () => {
    if (!job) return
    setBusy(true)
    setError(null)
    try {
      const updated = await api.approveJob(job.id)
      setJob(updated)
      setLiveEntries(updated.ledger ?? [])
      await refreshLists()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }, [job, refreshLists])

  const reconcile = useCallback(async () => {
    if (!job) return
    setBusy(true)
    setError(null)
    try {
      const updated = await api.reconcileJob(job.id)
      setJob(updated)
      setLiveEntries(updated.ledger ?? [])
      await refreshLists()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }, [job, refreshLists])

  const viewJob = useMemo(
    () => (job ? { ...job, ledger: liveEntries.length ? liveEntries : job.ledger } : null),
    [job, liveEntries],
  )

  const chainValid = useMemo(() => {
    if (!viewJob) return true
    return viewJob.ledger_chain_valid !== false
  }, [viewJob])

  return (
    <div className="min-h-screen bg-ink-950 grain">
      <Header health={health} live={live} />

      <main className="mx-auto max-w-[1500px] px-5 py-6">
        {job?.escrow?.state === 'awaiting_approval' && (
          <div className="mb-5 flex flex-wrap items-center justify-between gap-4 rounded-xl border border-held/40 bg-held-dim/40 px-4 py-3">
            <div>
              <div className="text-sm font-medium text-held">Buyer approval required</div>
              <div className="mt-0.5 text-xs text-paper-dim">
                PayPal will not authorize this order until the buyer approves it. Nothing is held
                and no work is produced until then.
              </div>
            </div>
            <div className="flex items-center gap-2">
              {job.escrow.approval_url && (
                <a
                  href={job.escrow.approval_url}
                  target="_blank"
                  rel="noreferrer"
                  className="chip ring-1 bg-ink-800 text-paper-dim ring-ink-600 hover:text-paper"
                >
                  Open PayPal approval
                </a>
              )}
              <button
                onClick={approve}
                disabled={busy}
                className="chip ring-1 bg-paypal-deep/50 text-paypal-bright ring-paypal/40 disabled:opacity-50"
              >
                {busy ? 'Continuing…' : 'I approved — continue'}
              </button>
            </div>
          </div>
        )}

        {job?.status === 'unresolved' && (
          <div className="mb-5 rounded-xl border border-held/40 bg-held-dim/40 px-4 py-3">
            <div className="text-sm font-medium text-held">Settlement unresolved</div>
            <div className="mt-0.5 text-xs text-paper-dim">
              PayPal did not confirm a terminal outcome, so Pact has recorded neither a release nor
              a return. This needs reconciliation rather than a retry — a retry could capture twice.
            </div>
            <button
              disabled={busy}
              onClick={reconcile}
              className="chip mt-3 ring-1 bg-paypal-deep/50 text-paypal-bright ring-paypal/40 disabled:opacity-50"
            >
              {busy ? 'Reconciling…' : 'Reconcile with PayPal'}
            </button>
          </div>
        )}

        {notice && (
          <div className="mb-5 flex items-start justify-between gap-4 rounded-xl border border-held/40 bg-held-dim/40 px-4 py-3">
            <div className="text-xs text-paper-dim">{notice}</div>
            <button className="text-2xs text-paper-faint hover:text-paper" onClick={() => setNotice(null)}>
              dismiss
            </button>
          </div>
        )}

        {error && (
          <div className="mb-5 flex items-start justify-between gap-4 rounded-xl border border-voided/40 bg-voided-dim/40 px-4 py-3">
            <div>
              <div className="text-sm font-medium text-voided">Something went wrong</div>
              <div className="mt-0.5 text-xs text-paper-dim">{error}</div>
            </div>
            <button className="text-2xs text-paper-faint hover:text-paper" onClick={() => setError(null)}>
              dismiss
            </button>
          </div>
        )}

        <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1fr)_380px]">
          {/* ------------------------------------------------ primary column */}
          <div className="space-y-5">
            {viewJob ? (
              <>
                <JobHeader job={viewJob} />
                <div className="panel">
                  <div className="panel-head">
                    <div className="flex gap-1">
                      {TABS.map((t) => (
                        <button
                          key={t.id}
                          onClick={() => setTab(t.id)}
                          className={`chip ring-1 transition-colors ${
                            tab === t.id
                              ? 'bg-paypal-deep/40 text-paypal-bright ring-paypal/40'
                              : 'bg-ink-850 text-paper-faint ring-ink-700 hover:text-paper-dim'
                          }`}
                        >
                          {t.label}
                          {t.id === 'ledger' && viewJob.ledger?.length > 0 && (
                            <span className="tnum opacity-60">{viewJob.ledger.length}</span>
                          )}
                        </button>
                      ))}
                    </div>
                    {live && (
                      <span className="chip bg-paypal-deep/40 text-paypal-bright ring-1 ring-paypal/40">
                        <span className="live-dot h-1.5 w-1.5 rounded-full bg-current" />
                        live
                      </span>
                    )}
                  </div>

                  <div className="max-h-[620px] overflow-y-auto">
                    {tab === 'negotiation' && <Negotiation job={viewJob} />}
                    {tab === 'verification' && <Verification job={viewJob} />}
                    {tab === 'ledger' && (
                      <Ledger job={viewJob} entries={viewJob.ledger} chainValid={chainValid} />
                    )}
                  </div>
                </div>
              </>
            ) : (
              <div className="panel">
                <Empty
                  title="No settlement selected"
                  body="Post a job to watch agents negotiate a price, hold funds in PayPal escrow, verify the delivery, and release payment."
                />
              </div>
            )}

            <JobsTable jobs={jobs} activeId={job?.id} onOpen={openJob} />
          </div>

          {/* -------------------------------------------------- right column */}
          <aside className="space-y-5">
            <Composer onRun={run} busy={busy} health={health} />

            {viewJob && <EscrowRecord job={viewJob} />}

            <div className="panel">
              <div className="panel-head">
                <span className="label">Seller reputation</span>
                <span className="text-2xs text-paper-faint">from verified deliveries</span>
              </div>
              <Reputation agents={reputation} />
            </div>

            <HowItWorks />
          </aside>
        </div>
      </main>

      <footer className="mx-auto max-w-[1500px] px-5 pb-10 pt-2">
        <div className="rule mb-4" />
        <p className="text-2xs leading-relaxed text-paper-faint">
          Pact settles agent commerce on PayPal escrow: an authorization holds the buyer's funds, and a capture
          releases them only once an independent verifier confirms the delivery. A failed delivery is voided, so
          the buyer is charged nothing.
        </p>
      </footer>
    </div>
  )
}

function Header({ health, live }) {
  const paypalMode = health?.paypal?.mode
  const llmMock = health?.llm?.is_mock
  return (
    <header className="sticky top-0 z-20 border-b border-ink-800 bg-ink-950/85 backdrop-blur">
      <div className="mx-auto flex max-w-[1500px] items-center justify-between gap-4 px-5 py-3">
        <div className="flex items-center gap-3">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-paypal text-white">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.25">
              <path d="M9 12l2 2 4-4" />
              <path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3z" />
            </svg>
          </div>
          <div>
            <div className="text-sm font-semibold tracking-tight text-paper">Pact</div>
            <div className="text-2xs text-paper-faint">Settlement for the agent economy</div>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {llmMock && (
            <span
              className="chip bg-ink-800 text-paper-faint ring-1 ring-ink-600"
              title="No LLM_API_KEY configured. Agents fall back to a deterministic policy engine so the full lifecycle still runs."
            >
              Mock agents
            </span>
          )}
          {paypalMode && <ModeBadge mode={paypalMode} />}
          {live && (
            <span className="chip bg-released-dim text-released ring-1 ring-released/40">
              <span className="live-dot h-1.5 w-1.5 rounded-full bg-current" />
              settling
            </span>
          )}
        </div>
      </div>
    </header>
  )
}

function JobHeader({ job }) {
  return (
    <div className="panel px-4 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-base font-semibold tracking-tight text-paper">{job.title}</h1>
            <StatusPill status={displayStatus(job.status, job.escrow?.state)} />
          </div>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-paper-dim">{job.brief}</p>
        </div>
        <div className="flex items-center gap-2">
          <EscrowBadge state={job.escrow?.state} />
        </div>
      </div>

      <div className="mt-4">
        <Lifecycle job={job} />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat
          label="Agreed price"
          value={job.agreed_price != null ? money(job.agreed_price, job.currency) : '—'}
          sub={job.agreed_price != null ? `ceiling ${money(job.budget_max, job.currency)}` : 'not agreed yet'}
          tone={job.agreed_price != null ? 'text-paper' : 'text-paper-faint'}
        />
        <Stat
          label="Seller"
          value={job.seller_id ? job.seller_id.replace('seller_', '') : '—'}
          sub={job.offers?.length ? `${job.offers.length} offers` : 'no offers'}
          tone={job.seller_id ? 'text-paper' : 'text-paper-faint'}
        />
        <Stat
          label="Verification"
          value={job.verification ? (job.verification.passed ? 'Passed' : 'Failed') : '—'}
          sub={job.verification ? `score ${job.verification.score.toFixed(2)}` : 'pending'}
          tone={job.verification ? (job.verification.passed ? 'text-released' : 'text-voided') : 'text-paper-faint'}
        />
        <Stat
          label={
            job.escrow?.state === 'released'
              ? 'Released to seller'
              : job.escrow?.state === 'unresolved'
                ? 'Outcome unresolved'
                : 'Held / returned'
          }
          value={
            job.escrow?.state === 'released'
              ? money(job.escrow.amount, job.escrow.currency)
              : job.escrow?.state === 'voided'
                ? '$0.00'
                : money(job.escrow?.amount ?? 0, job.escrow?.currency ?? 'USD')
          }
          sub={
            job.escrow?.state === 'voided'
              ? 'buyer charged nothing'
              : job.escrow?.state === 'released'
                ? 'captured from escrow'
                : job.escrow?.state === 'unresolved'
                  ? 'needs reconciliation'
                  : job.escrow?.state === 'awaiting_approval'
                    ? 'not yet authorized'
                    : 'in escrow'
          }
          tone={
            job.escrow?.state === 'released'
              ? 'text-released'
              : job.escrow?.state === 'voided'
                ? 'text-voided'
                : 'text-held'
          }
        />
      </div>
    </div>
  )
}

function JobsTable({ jobs, activeId, onOpen }) {
  return (
    <div className="panel">
      <div className="panel-head">
        <span className="label">Settlements</span>
        <span className="text-2xs text-paper-faint tnum">{jobs.length} total</span>
      </div>
      {!jobs.length ? (
        <Empty title="No settlements yet" body="Your first job will appear here." />
      ) : (
        <ul className="divide-y divide-ink-800">
          {jobs.map((j) => (
            <li key={j.id}>
              <button
                onClick={() => onOpen(j.id)}
                className={`flex w-full items-center justify-between gap-4 px-4 py-2.5 text-left transition-colors hover:bg-ink-850 ${
                  j.id === activeId ? 'bg-ink-850' : ''
                }`}
              >
                <div className="min-w-0">
                  <div className="truncate text-xs text-paper">{j.title}</div>
                  <div className="mt-0.5 flex items-center gap-2 text-2xs text-paper-faint">
                    <span>{j.seller_id ? j.seller_id.replace('seller_', '') : 'unassigned'}</span>
                    <span>·</span>
                    <span className="tnum">{j.ledger_entries} entries</span>
                    {j.escrow_mode === 'mock' && (
                      <>
                        <span>·</span>
                        <span>simulated</span>
                      </>
                    )}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  {j.agreed_price != null && (
                    <Money value={j.agreed_price} currency={j.currency} className="text-xs text-paper-dim" />
                  )}
                  <StatusPill status={displayStatus(j.status, j.escrow_state)} />
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function HowItWorks() {
  const steps = [
    ['Agents negotiate', 'Seller agents quote, the buyer counters, and a price is agreed under a hard ceiling.'],
    ['Escrow holds funds', 'PayPal authorizes the agreed amount. The seller has committed funds, the buyer has parted with none.'],
    ['Delivery is verified', 'An independent verifier checks the artifact against each acceptance criterion, on the record.'],
    ['Settlement releases or returns', 'A pass captures the authorization. A fail voids it, and the buyer is charged nothing.'],
  ]
  return (
    <div className="panel">
      <div className="panel-head">
        <span className="label">How a pact settles</span>
      </div>
      <ol className="divide-y divide-ink-800">
        {steps.map(([title, body], i) => (
          <li key={title} className="flex gap-3 px-4 py-2.5">
            <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md border border-ink-600 text-2xs tnum text-paper-faint">
              {i + 1}
            </span>
            <div>
              <div className="text-xs text-paper">{title}</div>
              <div className="mt-0.5 text-2xs leading-relaxed text-paper-faint">{body}</div>
            </div>
          </li>
        ))}
      </ol>
    </div>
  )
}
