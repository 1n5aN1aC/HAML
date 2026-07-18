// Maps a canonical callsign-lookup record into the entry-form fills that the
// operator's local data can't produce (a DXCC prefix lookup can't tell you
// someone's name, grid square, or state). The record shape is fixed by
// `FIELDS` in server/lookup_record.py — every key always present, null when
// absent, enums lowercased, lat/lon floats, dates ISO, state a 2-letter USPS
// code or null — so this module trusts field names/types and only null-checks
// (or in state's case, code-validates) each value it consumes. Returns `{}`
// for anything unusable so callers can merge it blindly.
//
// Provider-agnostic: no upstream name appears anywhere here. The record is
// the contract; whichever adapter the server talks to today is irrelevant.
//
// State: the server's canonical-record layer has already coerced `state` to
// a 2-letter USPS code (or null), accepting both codes and spelled-out names
// upstream. The client only needs to gate against the entry field's accepted
// codes so a Canadian province or foreign value never lands invalid.
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

// Continent codes the entry field's validation accepts (mirrors
// BUILTINS.continent.validation.pattern in builtin-fields.js), inlined for
// the same no-coupling reason as VALID_STATES above.
const VALID_CONTINENTS = new Set(['AF', 'AN', 'AS', 'EU', 'NA', 'OC', 'SA'])

// First token of a name, title-cased. "JOSHUA A COOK" -> "Joshua". The
// record stores ALL-CAPS last-write from the upstream; we just normalize
// the first token for the entry field.
function firstTokenTitleCased(name) {
  const first = String(name).trim().split(/\s+/)[0]
  if (!first) return ''
  return first.charAt(0).toUpperCase() + first.slice(1).toLowerCase()
}

// 2-letter state straight off the canonical record, or null when absent or not on the entry field's accepted list.
// The server has already coerced codes and spelled-out names to the USPS form; only the entry-field gate remains.
function stateFromRecord(record) {
  const code = record.state
  if (!code) return null
  return VALID_STATES.has(code) ? code : null
}

// { name?, gridsquare?, state?, county?, continent?, distance?, itu_zone?, cq_zone? } —
// only the keys the entry form knows how to fill from a server lookup, only
// when the record carries a usable value. Clubs, military, and RACES
// (license_type !== 'person') deliberately skip the name fill; a null/missing
// record or one without any usable value returns `{}`.
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
  const s = stateFromRecord(record)
  if (s) patch.state = s
  if (record.county) patch.county = record.county
  // Guarded like state: the record's continent is a free string, so only a
  // code the entry field's validation accepts fills; anything else stays
  // blank rather than landing invalid.
  const c = record.continent && String(record.continent).toUpperCase()
  if (c && VALID_CONTINENTS.has(c)) patch.continent = c
  // 0 km is a valid value, hence the explicit null check.
  if (record.distance != null) patch.distance = String(record.distance)
  // The record stores zones as integers (the server's coercer rejects fractional/bool/out-of-range inputs as dirty).
  // String-convert here because the entry form's text-input layer is type-agnostic and stringifies on its own.
  // But going through String() keeps the empty-patch semantics consistent: null in, absent from the patch.
  if (record.itu_zone != null) patch.itu_zone = String(record.itu_zone)
  if (record.cq_zone != null) patch.cq_zone = String(record.cq_zone)
  return patch
}
