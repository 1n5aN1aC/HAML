// REST wrappers (ADR-0005: REST carries data). Throws on network failure or
// non-2xx; callers decide what "offline" means for them.
async function request(path, options) {
  const res = await fetch(path, options)
  if (!res.ok) {
    let message = `HTTP ${res.status}`
    try {
      message = (await res.json()).error || message
    } catch {
      /* non-JSON error body; keep the status text */
    }
    throw new Error(message)
  }
  return res.json()
}

export function getEvent() {
  return request('/api/event')
}
