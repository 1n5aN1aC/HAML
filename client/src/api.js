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

// --- admin endpoints (ADR-0004: gated by the shared password header) --------

const adminHeaders = (password) => ({ 'X-Admin-Password': password })

export function adminListTemplates(password) {
  return request('/api/admin/templates', { headers: adminHeaders(password) })
}

// Fetch one template's full JSON, for the template editor.
export function adminGetTemplate(password, id) {
  return request(`/api/admin/templates/${encodeURIComponent(id)}`, {
    headers: adminHeaders(password),
  })
}

// Create or overwrite a template file by id.
export function adminSaveTemplate(password, id, template) {
  return request(`/api/admin/templates/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...adminHeaders(password) },
    body: JSON.stringify(template),
  })
}

export function adminDeleteTemplate(password, id) {
  return request(`/api/admin/templates/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: adminHeaders(password),
  })
}

export function adminListEvents(password) {
  return request('/api/admin/events', { headers: adminHeaders(password) })
}

// Create a new event from a template; the server activates it immediately.
export function adminCreateEvent(password, { name, station_callsign, template }) {
  return request('/api/admin/events', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...adminHeaders(password) },
    body: JSON.stringify({ name, station_callsign, template }),
  })
}

export function adminActivateEvent(password, eventUuid) {
  return request(`/api/admin/events/${encodeURIComponent(eventUuid)}/activate`, {
    method: 'POST',
    headers: adminHeaders(password),
  })
}

// Delete an inactive event's database; the server refuses the active one.
export function adminDeleteEvent(password, eventUuid) {
  return request(`/api/admin/events/${encodeURIComponent(eventUuid)}`, {
    method: 'DELETE',
    headers: adminHeaders(password),
  })
}

// Snapshot the active event into data/backups/; returns the backup filename.
export function adminBackup(password) {
  return request('/api/admin/backup', {
    method: 'POST',
    headers: adminHeaders(password),
  })
}

export function adminClearChat(password) {
  return request('/api/admin/chat', {
    method: 'DELETE',
    headers: adminHeaders(password),
  })
}
