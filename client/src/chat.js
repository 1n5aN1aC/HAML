// Chat client logic (ADR-0005): live messages arrive over the WebSocket; on
// every (re)connect the entire history is re-fetched over REST and replaces
// local state — an Event's chat is small enough that cursors aren't worth it.
// Own sends stay `pending` until the server's broadcast echoes them back; a
// send that misses the socket, or a pending message absent from a refreshed
// history, becomes `failed` for manual resend. UUIDs make resends harmless.
import { db, kvGet } from './db.js'
import { getChat } from './api.js'
import { sendChat as socketSend } from './socket.js'

const wire = ({ uuid, operator_callsign, operator_initials, client_uuid, text }) => ({
  uuid, operator_callsign, operator_initials, client_uuid, text,
})

export function loadChat() {
  return db.chat.orderBy('sent_at').toArray()
}

// Full-history refresh (connect / blip recovery). Server rows replace local
// state; own unconfirmed messages the server doesn't have become failed.
export async function refreshChat() {
  const { messages } = await getChat()
  const known = new Set(messages.map((m) => m.uuid))
  const unconfirmed = (await loadChat()).filter(
    (m) => m.status && m.status !== 'sent' && !known.has(m.uuid),
  )
  await db.transaction('rw', db.chat, async () => {
    await db.chat.clear()
    await db.chat.bulkPut(messages.map((m) => ({ ...m, status: 'sent' })))
    await db.chat.bulkPut(unconfirmed.map((m) => ({ ...m, status: 'failed' })))
  })
  return loadChat()
}

// A live broadcast — which is also the echo that confirms an own pending send.
export async function applyChatBroadcast(message) {
  await db.chat.put({ ...message, status: 'sent' })
  return loadChat()
}

export async function sendMessage({ text, session, clientUuid }) {
  const offset = (await kvGet('clock_offset')) ?? 0
  const message = {
    uuid: crypto.randomUUID(),
    // Provisional, for local ordering only — the server restamps on store.
    sent_at: new Date(Date.now() + offset).toISOString(),
    operator_callsign: session.callsign.trim().toUpperCase(),
    operator_initials: session.initials.trim().toUpperCase(),
    client_uuid: clientUuid,
    text,
  }
  const sent = socketSend(wire(message))
  await db.chat.put({ ...message, status: sent ? 'pending' : 'failed' })
  return loadChat()
}

export async function resendMessage(uuid) {
  const message = await db.chat.get(uuid)
  if (message && socketSend(wire(message))) {
    await db.chat.put({ ...message, status: 'pending' })
  }
  return loadChat()
}
