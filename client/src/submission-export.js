// Contest-submission export (client-side). The sibling of adif-export.js:
// where that one dumps the whole log for archiving, this one writes the file a
// specific contest sponsor wants, shaped by the Event Template's `export`
// block (server/templates/example.json documents the schema).
//
// The two writers are deliberately independent. An archival ADIF is the
// operator's judgement — they resolve every band and mode in a dialog. A
// submission is the Template's judgement: the admin who wrote the Template
// knew which contest this is, so the mapping tables live in the Template and
// there is no dialog. The same 'Phone' becomes SSB for ADIF and PH for a dupe sheet,
// which is exactly why the maps live inside `export` rather than beside the Template's band/mode lists.
//
// A format is an entry in FORMATS with a fixed shape — { label, filename,
// build } — so nothing outside this module ever names a format except as the
// string the Template supplies. Three ship today:
//   pota      ADIF for the POTA uploader (park SIG tags, call@park filename)
//   adif      generic ADIF submission (Winter Field Day: CALL/CLASS/ARRL_SECT)
//   dupesheet ARRL Field Day dupe sheet — plain text, calls sorted by band/mode

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

  // Remap only when the Template actually declares the table. A name in
  // VALUE_MAPS with no table (e.g. `band` when a format omits band_map) passes
  // through raw — its log value is already what the file wants, as ADIF bands
  // are. A declared-but-incomplete table still drops unlisted values to blank,
  // which is the "declaring a map declares it complete" contract unmappedValues
  // reports on.
  const map = ctx.config[VALUE_MAPS[name]]
  if (map) value = map[String(value ?? '').trim()]
  return value
}

// The earliest QSO date in the file, as YYYYMMDD — the date segment of a filename.
// Contacts arrive sorted, but a bad timestamp anywhere would break
// that assumption, so take the real minimum.
function earliestDate(contacts) {
  const times = contacts
    .map((c) => Date.parse(c.qso_at))
    .filter((ms) => !Number.isNaN(ms))
  if (!times.length) return ''
  return new Date(Math.min(...times)).toISOString().slice(0, 10).replace(/-/g, '')
}

// -------------------------------------------------------------- ADIF -----
// Field name -> ADIF tag, the standard table all ADIF writers here share.
// Its own copy rather than importing adif-export.js's: tag naming is a writer's
// business, and keeping it here means a change made for the archival export can
// never silently alter submissions.
//
// A name absent from this table (and from a Template's `tags` override) but
// carrying a value still exports, as APP_HAML_<NAME> — the same fallback
// adif-export.js uses, so an admin who lists a custom field in `export.fields`
// gets it in the file rather than silently losing it.
const ADIF_TAGS = {
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

// The tags one field name contributes to an ADIF submission. A timestamp
// is ADIF's QSO_DATE + TIME_ON; everything else is one tag, whose name a
// Template can override via `export.tags` (this is how Winter Field Day maps its
// custom `class` field to the required <CLASS> tag) before falling back to the
// standard table and then APP_HAML_<NAME>.
function adifTags(name, value, ctx) {
  if (name === 'qso_at') return timeTags(value)
  const tagName = ctx.config.tags?.[name] ?? ADIF_TAGS[name] ?? `APP_HAML_${name.toUpperCase()}`
  return [tag(tagName, value)]
}

// One record per contact, each field resolved and turned into tags by `tagFor`, wrapped in the shared ADIF header.
function renderAdif(contacts, ctx, tagFor, title) {
  const names = ctx.config.fields ?? []
  const records = contacts.map((contact) => {
    const tags = []
    for (const name of names) {
      tags.push(...tagFor(name, resolveValue(name, contact, ctx), ctx))
    }
    return tags.filter(Boolean).join(' ') + ' <EOR>'
  })
  return adifHeader(title) + records.join('\n') + '\n'
}

// ---------------------------------------------------------------- POTA -----

// POTA's tag seam: the two park references each write three tags, and
// everything else is a plain ADIF tag. Both park tags, deliberately.
// SIG/SIG_INFO (with 'POTA' as the program) is what POTA's own reference
// documents and what its uploader reads. MY_POTA_REF and POTA_REF are the
// dedicated ADIF 3.1.4 fields for the same thing, which POTA appears to ignore
// but every other consumer of the file understands. Writing both costs one tag
// each and makes the file correct for readers beyond the sponsor.
//
// The park groups must be gated on the value, not left to tag() to drop: the
// 'POTA' program tag is a constant, and would otherwise survive on its own and
// mark every ordinary contact as a park-to-park with no park.
function potaTags(name, value, ctx) {
  const blank = !String(value ?? '').trim()
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
  return adifTags(name, value, ctx)
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
  return renderAdif(contacts, ctx, potaTags,
    `POTA submission from HAML — ${ctx.event.name ?? 'event'}`)
}

// ------------------------------------------------------ generic ADIF -----

// Winter Field Day and any future ADIF-submission contest: no per-field special
// cases, just the standard table plus the Template's `tags` overrides.
function buildAdifSubmission(contacts, ctx) {
  return renderAdif(contacts, ctx, adifTags,
    `ADIF submission from HAML — ${ctx.event.name ?? 'event'}`)
}

function adifFilename(contacts, ctx) {
  const call = ctx.event.station_callsign || 'CALL'
  const year = earliestDate(contacts).slice(0, 4) || 'log'
  return `${call}-${year}.adi`
}

// ------------------------------------dupe sheet -------------------------------
// An ARRL Field Day dupe sheet: the plain-text list ARRL's web applet takes in lieu of a full log
// Calls grouped by band then mode, modes sorted within each band;
// calls are sorted but NOT de-duplicated, so a genuine dupe shows as a repeat.
// Band labels drop the trailing meter-`m` (80m -> 80, 70cm stays 70cm);
// Modes come through the Template's mode_map (PH / CW / DIG).
// The station's own class and section head the file and are export prompts.
function buildDupeSheet(contacts, ctx) {
  const bandLabel = (b) => b.replace(/(\d)m$/, '$1')

  // band -> (modeCode -> calls[])
  const groups = new Map()
  for (const contact of contacts) {
    const band = String(contact.band ?? '').trim()
    const mode = String(resolveValue('mode', contact, ctx) ?? '').trim()
    const call = String(contact.remote_callsign ?? '').trim()
    if (!band || !mode || !call) continue
    if (!groups.has(band)) groups.set(band, new Map())
    const byMode = groups.get(band)
    if (!byMode.has(mode)) byMode.set(mode, [])
    byMode.get(mode).push(call)
  }

  // Template band order first, then any stray bands not in the Template's list
  const templateBands = ctx.event.config?.bands ?? []
  const orderedBands = [
    ...templateBands.filter((b) => groups.has(b)),
    ...[...groups.keys()].filter((b) => !templateBands.includes(b)).sort(),
  ]

  const lines = [
    `Call Used: ${ctx.event.station_callsign || ''}  Class: ${ctx.prompts.my_class || ''}  ARRL Section: ${ctx.prompts.my_section || ''}`,
    '',
    'Dupe Sheet',
    '',
  ]
  for (const band of orderedBands) {
    const byMode = groups.get(band)
    for (const mode of [...byMode.keys()].sort()) {
      const calls = byMode.get(mode).slice().sort()
      const head = `${bandLabel(band)}  ${mode}`
      lines.push(head, ...calls, '', `${head}  Total Contacts = ${calls.length}`, '', '')
    }
  }
  return lines.join('\n')
}

function dupeSheetFilename(contacts, ctx) {
  const call = ctx.event.station_callsign || 'CALL'
  const year = earliestDate(contacts).slice(0, 4) || 'log'
  return `${year}-FD-${call}.dup`
}

// --------------------------------------------------------------------------

// Every supported submission format, keyed by the string a Template's
// `export.format` carries. Each entry has the same three members, so callers
// stay format-agnostic: look the format up, call filename(), call build().
export const FORMATS = {
  pota: { label: 'POTA', filename: potaFilename, build: buildPota },
  adif: { label: 'ADIF', filename: adifFilename, build: buildAdifSubmission },
  dupesheet: { label: 'Dupe Sheet', filename: dupeSheetFilename, build: buildDupeSheet },
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
// mistake and the contest sponsor rejecting those QSOs. A Template that
// declares no map for a key is opting out entirely (e.g. a raw ADIF BAND), so
// there is nothing to warn about there.
export function unmappedValues(contacts, config) {
  const found = []
  for (const [key, mapKey] of Object.entries(VALUE_MAPS)) {
    const map = config[mapKey]
    if (!map) continue
    const counts = new Map()
    for (const contact of contacts) {
      const value = String(contact[key] ?? '').trim()
      if (value && !map[value]) counts.set(value, (counts.get(value) ?? 0) + 1)
    }
    for (const [value, count] of counts) found.push({ key, value, count })
  }
  return found
}