const BASE = import.meta.env?.VITE_API_BASE ?? ''

// The API requires a bearer token on every route. Resolution order:
//   1. a token the operator pasted into this browser (localStorage)
//   2. VITE_API_TOKEN, for a zero-config demo build
// A token is a *credential*: the one baked into a public bundle is a low-privilege
// buyer token for the demo, never an admin token.
const TOKEN_KEY = 'pact.token'

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || import.meta.env?.VITE_API_TOKEN || ''
  } catch {
    return import.meta.env?.VITE_API_TOKEN || ''
  }
}

export function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch { /* storage may be unavailable; the in-memory fallback is not offered */ }
}

function authHeaders() {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function req(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...(options.headers ?? {}) },
  })
  if (!res.ok) {
    if (res.status === 401 || res.status === 403) {
      throw new Error('Not authorized — connect with a valid access token.')
    }
    if (res.status === 503) {
      throw new Error('This server is not configured for authentication.')
    }
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
  approveJob: (id) => req(`/api/jobs/${id}/approve`, { method: 'POST' }),
  cancelJob: (id) => req(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  reconcileJob: (id) => req(`/api/jobs/${id}/reconcile`, { method: 'POST' }),
  deleteJob: (id) => req(`/api/jobs/${id}`, { method: 'DELETE' }),
  ledger: (id) => req(`/api/jobs/${id}/ledger`),
  reputation: () => req('/api/reputation'),
  globalLedger: () => req('/api/ledger'),
}

/** Subscribe to a job's ledger as it is written. Returns an unsubscribe fn.
 *
 * ``EventSource`` cannot set an ``Authorization`` header, so the credential must
 * travel in the URL. Sending the real token there would expose it to server logs,
 * browser history, and any ``Referer``. Instead the client mints a short-lived,
 * single-job *ticket* over a normal authenticated request and puts only that in
 * the URL.
 */
export function streamJob(jobId, { onEntry, onDone, onError } = {}) {
  const sourceRef = { current: null }
  let closed = false
  const close = () => {
    closed = true
    sourceRef.current?.close()
    sourceRef.current = null
  }

  req(`/api/jobs/${jobId}/stream-ticket`, { method: 'POST' })
    .then(({ ticket }) => {
      if (closed) return
      const source = new EventSource(`${BASE}/api/jobs/${jobId}/stream?ticket=${encodeURIComponent(ticket)}`)
      sourceRef.current = source
      source.addEventListener('ledger', (e) => onEntry?.(JSON.parse(e.data)))
      source.addEventListener('done', (e) => {
        onDone?.(JSON.parse(e.data))
        source.close()
      })
      source.onerror = () => {
        onError?.()
        source.close()
      }
    })
    .catch((err) => {
      if (!closed) onError?.(err)
    })

  return close
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
