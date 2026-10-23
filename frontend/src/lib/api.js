const BASE = import.meta.env.VITE_API_BASE ?? ''

async function req(path, options) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = `Request failed (${res.status})`
    try {
      const body = await res.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch { /* keep the status-based message */ }
    throw new Error(detail)
  }
  return res.status === 204 ? null : res.json()
}

export const api = {
  health: () => req('/api/health'),
  jobs: () => req('/api/jobs'),
  job: (id) => req(`/api/jobs/${id}`),
  createJob: (payload) => req('/api/jobs', { method: 'POST', body: JSON.stringify(payload) }),
  runJob: (id, sellerIds) =>
    req(`/api/jobs/${id}/run${sellerIds?.length ? `?seller_ids=${sellerIds.join(',')}` : ''}`, { method: 'POST' }),
  ledger: (id) => req(`/api/jobs/${id}/ledger`),
  reputation: () => req('/api/reputation'),
  globalLedger: () => req('/api/ledger'),
}

/** Subscribe to a job's ledger as it is written. Returns an unsubscribe fn. */
export function streamJob(jobId, { onEntry, onDone, onError } = {}) {
  const source = new EventSource(`${BASE}/api/jobs/${jobId}/stream`)
  source.addEventListener('ledger', (e) => onEntry?.(JSON.parse(e.data)))
  source.addEventListener('done', (e) => {
    onDone?.(JSON.parse(e.data))
    source.close()
  })
  source.onerror = () => {
    onError?.()
    source.close()
  }
  return () => source.close()
}

export const money = (value, currency = 'USD') => {
  const n = Number(value ?? 0)
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency,
    minimumFractionDigits: 2,
  }).format(n)
}

export const shortHash = (hash) => (hash ? `${hash.slice(0, 8)}…${hash.slice(-4)}` : '')
