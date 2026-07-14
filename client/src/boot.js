// Boot sequence (plan §3.2): establish client identity, fetch the active
// Event, and detect Event switches via the Event UUID (ADR-0002).
import { getEvent } from './api.js'
import { getClientUuid, kvGet, kvSet, wipeEventData } from './db.js'

// Resolves to { status, event, clientUuid, connected }
//   status: 'ready' | 'no-server' | 'mismatch'
//   'mismatch' means the server runs a different Event and the operator must
//   confirm the wipe; caller re-runs boot with { acceptNewEvent: true }.
export async function boot({ acceptNewEvent = false } = {}) {
  const clientUuid = await getClientUuid()
  const cached = await kvGet('event')

  let event
  try {
    event = await getEvent()
  } catch {
    // Offline boot: fine if we have a cached config, dead end otherwise.
    return cached
      ? { status: 'ready', event: cached, clientUuid, connected: false }
      : { status: 'no-server', event: null, clientUuid, connected: false }
  }

  if (cached && cached.event_uuid !== event.event_uuid) {
    if (!acceptNewEvent) {
      return { status: 'mismatch', event, cached, clientUuid, connected: true }
    }
    await wipeEventData()
  }
  await kvSet('event', {
    event_uuid: event.event_uuid,
    name: event.name,
    station_callsign: event.station_callsign,
    local_exchange: event.local_exchange,
    config: event.config,
  })
  return { status: 'ready', event, clientUuid, connected: true }
}
