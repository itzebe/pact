import { useMemo, useRef } from 'react'
import { AgGridReact } from 'ag-grid-react'
import { AllCommunityModule, ModuleRegistry, themeQuartz } from 'ag-grid-community'
import { Hash } from './primitives'

ModuleRegistry.registerModules([AllCommunityModule])

const KIND_TONE = {
  escrow_held: 'text-held',
  escrow_released: 'text-released',
  escrow_voided: 'text-voided',
  verification_result: 'text-paypal-bright',
  delivery: 'text-paper',
  agreement: 'text-paypal-bright',
  offer: 'text-paper-dim',
  counter: 'text-paper-dim',
  acceptance: 'text-released',
  reputation_updated: 'text-paper-faint',
}

const compact = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
})

/**
 * The settlement ledger as a real data grid.
 *
 * Every settlement decision Pact makes is appended here, hash-chained to the
 * entry before it. The chain column is the point: an auditor can recompute it
 * and prove no row was altered after the fact, which is what makes this
 * defensible as a financial record rather than an application log.
 */
export default function Ledger({ job, entries, chainValid }) {
  const gridRef = useRef(null)

  const columnDefs = useMemo(
    () => [
      {
        field: 'seq',
        headerName: '#',
        width: 62,
        cellClass: 'text-paper-faint tnum',
        valueFormatter: (p) => String(p.value).padStart(2, '0'),
      },
      {
        field: 'at',
        headerName: 'Time',
        width: 104,
        cellClass: 'text-paper-faint tnum',
        valueFormatter: (p) => (p.value ? compact.format(new Date(p.value)) : ''),
      },
      {
        field: 'kind',
        headerName: 'Event',
        width: 168,
        cellRenderer: (p) => (
          <span className={`font-mono text-2xs ${KIND_TONE[p.value] ?? 'text-paper-dim'}`}>{p.value}</span>
        ),
      },
      {
        field: 'actor',
        headerName: 'Actor',
        width: 132,
        cellClass: 'text-paper-dim',
      },
      { field: 'summary', headerName: 'Detail', flex: 1, minWidth: 280, cellClass: 'text-paper' },
      {
        field: 'hash',
        headerName: 'Chain',
        width: 118,
        cellRenderer: (p) => <Hash value={p.value} />,
        sortable: false,
      },
    ],
    [],
  )

  const theme = useMemo(
    () =>
      themeQuartz.withParams({
        backgroundColor: 'transparent',
        oddRowBackgroundColor: 'transparent',
        headerBackgroundColor: 'transparent',
        borderColor: '#232936',
        rowBorder: { color: '#171b24' },
        headerTextColor: '#6b7488',
        foregroundColor: '#eef1f6',
        fontFamily: 'inherit',
        fontSize: 12.5,
        headerHeight: 34,
        rowHeight: 36,
        cellHorizontalPadding: 14,
        wrapperBorderRadius: 0,
      }),
    [],
  )

  if (!entries?.length) {
    return <p className="px-4 py-6 text-xs text-paper-faint">The ledger is written as the job runs.</p>
  }

  return (
    <div>
      <div className="ag-theme-quartz-dark" style={{ height: 380, width: '100%' }}>
        <AgGridReact
          ref={gridRef}
          theme={theme}
          rowData={entries}
          columnDefs={columnDefs}
          defaultColDef={{ resizable: true, sortable: true, suppressHeaderMenuButton: true }}
          animateRows
          suppressCellFocus
          getRowId={(p) => String(p.data.seq)}
          overlayNoRowsTemplate="<span style='color:#6b7488;font-size:12px'>No ledger entries yet</span>"
        />
      </div>
      <div className="flex items-center justify-between border-t border-ink-800 px-4 py-2.5">
        <span className="text-2xs text-paper-faint">
          {entries.length} entries · hash-chained SHA-256
        </span>
        <span
          className={`chip ring-1 ${
            chainValid
              ? 'bg-released-dim text-released ring-released/40'
              : 'bg-voided-dim text-voided ring-voided/40'
          }`}
        >
          {chainValid ? 'Chain verified' : 'Chain broken'}
        </span>
      </div>
    </div>
  )
}
