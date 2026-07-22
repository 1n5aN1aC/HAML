// Contest-submission export (client-side). The sibling of adif-export.js:
// where that one dumps the whole log for archiving, this one writes the file a
// specific contest sponsor wants, shaped by the Event Template's `export`
// block (server/templates/example.json documents the schema).
//
// The two writers are deliberately independent. An archival ADIF is the
// operator's judgement — they resolve every band and mode in a dialog. A
// submission is the Template's judgement: the admin who wrote the Template
// knew which contest this is, so the mapping tables live in the Template and
// there is no dialog. The same 'Phone' becomes SSB for ADIF and (one day) PH
// for Cabrillo, which is exactly why the maps live inside `export` rather than
// beside the Template's band/mode lists.
//
// A format is an entry in FORMATS with a fixed shape — { label, filename,
// build } — so nothing outside this module ever names a format except as the
// string the Template supplies.

import { adifHeader, tag, timeTags } from './adif-export.js'
import { isBuiltin } from './builtin-fields.js'

// Event-level names the `fields` list may reference. Deliberately just the
// one: the Event's `name` would shadow the `name` built-in (the operator's
// name for the contact), and no format needs it. Add here only on demand.
const EVENT_NAMES = new Set(['station_callsign'])

// Contact columns that are not built-in fields but are nameable all the same.
// Built-ins are handled through the registry, custom fields through `fields`.
const CONTACT_COLUMNS = new Set([
  'remote_callsign', 'operator_callsign', 'operator_initials', 'qso_at',
  'band', 'mode',
])

// Which `export` key holds the mapping table for a given field name. A mapped
// field's raw log value is replaced by its table entry, and a value the table
// does not list resolves to blank — which omits the tag. That is the contract:
// declaring a map declares it complete, and `unmappedValues()` shows the
// operator what a given log would drop before they export.
const VALUE_MAPS = { band: 'band_map', mode: 'mode_map' }

// One name from the Template's `fields` list -> its value for this contact.
// Resolution order is prompt answer, then Event meta, then contact column,
// then custom template field. A name matching nothing resolves to undefined
// and its tag is simply omitted, the same as a name whose value is blank.
export function resolveValue(name, contact, ctx) {
  let value
  if (Object.prototype.hasOwnProperty.call(ctx.prompts, name)) value = ctx.prompts[name]
  else if (EVENT_NAMES.has(name)) value = ctx.event[name]
  else if (CONTACT_COLUMNS.has(name) || isBuiltin(name)) value = contact[name]
  else value = contact.fields?.[name]

  const mapKey = VALUE_MAPS[name]
  if (mapKey) value = ctx.config[mapKey]?.[String(value ?? '').trim()]
  return value
}

// The earliest QSO date in the file, as YYYYMMDD — the date segment of a POTA
// filename. Contacts arrive sorted, but a bad timestamp anywhere would break
// that assumption, so take the real minimum.
function earliestDate(contacts) {
  const times = contacts
    .map((c) => Date.parse(c.qso_at))
    .filter((ms) => !Number.isNaN(ms))
  if (!times.length) return ''
  return new Date(Math.min(...times)).toISOString().slice(0, 10).replace(/-/g, '')
}

// ---------------------------------------------------------------- POTA -----

// Field name -> ADIF tag for the POTA writer. Its own table rather than a
// shared one: tag naming is a format's business, and keeping it here means a
// change made for the archival export can never silently alter submissions.
//
// A name absent from this table but carrying a value still exports, as
// APP_HAML_<NAME> — the same fallback adif-export.js uses, so an admin who
// lists a custom field in `export.fields` gets it in the file rather than
// silently losing it.
const POTA_TAGS = {
  station_callsign: 'STATION_CALLSIGN',
  operator_callsign: 'OPERATOR',
  remote_callsign: 'CALL',
  band: 'BAND',
  mode: 'MODE',
  my_state: 'MY_STATE',
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

// The tags one field name contributes. Three names produce more than one tag:
// a timestamp is ADIF's QSO_DATE + TIME_ON, and each park reference is written
// two ways.
//
// Both ways, deliberately. SIG/SIG_INFO (with 'POTA' as the program) is what
// POTA's own reference documents and what its uploader reads. MY_POTA_REF and
// POTA_REF are the dedicated ADIF 3.1.4 fields for the same thing, which POTA
// appears to ignore but every other consumer of the file understands. Writing
// both costs one tag each and makes the file correct for readers beyond the
// sponsor it was produced for.
//
// The park groups must be gated on the value, not left to tag() to drop: the
// 'POTA' program tag is a constant, and would otherwise survive on its own and
// mark every ordinary contact as a park-to-park with no park.
function potaTags(name, value) {
  const blank = !String(value ?? '').trim()
  if (name === 'qso_at') return timeTags(value)
  if (name === 'my_park') {
    return blank ? [] : [
      tag('MY_SIG', 'POTA'), tag('MY_SIG_INFO', value), tag('MY_POTA_REF', value),
    ]
  }
  if (name === 'their_park') {
    return blank ? [] : [
      tag('SIG', 'POTA'), tag('SIG_INFO', value), tag('POTA_REF', value),
    ]
  }
  return [tag(POTA_TAGS[name] ?? `APP_HAML_${name.toUpperCase()}`, value)]
}

// <call>@<park>-<first date>.adi, per POTA's documented convention. Only the
// default: the export modal offers it in an editable box, which is what covers
// the cases POTA documents but we do not model — a park spanning two states,
// a two-fer, or one club position's share of a split log.
function potaFilename(contacts, ctx) {
  const call = ctx.event.station_callsign || 'CALL'
  const park = ctx.prompts.my_park || 'PARK'
  return `${call}@${park}-${earliestDate(contacts)}.adi`
}

function buildPota(contacts, ctx) {
  const names = ctx.config.fields ?? []
  const records = contacts.map((contact) => {
    const tags = []
    for (const name of names) {
      tags.push(...potaTags(name, resolveValue(name, contact, ctx)))
    }
    return tags.filter(Boolean).join(' ') + ' <EOR>'
  })
  return adifHeader(`POTA submission from HAML — ${ctx.event.name ?? 'event'}`)
    + records.join('\n') + '\n'
}

// --------------------------------------------------------------------------

// Every supported submission format, keyed by the string a Template's
// `export.format` carries. Each entry has the same three members, so callers
// stay format-agnostic: look the format up, call filename(), call build().
export const FORMATS = {
  pota: { label: 'POTA', filename: potaFilename, build: buildPota },
}

// The Template's export config for an Event, or null when it has none or
// names a format this client does not know.
export function exportConfig(event) {
  const config = event?.config?.export
  if (!config || !FORMATS[config.format]) return null
  return config
}

// Prompt definitions to ask the operator, in Template order. They are ordinary
// field defs, so FieldInput renders and validates them exactly as it does the
// entry form's.
export function exportPrompts(config) {
  return (config.prompts ?? []).map((p) => ({
    required: false,
    max_length: 20,
    ...p,
  }))
}

// Log values that no mapping table covers, as [{ key, value, count }]. These
// export with their tag omitted, so the modal warns about them by name and
// count before anything is written — the only feedback between a Template
// mistake and the contest sponsor rejecting those QSOs.
export function unmappedValues(contacts, config) {
  const found = []
  for (const [key, mapKey] of Object.entries(VALUE_MAPS)) {
    const map = config[mapKey]
    const counts = new Map()
    for (const contact of contacts) {
      const value = String(contact[key] ?? '').trim()
      if (value && !map?.[value]) counts.set(value, (counts.get(value) ?? 0) + 1)
    }
    for (const [value, count] of counts) found.push({ key, value, count })
  }
  return found
}