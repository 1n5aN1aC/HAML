// Shared realistic fixtures for HAML component previews.
// Shapes mirror the app: Template config (ADR-0003), Contact rows, presence
// stations, and chat messages. Not a preview itself (name isn't a component),
// so the converter never builds it — the previews import it relatively.

export const config = {
  name: 'ARRL Field Day',
  bands: ['160m', '80m', '40m', '20m', '15m', '10m', '6m', '2m'],
  modes: ['Phone', 'CW', 'Digital'],
  dupe_key: ['remote_callsign', 'band', 'mode'],
  contact_list: ['class', 'section'],
  fields: [
    {
      name: 'class',
      label: 'Class',
      type: 'text',
      required: true,
      default: '',
      order: 1,
      validation: { pattern: '\\d{1,2}[A-F]', message: 'Class must be a Field Day class like 3A' },
    },
    {
      name: 'section',
      label: 'Section',
      type: 'text',
      required: true,
      default: '',
      order: 2,
      validation: { pattern: '[A-Z]{2,3}', message: 'ARRL Section must be an abbreviation like OR' },
    },
  ],
}

export const session = { callsign: 'W7ABC', initials: 'JS', band: '20m', mode: 'Phone' }

export const clientUuid = 'client-me-0001'

// A few UTC ISO timestamps, most-recent last.
const t = (min: number) => new Date(Date.now() - min * 60_000).toISOString()

export const contacts = [
  { uuid: 'c1', qso_at: t(2), created_at: t(2), last_edited: t(2), remote_callsign: 'K1ABC', operator_callsign: 'W7ABC', operator_initials: 'JS', client_uuid: clientUuid, band: '20m', mode: 'Phone', deleted: false, sync_state: 'synced', fields: { class: '3A', section: 'OR' } },
  { uuid: 'c2', qso_at: t(5), created_at: t(5), last_edited: t(5), remote_callsign: 'N0XYZ', operator_callsign: 'W7ABC', operator_initials: 'JS', client_uuid: clientUuid, band: '20m', mode: 'Phone', deleted: false, sync_state: 'pending', fields: { class: '1D', section: 'CO' } },
  { uuid: 'c3', qso_at: t(9), created_at: t(9), last_edited: t(9), remote_callsign: 'VE3QRP', operator_callsign: 'KD9OPS', operator_initials: 'AM', client_uuid: 'client-other-1', band: '40m', mode: 'CW', deleted: false, sync_state: 'synced', fields: { class: '2E', section: 'ONE' } },
  { uuid: 'c4', qso_at: t(14), created_at: t(14), last_edited: t(14), remote_callsign: 'W5FD', operator_callsign: 'W7ABC', operator_initials: 'JS', client_uuid: clientUuid, band: '15m', mode: 'Digital', deleted: false, sync_state: 'synced', fields: { class: '5A', section: 'STX' } },
  { uuid: 'c5', qso_at: t(21), created_at: t(21), last_edited: t(21), remote_callsign: 'KH6ISL', operator_callsign: 'KD9OPS', operator_initials: 'AM', client_uuid: 'client-other-1', band: '20m', mode: 'Phone', deleted: false, sync_state: 'synced', fields: { class: '1B', section: 'PAC' } },
]

export const sampleContact = contacts[0]

// Presence roster: server sends last_seen_at as epoch SECONDS.
const now = Date.now() / 1000
export const stations = [
  { client_uuid: clientUuid, callsign: 'W7ABC', initials: 'JS', band: '20m', mode: 'Phone', last_seen_at: now - 2 },
  { client_uuid: 'client-other-1', callsign: 'KD9OPS', initials: 'AM', band: '40m', mode: 'CW', last_seen_at: now - 8 },
  { client_uuid: 'client-other-2', callsign: 'N0XYZ', initials: 'BT', band: '20m', mode: 'Phone', last_seen_at: now - 34 },
  { client_uuid: 'client-other-3', callsign: 'VE3QRP', initials: 'CL', band: 'Off-Air', mode: 'Phone', last_seen_at: now - 75 },
]

// Another 20m station (not us) makes StatusBar's band-conflict warning fire.
export const conflicts = [{ client_uuid: 'client-other-2', callsign: 'N0XYZ' }]

export const chat = [
  { uuid: 'm1', sent_at: t(12), operator_callsign: 'KD9OPS', text: 'Anyone hearing the OR beacon on 20m?', status: 'synced' },
  { uuid: 'm2', sent_at: t(8), operator_callsign: 'W7ABC', text: 'Loud and clear here, running 100W.', status: 'synced' },
  { uuid: 'm3', sent_at: t(3), operator_callsign: 'N0XYZ', text: 'Switching to 40m CW for a bit.', status: 'pending' },
  { uuid: 'm4', sent_at: t(1), operator_callsign: 'W7ABC', text: 'Message did not send — will retry.', status: 'failed' },
]

// Dexie schema, copied verbatim from client/src/db.js. ContactList / LoggingTab
// read the live query over IndexedDB 'haml'; previews seed it before mounting.
export const DB_NAME = 'haml'
export const DB_STORES = { contacts: 'uuid, qso_at, last_edited, sync_state', kv: 'key', chat: 'uuid, sent_at' }
