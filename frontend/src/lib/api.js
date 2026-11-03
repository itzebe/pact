const BASE = import.meta.env?.VITE_API_BASE ?? ''

// Two credential forms are supported, and the client uses whichever is present:
//
//   1. a *guest session* — the default for a public deployment. The server sets an
//      HttpOnly session cookie on first visit; this module never sees or stores it.
//      The matching CSRF token is kept in memory only (never localStorage) and
//      echoed on state-changing requests.
//   2. an *operator token* the operator pastes into this browser (localStorage),
//      or VITE_API_TOKEN for a zero-config demo build. Header-authenticated, so it
//      needs no CSRF token.
//
// A token is a *credential*: the one baked into a public bundle would be a
// low-privilege buyer token for the demo, never an admin token.
const TOKEN_KEY = 'pact.token'

// In-memory only. Persisting it would defeat the point: it exists so a cross-site
// page, which cannot read a JSON response from this origin, cannot forge it.
let csrfToken = ''

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
  } catch { /* storage may be unavailable; the guest cookie path still works */ }
}

export function setCsrf(token) {
  csrfToken = token || ''
}

function authHeaders() {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

// A guest session is authenticated by cookie, so a mutating request must carry the
// CSRF token. An operator token is header-authenticated and needs none.
function csrfHeaders(method) {
  if (getToken()) return {}
  const m = (method ?? 'GET').toUpperCase()
  if (!['POST', 'PUT', 'PATCH', 'DELETE'].includes(m)) return {}
  return csrfToken ? { 'X-CSRF-Token': csrfToken } : {}
}

async function req(path, options = {}) {
  const method = (options.method ?? 'GET').toUpperCase()
  const res = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(),
      ...csrfHeaders(method),
      ...(options.headers ?? {}),
    },
  })
  if (!res.ok) {
    if (res.status === 401) {
      throw new Error('Your session has ended. Reload to start a new workspace.')
    }
    if (res.status === 403) {
      // Could be a CSRF rejection or an ownership refusal; surface the server's
      // reason rather than guessing.
      let detail = 'Not authorized for this action.'
      try {
        const body = await res.json()
        if (body?.detail && typeof body.detail === 'string') detail = body.detail
      } catch { /* keep the default */ }
      throw new Error(detail)
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

/**
 * Establish a session before the product renders.
 *
 * With an operator token already present there is nothing to do. Otherwise the
 * client tries to *restore* an existing guest session (the cookie survives a
 * reload, but the in-memory CSRF token does not, and the server will hand it back
 * to the cookie's owner), and falls back to *creating* one. Both requests are
 * same-origin, so a cross-site page can trigger neither and read neither response.
 */
export async function bootstrapSession() {
  if (getToken()) return { type: 'operator' }

  let res = await fetch(`${BASE}/api/auth/guest`, { credentials: 'include' })
  if (res.status === 401) {
    res = await fetch(`${BASE}/api/auth/guest`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
  }
  if (res.status === 404) {
    // Guest sessions are disabled on this deployment; the operator path applies.
    return { type: 'unavailable' }
  }
  if (!res.ok) {
    throw new Error('Could not start a session. Reload the page to try again.')
  }
  const body = await res.json()
  setCsrf(body.csrf_token)
  return body
}

export async function endGuestSession() {
  setCsrf('')
  await fetch(`${BASE}/api/auth/guest/logout`, { method: 'POST', credentials: 'include' })
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
  refundJob: (id, body) =>
    req(`/api/jobs/${id}/refund`, { method: 'POST', body: JSON.stringify(body ?? {}) }),
  reconcileJob: (id) => req(`/api/jobs/${id}/reconcile`, { method: 'POST' }),
  deleteJob: (id) => req(`/api/jobs/${id}`, { method: 'DELETE' }),
  ledger: (id) => req(`/api/jobs/${id}/ledger`),
  reputation: () => req('/api/reputation'),
  globalLedger: () => req('/api/ledger'),
}

/** Subscribe to a job's ledger as it is written. Returns an unsubscribe fn.
 *
 * ``EventSource`` cannot set an ``Authorization`` header, so an operator's
 * credential must travel in the URL. Sending the real token there would expose it
 * to server logs, browser history, and any ``Referer``. Instead the client mints a
 * short-lived, single-job *ticket* over a normal authenticated request and puts
 * only that in the URL. A guest session is a cookie, which the ``EventSource``
 * sends automatically, so no ticket is needed on that path.
 */
export function streamJob(jobId, { onEntry, onDone, onError } = {}) {
  const sourceRef = { current: null }
  let closed = false
  const close = () => {
    closed = true
    sourceRef.current?.close()
    sourceRef.current = null
  }

  const open = (url) => {
    const source = new EventSource(url, { withCredentials: true })
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
  }

  if (getToken()) {
    req(`/api/jobs/${jobId}/stream-ticket`, { method: 'POST' })
      .then(({ ticket }) => {
        if (closed) return
        open(`${BASE}/api/jobs/${jobId}/stream?ticket=${encodeURIComponent(ticket)}`)
      })
      .catch((err) => {
        if (!closed) onError?.(err)
      })
  } else {
    open(`${BASE}/api/jobs/${jobId}/stream`)
  }

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

/**
 * Poll a job until its escrow state stops changing, or the attempt budget runs out.
 *
 * A webhook or a reconciliation can move a payment on the server while the browser
 * is sitting still — a buyer who approves in PayPal then returns, or an operator
 * who reconciles an ambiguous capture. The SSE stream covers ledger writes, but a
 * webhook-driven settlement can land when no stream is open, so the payment panel
 * polls a bounded number of times rather than showing a stale "held".
 *
 * Returns the last job seen. Never throws for a transient fetch error: a payment
 * panel that blanks on one failed poll is worse than one that keeps the last known
 * state.
 */
export async function pollJob(jobId, { attempts = 6, intervalMs = 2000, isDone, onUpdate } = {}) {
  let last = null
  for (let i = 0; i < attempts; i += 1) {
    try {
      last = await api.job(jobId)
      onUpdate?.(last)
      if (isDone ? isDone(last) : false) return last
    } catch {
      // keep the previous reading; try again
    }
    if (i < attempts - 1) await new Promise((r) => setTimeout(r, intervalMs))
  }
  return last
}
