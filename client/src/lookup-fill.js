// Maps a canonical callsign-lookup record into the entry-form fills that the
// operator's local data can't produce (a DXCC prefix lookup can't tell you
// someone's name, grid square, or state). The record shape is fixed by
// `FIELDS` in server/lookup_record.py — every key always present, null when
// absent, enums lowercased, lat/lon floats, dates ISO — so this module
// trusts field names/types and only null-checks each value it consumes.
// Returns `{}` for anything unusable so callers can merge it blindly.
//
// Provider-agnostic: no upstream name appears anywhere here. The record is
// the contract; whichever adapter the server talks to today is irrelevant.
//
// Gridsquare: The server's canonical-record layer already truncates, uppercases, and pattern-validates it.
// (4-char Maidenhead field grid or null).
//
// Zones (itu_zone, cq_zone): The server's canonical-record layer already coerces these to integers or null.
// This patch is the ONLY zone autofill — the CallParser prefix lookup deliberately doesn't
// fill zones (a US call area doesn't encode station location; see AUTO_FIELDS in
// builtin-fields.js), so a null here means the field simply stays blank.

// True when the text looks like a callsign the lookup might know about — at
// least 3 characters and contains a digit. US callsigns always have a digit;
// most other countries' do too. The server rejects anything that isn't real,
// so the gate here is just plausibility.
export function isPlausibleCallsign(s) {
  return typeof s === 'string' && s.length >= 3 && /\d/.test(s)
}

// US/Canadian ZIP suffix on the address — pulls the 2-letter state out of
// "PORTLAND, OR 97201". Absent addresses or addresses with state in
// different positions simply don't match, returning null.
const STATE_IN_ADDRESS_RE = /\b([A-Z]{2})\s+\d{5}\b/

// State codes the entry field's validation accepts (mirrors
// BUILTINS.state.validation.pattern in builtin-fields.js). Inline so this
// module has no template/React coupling and stays easy to test against the
// canonical record.
const VALID_STATES = new Set([
  'AB','AK','AL','AR','AZ','BC','CA','CO','CT','DC','DE','DX','FL','GA',
  'HI','IA','ID','IL','IN','KS','KY','LA','MA','MB','MD','ME','MI','MN',
  'MO','MS','MT','NB','NC','ND','NE','NH','NJ','NL','NM','NS','NT','NU',
  'NV','NY','OH','OK','ON','OR','PA','PE','QC','RI','SC','SD','SK','TN',
  'TX','UT','VA','VT','WA','WI','WV','WY','YT',
])

// First token of a name, title-cased. "JOSHUA A COOK" -> "Joshua". The
// record stores ALL-CAPS last-write from the upstream; we just normalize
// the first token for the entry field.
function firstTokenTitleCased(name) {
  const first = String(name).trim().split(/\s+/)[0]
  if (!first) return ''
  return first.charAt(0).toUpperCase() + first.slice(1).toLowerCase()
}

// 2-letter state from the address_line2 ZIP, or null when the line isn't
// shaped like one or the code isn't on the entry field's accepted list.
function stateFromAddress(record) {
  if (!record.address_line2) return null
  const m = String(record.address_line2).match(STATE_IN_ADDRESS_RE)
  if (!m) return null
  const code = m[1]
  return VALID_STATES.has(code) ? code : null
}

// { name?, gridsquare?, state?, itu_zone?, cq_zone? } — only the keys the
// entry form knows how to fill from a server lookup, only when the record
// carries a usable value. Clubs, military, and RACES (license_type !== 'person')
// deliberately skip the name fill; a null/missing record or one without any
// usable value returns `{}`.
export function lookupPatchFromRecord(record) {
  if (!record || typeof record !== 'object') return {}
  const patch = {}
  // Personal licenses only — clubs/trustees/RACES/military all skip the name.
  if (record.name && record.license_type === 'person') {
    const n = firstTokenTitleCased(record.name)
    if (n) patch.name = n
  }
  // The server has already coerced gridsquare to a 4-char uppercase grid.
  if (record.gridsquare) patch.gridsquare = record.gridsquare
  const s = stateFromAddress(record)
  if (s) patch.state = s
  // The record stores zones as integers (the server's coercer rejects fractional/bool/out-of-range inputs as dirty).
  // String-convert here because the entry form's text-input layer is type-agnostic and stringifies on its own.
  // But going through String() keeps the empty-patch semantics consistent: null in, absent from the patch.
  if (record.itu_zone != null) patch.itu_zone = String(record.itu_zone)
  if (record.cq_zone != null) patch.cq_zone = String(record.cq_zone)
  return patch
}
