// Local Dexie database: the client's own copy of the log (ADR-0001).
// `contacts` mirrors the server row shape plus sync_state (pending | synced).
// `kv` holds client identity, session, cached event config, and sync cursor.
import Dexie from 'dexie'

export const db = new Dexie('haml')

db.version(1).stores({
  contacts: 'uuid, qso_at, last_edited, sync_state',
  kv: 'key',
  chat: 'uuid, sent_at',
})

export async function kvGet(key) {
  const row = await db.kv.get(key)
  return row ? row.value : undefined
}

export async function kvSet(key, value) {
  await db.kv.put({ key, value })
}

// The Client UUID identifies this logging position (see CONTEXT.md). It is
// generated once and survives event switches — it names the machine, not the event.
export async function getClientUuid() {
  let uuid = await kvGet('client_uuid')
  if (!uuid) {
    uuid = crypto.randomUUID()
    await kvSet('client_uuid', uuid)
  }
  return uuid
}

// Event switch (ADR-0002): wipe everything that belongs to the old Event.
// Client UUID and operator identity survive — the machine and person didn't change.
export async function wipeEventData() {
  await db.contacts.clear()
  await db.chat.clear()
  await db.kv.bulkDelete(['event', 'sync_cursor', 'clock_offset'])
}
