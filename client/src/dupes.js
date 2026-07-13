// Advisory duplicate detection (ADR-0003: the client warns at entry time,
// never blocks, and the server never enforces uniqueness). Checks every
// non-deleted contact in the local store — teammates' synced contacts
// included — against the event's duplicate_type.
import { db } from './db.js'

// Returns the most recent matching contact, or null. nowMs is server-corrected
// epoch ms, used only for band-mode-day's current-UTC-day scope.
export async function findDuplicate({ callsign, band, mode, duplicateType, nowMs }) {
  const call = callsign.trim().toUpperCase()
  if (!call || duplicateType === 'none') return null
  const today = new Date(nowMs).toISOString().slice(0, 10)
  const matches = await db.contacts
    .filter((c) => {
      if (c.deleted || c.remote_callsign !== call) return false
      if (duplicateType === 'any') return true
      if (c.band !== band || c.mode !== mode) return false
      // band-mode-day narrows band-mode to the current UTC day (qso_at is ISO UTC)
      return duplicateType !== 'band-mode-day' || c.qso_at.slice(0, 10) === today
    })
    .toArray()
  if (matches.length === 0) return null
  return matches.reduce((a, b) => (a.qso_at >= b.qso_at ? a : b))
}

// Most recent non-deleted contact for a callsign on any band/mode, or null —
// feeds the entry form's "remember" autofill. nowMs is unused on the 'any'
// path but findDuplicate derives its day scope from it unconditionally.
export function findLatestContact(callsign) {
  return findDuplicate({ callsign, duplicateType: 'any', nowMs: Date.now() })
}
