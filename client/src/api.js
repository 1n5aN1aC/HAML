// REST wrappers (ADR-0005: REST carries data). Throws on network failure or
// non-2xx; HTTP errors carry err.status so callers can tell "server said no"
// (err.status set) apart from "server unreachable" (no status).
async function request(path, options) {
  const res = await fetch(path, options)
  if (!res.ok) {
    let message = `HTTP ${res.status}`
    try {
      message = (await res.json()).error || message
    } catch {
      /* non-JSON error body; keep the status text */
    }
    const err = new Error(message)
    err.status = res.status
    throw err
  }
  return res.json()
}

export function getEvent() {
  return request('/api/event')
}

export function postContact(contact) {
  return request('/api/contacts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(contact),
  })
}

export function getContacts(since) {
  return request(
    since ? `/api/contacts?since=${encodeURIComponent(since)}` : '/api/contacts',
  )
}
