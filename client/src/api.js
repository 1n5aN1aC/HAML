// REST wrappers (ADR-0005: REST carries data).

// Wrapper around fetch that throws on any non-2xx response.
// Attaches err.status so callers can distinguish HTTP failures from network errors.
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

// Fetch the active event config (template id, frozen field set, metadata).
export function getEvent() {
  return request('/api/event')
}

// Submit a new contact log entry; the client must supply uuid, qso_at,
// last_edited, and client_uuid — the server upserts by uuid and stamps synced_at.
export function postContact(contact) {
  return request('/api/contacts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(contact),
  })
}

// Fetch the full chat history (connect / blip recovery — ADR-0005).
export function getChat() {
  return request('/api/chat')
}

// Fetch all contacts if called with no arguments.
// Else pass a timestamp string to fetch only contacts created or updated since that time.
export function getContacts(since) {
  return request(
    since ? `/api/contacts?since=${encodeURIComponent(since)}` : '/api/contacts',
  )
}
