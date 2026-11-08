import { Suspense, lazy } from 'react'

// AG Grid is the single largest dependency in the bundle and is only needed when
// the Ledger tab is open. Loading it statically pulled ~700 kB into the initial
// chunk for every visitor, including the majority who never open the tab. The
// split keeps the grid on the Ledger route; the rest of the app (guest session,
// composer, negotiation, payment panel) loads without it.
const Ledger = lazy(() => import('./Ledger'))

// A skeleton that matches the grid's real layout (a header row plus rows of the
// same height) so the swap does not shift the page. `role="status"` announces the
// load to assistive technology without stealing focus.
function LedgerSkeleton() {
  return (
    <div role="status" aria-label="Loading the settlement ledger" className="px-4 py-4">
      <div className="flex items-center gap-3 border-b border-ink-800 pb-2.5">
        <span className="h-2.5 w-8 rounded bg-ink-800" />
        <span className="h-2.5 w-14 rounded bg-ink-800" />
        <span className="h-2.5 w-16 rounded bg-ink-800" />
        <span className="ml-auto h-2.5 w-12 rounded bg-ink-800" />
      </div>
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className="flex items-center gap-3 border-b border-ink-850 py-3">
          <span className="h-3 w-6 rounded bg-ink-850" />
          <span className="h-3 w-16 rounded bg-ink-850" />
          <span className="h-3 flex-1 rounded bg-ink-850" />
          <span className="h-3 w-20 rounded bg-ink-850" />
        </div>
      ))}
      <span className="sr-only">Loading the settlement ledger…</span>
    </div>
  )
}

export default function LedgerPanel(props) {
  return (
    <Suspense fallback={<LedgerSkeleton />}>
      <Ledger {...props} />
    </Suspense>
  )
}
