// ADIF export (client-side). The mirror image of adif.js: turn the local
// contact rows back into a <NAME:length>value stream other loggers can read.
//
// Bands and modes are the hard part. An Event's band/mode lists are arbitrary
// template strings ('Phone', 'Digital', 'Other', 'FT2') — none of which are
// necessarily ADIF enumeration values — so the caller supplies bandMap/modeMap
// resolved by the operator in the export modal. This module never guesses at
// export time; it only offers the alias tables that seed those choices.

import { BUILTIN_ORDER, isBuiltin, readFieldValue } from './builtin-fields.js'

// Stamped into the header as PROGRAMVERSION; bump when the export format
// changes in a way another logger could notice.
const APP_VERSION = '0.1.0'

// ADIF 3.1.5 enumerations, spec casing (bands are lowercase). These populate
// the modal's dropdowns and are the only values we will ever emit.
export const ADIF_BANDS = [
  '2190m', '630m', '560m', '160m', '80m', '60m', '40m', '30m', '20m', '17m',
  '15m', '12m', '10m', '8m', '6m', '5m', '4m', '2m', '1.25m', '70cm', '33cm',
  '23cm', '13cm', '9cm', '6cm', '3cm', '1.25cm', '6mm', '4mm', '2.5mm', '2mm',
  '1mm', 'submm',
]

// Primary modes only — submodes (USB, LSB, FT4, …) belong in SUBMODE, and
// offering both in one list would invite invalid MODE values.
export const ADIF_MODES = [
  'AM', 'ARDOP', 'ATV', 'CHIP', 'CLO', 'CONTESTI', 'CW', 'DIGITALVOICE',
  'DOMINO', 'DYNAMIC', 'FAX', 'FM', 'FSK441', 'FT8', 'HELL', 'ISCAT', 'JT4',
  'JT6M', 'JT9', 'JT44', 'JT65', 'MFSK', 'MSK144', 'MT63', 'OLIVIA', 'OPERA',
  'PAC', 'PAX', 'PKT', 'PSK', 'PSK2K', 'Q15', 'QRA64', 'ROS', 'RTTY', 'RTTYM',
  'SSB', 'SSTV', 'T10', 'THOR', 'THRB', 'TOR', 'V4', 'VOI', 'WINMOR', 'WSPR',
]

// Seed guesses for the modal, keyed lowercase. Anything absent here arrives
// unselected and the operator must choose — deliberately so for values whose
// meaning only the operator knows, like Field Day's catch-all 'Other'.
//
// The seeds are only a starting point: every one of them is visible and
// changeable in the export modal before a file is written. So a broad category
// like Field Day's 'Digital' gets its most common member (MFSK, which covers
// the FT4/FT8-era traffic that dominates digital QSOs) rather than being left
// blank — the operator retargets it to RTTY, PSK or whatever they actually ran.
export const BAND_ALIASES = Object.fromEntries(ADIF_BANDS.map((b) => [b.toLowerCase(), b]))

export const MODE_ALIASES = {
  ...Object.fromEntries(ADIF_MODES.map((m) => [m.toLowerCase(), m])),
  phone: 'SSB',
  ssb: 'SSB',
  voice: 'SSB',
  usb: 'SSB',
  lsb: 'SSB',
  ft8: 'FT8',
  digital: 'MFSK',
  data: 'MFSK',
  // Real ADIF submodes, each unambiguous about its parent mode.
  ft4: 'MFSK',
  ft2: 'MFSK',
  psk31: 'PSK',
}

// Built-in contact column -> ADIF field name. Every built-in has a real ADIF
// home; `section` is ours-by-another-name (ARRL_SECT). ADIF also has a NOTES
// field, but COMMENT is the one loggers surface most prominently and its
// single-line String type matches our single-line 200-char input — which is
// why our own field is named `comment` to match.
//
// A built-in missing from this table is NOT dropped — it falls through to
// APP_HAML_<NAME>, the same fallback custom fields use. That way adding a
// built-in to builtin-fields.js can never silently lose data from the export;
// the worst case is an ugly tag name until someone maps it properly here.
const BUILTIN_ADIF = {
  country: 'COUNTRY',
  itu_zone: 'ITUZ',
  cq_zone: 'CQZ',
  continent: 'CONT',
  gridsquare: 'GRIDSQUARE',
  distance: 'DISTANCE',
  state: 'STATE',
  section: 'ARRL_SECT',
  county: 'CNTY',
  frequency: 'FREQ',
  rst_sent: 'RST_SENT',
  rst_received: 'RST_RCVD',
  name: 'NAME',
  comment: 'COMMENT',
}

// Custom template fields our shipped templates define, mapped to their real
// ADIF names. `their_park` is handled separately (it needs two tags).
const CUSTOM_ADIF = {
  class: 'CLASS',
}

// ADIF declares lengths in bytes, and lookup-filled values (country, county,
// name) can carry non-ASCII the operator never typed. Note that adif.js slices
// by JS characters, so a non-ASCII value survives round-tripping through other
// loggers but not through our own importer — a pre-existing import limitation.
const encoder = new TextEncoder()

// One <NAME:len>value token, or '' when the value is blank. Blank fields are
// omitted entirely rather than written as zero-length tags.
function tag(name, value) {
  const v = String(value ?? '').trim()
  return v ? `<${name}:${encoder.encode(v).length}>${v}` : ''
}

// A contact's QSO_DATE + TIME_ON, both UTC. qso_at reaches us in either the
// client's 'Z' form or the server's '+00:00' form, so parse rather than slice.
function timeTags(qso_at) {
  const ms = Date.parse(qso_at)
  if (Number.isNaN(ms)) return []
  const iso = new Date(ms).toISOString()
  return [
    tag('QSO_DATE', iso.slice(0, 10).replace(/-/g, '')),
    tag('TIME_ON', iso.slice(11, 19).replace(/:/g, '')),
  ]
}

// Every tag for one contact, in a stable, readable order.
function recordTags(contact, { event, bandMap, modeMap }) {
  const tags = [
    tag('CALL', contact.remote_callsign),
    ...timeTags(contact.qso_at),
    tag('BAND', bandMap[contact.band] ?? ''),
    tag('MODE', modeMap[contact.mode] ?? ''),
    tag('STATION_CALLSIGN', event.station_callsign),
    tag('OPERATOR', contact.operator_callsign),
  ]
  // Drive this from the built-in registry, not from BUILTIN_ADIF, so a newly
  // added built-in exports (as APP_HAML_<NAME>) instead of vanishing.
  for (const name of BUILTIN_ORDER) {
    tags.push(tag(BUILTIN_ADIF[name] ?? `APP_HAML_${name.toUpperCase()}`, contact[name]))
  }
  // Custom template fields. Iterating the Event's own field list (rather than
  // the contact's `fields` blob) keeps the output stable across contacts and
  // guarantees admin-invented fields reach the APP_HAML_ fallback instead of
  // being silently dropped.
  for (const field of event.config?.fields ?? []) {
    const { name } = field
    if (isBuiltin(name)) continue
    const value = readFieldValue(contact, name)
    if (!String(value ?? '').trim()) continue
    if (name === 'their_park') {
      // Park-to-park, written the way our importer reads it back (SIG + SIG_INFO).
      tags.push(tag('SIG', 'POTA'), tag('SIG_INFO', value))
    } else if (CUSTOM_ADIF[name]) {
      tags.push(tag(CUSTOM_ADIF[name], value))
    } else {
      tags.push(tag(`APP_HAML_${name.toUpperCase()}`, value))
    }
  }
  return tags.filter(Boolean)
}

// The complete .adi file for `contacts` (already filtered and ordered by the
// caller) as a string.
export function buildAdif(contacts, { event, bandMap, modeMap }) {
  const created = new Date().toISOString()
  const header = [
    `ADIF export from HAML — ${event.name ?? 'event'}`,
    '',
    [
      tag('ADIF_VER', '3.1.5'),
      tag('PROGRAMID', 'HAML'),
      tag('PROGRAMVERSION', APP_VERSION),
      tag('CREATED_TIMESTAMP',
        created.slice(0, 10).replace(/-/g, '') + ' ' + created.slice(11, 19).replace(/:/g, '')),
    ].join(' '),
    '<EOH>',
    '',
  ].join('\n')
  const records = contacts.map(
    (c) => recordTags(c, { event, bandMap, modeMap }).join(' ') + ' <EOR>',
  )
  return header + records.join('\n') + '\n'
}

// Distinct values of one contact column, with counts — the rows the modal's
// mapping tables render. Mirrors ImportSection's breakdown(), over contacts
// rather than ADIF records.
export function breakdown(contacts, key) {
  const counts = new Map()
  for (const c of contacts) {
    const v = (c[key] ?? '').trim()
    if (v) counts.set(v, (counts.get(v) ?? 0) + 1)
  }
  return [...counts.entries()]
}

// Initial mapping for a breakdown: the alias table's guess, or '' (unselected,
// which gates the export until the operator chooses).
export function initialMapping(pairs, aliases) {
  const mapping = {}
  for (const [value] of pairs) mapping[value] = aliases[value.toLowerCase()] ?? ''
  return mapping
}
