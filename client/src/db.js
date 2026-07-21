// Local Dexie database: the client's own copy of the log (ADR-0001).
// `contacts` mirrors the server row shape plus sync_state (pending | synced).
// `kv` holds client identity, session, cached event config, and sync cursor.

import Dexie from 'dexie'
import { newUuid } from './uuid'

export const db = new Dexie('haml')

db.version(1).stores({
  contacts: 'uuid, qso_at, last_edited, sync_state',
  kv: 'key',
  chat: 'uuid, sent_at',
})

// Fetch a value from the kv table; returns undefined if not found.
export async function kvGet(key) {
  const row = await db.kv.get(key)
  return row ? row.value : undefined
}

// Store a value in the kv table; overwrites any existing value.
export async function kvSet(key, value) {
  await db.kv.put({ key, value })
}

// The Client UUID identifies this machine (see CONTEXT.md).
// It is generated once and survives event switches — it names the machine, not the event.
export async function getClientUuid() {
  let uuid = await kvGet('client_uuid')
  if (!uuid) {
    uuid = newUuid()
    await kvSet('client_uuid', uuid)
  }
  return uuid
}

// Snapshot everything belonging to the current Event for a safety export —
// used from the mismatch screen before the operator switches (and wipes).
export async function exportEventData() {
  return {
    exported_at: new Date().toISOString(),
    event: await kvGet('event'),
    contacts: await db.contacts.toArray(),
    chat: await db.chat.toArray(),
  }
}

// Trigger a browser download of `text` as `filename`.
export function download(filename, text, mime = 'text/plain') {
  const blob = new Blob([text], { type: mime })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = filename
  // Revoking synchronously after click() has historically cancelled the download in Firefox; 
  // Defer it past the current task instead.
  a.click()
  setTimeout(() => URL.revokeObjectURL(a.href), 0)
}

// Download the raw local snapshot: everything Dexie holds, deleted contacts and all. 
// HAML-RAW_<event name>_<date>.json
export async function exportRawEvent() {
  const data = await exportEventData()
  const name = (data.event?.name || 'event').replace(/[^\w-]+/g, '-')
  download(
    `HAML-RAW_${name}_${data.exported_at.slice(0, 10)}.json`,
    JSON.stringify(data, null, 2),
    'application/json',
  )
}

// Event switch (ADR-0002): wipe everything that belongs to the old Event.
// Client UUID and operator identity survive — the machine and person didn't change.
export async function wipeEventData() {
  await db.contacts.clear()
  await db.chat.clear()
  await db.kv.bulkDelete(['event', 'sync_cursor', 'clock_offset'])
}
