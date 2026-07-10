// The sync engine (ADR-0001, plan §3.3).
//
// Push: fire-and-retry POST of each pending contact, individually. A push
// response never marks anything synced — the pull is the acknowledgment.
// Pull: fetch changes since the cursor and apply them; whatever the server
// sends wins locally (LWW lives on the server only). The cursor and clock
// offset come from pull responses only — server time, never client time.
import { db, kvGet, kvSet } from './db.js'
import { getContacts, postContact } from './api.js'

const PUSH_INTERVAL = 10_000
const PUSH_BACKOFF_CAP = 120_000
const PULL_INTERVAL = 30_000

const ts = (iso) => Date.parse(iso)

let engine = null

// Starts the loops; onStatus(bool) reports connectivity after every server
// exchange. Returns a stop function.
export function startSync(onStatus) {
  stopSync()
  const e = {
    onStatus,
    stopped: false,
    pushTimer: null,
    pullTimer: null,
    pushDelay: PUSH_INTERVAL,
    pushing: false,
    pushAgain: false,
  }
  engine = e
  pushLoop(e) // first pass drains anything pending from offline time
  pullLoop(e)
  return () => {
    if (engine === e) stopSync()
  }
}

export function stopSync() {
  if (!engine) return
  engine.stopped = true
  clearTimeout(engine.pushTimer)
  clearTimeout(engine.pullTimer)
  engine = null
}

// Call after any local write: pushes pending contacts immediately.
export function pushNow() {
  if (engine) pushLoop(engine)
}

// Pull immediately (server poke / reconnect — used by the WebSocket layer).
export function pullNow() {
  if (engine) pullLoop(engine)
}

async function pushLoop(e) {
  if (e.stopped) return
  if (e.pushing) {
    e.pushAgain = true
    return
  }
  e.pushing = true
  clearTimeout(e.pushTimer)
  const ok = await pushPass(e)
  e.pushing = false
  if (e.stopped) return
  // Exponential backoff while the server is unreachable; normal cadence otherwise.
  e.pushDelay = ok ? PUSH_INTERVAL : Math.min(e.pushDelay * 2, PUSH_BACKOFF_CAP)
  const delay = ok && e.pushAgain ? 0 : e.pushDelay
  e.pushAgain = false
  e.pushTimer = setTimeout(() => pushLoop(e), delay)
}

async function pushPass(e) {
  const pending = await db.contacts.where('sync_state').equals('pending').toArray()
  let ok = true
  for (const row of pending) {
    const { sync_state, synced_at, ...contact } = row
    try {
      await postContact(contact)
    } catch (err) {
      if (err.status) continue // server rejected this one; stays pending, keep going
      ok = false // network failure: stop the pass and back off
      break
    }
  }
  e.onStatus(ok)
  return ok
}

async function pullLoop(e) {
  if (e.stopped) return
  clearTimeout(e.pullTimer)
  try {
    await pullPass()
    e.onStatus(true)
  } catch {
    e.onStatus(false)
  }
  if (e.stopped) return
  e.pullTimer = setTimeout(() => pullLoop(e), PULL_INTERVAL)
}

async function pullPass() {
  const cursor = await kvGet('sync_cursor')
  const { contacts, server_time } = await getContacts(cursor)
  await db.transaction('rw', db.contacts, db.kv, async () => {
    for (const contact of contacts) {
      await db.contacts.put({ ...contact, sync_state: 'synced' })
    }
    // Cursor and clock offset are only ever taken from pull responses (ADR-0001).
    await kvSet('sync_cursor', server_time)
    await kvSet('clock_offset', ts(server_time) - Date.now())
  })
}
