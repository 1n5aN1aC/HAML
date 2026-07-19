// Built-in contact fields: a fixed roster every contact can carry regardless
// of the event's template (server/db.py BUILTIN_FIELDS mirrors this name list;
// a smoke test keeps the two honest).
// 
// Templates declare one ordered `fields` list mixing custom field definitions
// and built-in references; each item opts into the callsign entry box (`entry`)
// and/or the history list (`history`) via two required booleans,
// and row/array order is the one order shared by both.
// 
// This module is the display registry: labels, widths, and
// validation patterns only — ALL autofill (country, continent, distance,
// zones, state, county, gridsquare, name) flows through `lookup-fill.js`
// against the server `POST /api/lookup` response.

// Signal reports come in two shapes, so both RST fields share one pattern:
// a 3-digit RST (`599`), or a 1-2 digit dB report with optional sign
// (`0`, `16`, `+5`, `+05`, `-13`) as FT8/JT modes use. The dB alternative
// subsumes 2-digit RST (`59`), which is why the first branch is 3-digit only.
const RST = '[1-5][1-9]\\d|[+-]?\\d{1,2}'
const RST_MESSAGE = 'RST: 59 or 599, or dB like -13 or +05'

// name -> { label, max_length, validation:{pattern,message}|null }
export const BUILTINS = {
  country: {
    label: 'Country', max_length: 40, validation: null,
  },
  itu_zone: {
    label: 'ITU Zone', max_length: 2,
    validation: { pattern: '[1-9]|[1-8]\\d|90', message: 'ITU zone 1–90' },
  },
  cq_zone: {
    label: 'CQ Zone', max_length: 2,
    validation: { pattern: '[1-9]|[1-3]\\d|40', message: 'CQ zone 1–40' },
  },
  continent: {
    label: 'Continent', max_length: 2,
    validation: {
      pattern: 'AF|AN|AS|EU|NA|OC|SA',
      message: 'Continent must be AF, AN, AS, EU, NA, OC, or SA',
    },
  },
  gridsquare: {
    label: 'Grid', max_length: 4,
    validation: {
      pattern: '[A-R]{2}\\d{2}',
      message: 'Maidenhead grid like CN84',
    },
  },
  distance: {
    label: 'Distance (km)', max_length: 5,
    validation: { pattern: '\\d{1,5}', message: 'Whole kilometers, like 79' },
  },
  state: {
    label: 'State', max_length: 2,
    validation: {
      pattern: 'AB|AK|AL|AR|AZ|BC|CA|CO|CT|DC|DE|DX|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MB|MD|ME|MI|MN|MO|MS|MT|NB|NC|ND|NE|NH|NJ|NL|NM|NS|NT|NU|NV|NY|OH|OK|ON|OR|PA|PE|QC|RI|SC|SD|SK|TN|TX|UT|VA|VT|WA|WI|WV|WY|YT',
      message: 'State must be the 2-letter code',
    },
  },
  section: {
    label: 'Section', max_length: 3,
    validation: {
      pattern: 'AB|AK|AL|AR|AZ|BC|CO|CT|DE|DX|EB|EMA|ENY|EPA|EWA|GA|GH|IA|ID|IL|IN|KS|KY|LA|LAX|MB|MDC|ME|MI|MN|MO|MS|MT|MX|NB|NC|ND|NE|NFL|NH|NL|NLI|NM|NNJ|NNY|NS|NTX|NV|OH|OK|ONE|ONN|ONS|OR|ORG|PAC|PE|PR|QC|RI|SB|SC|SCV|SD|SDG|SF|SFL|SJV|SK|SNJ|STX|SV|TER|TN|UT|VA|VI|VT|WCF|WI|WMA|WNY|WPA|WTX|WV|WWA|WY',
      message: 'Improper Section',
    },
  },
  county: {
    label: 'County', max_length: 30, validation: null,
  },
  rst_sent: {
    label: 'RST Sent', max_length: 3,
    validation: { pattern: RST, message: RST_MESSAGE },
  },
  rst_received: {
    label: 'RST Rcvd', max_length: 3,
    validation: { pattern: RST, message: RST_MESSAGE },
  },
  name: {
    label: 'Name', max_length: 20, validation: null,
  },
  frequency: {
    label: 'Frequency', max_length: 10,
    validation: { pattern: '(?:[1-9]\\d{0,3}|0)\\.\\d{3}', message: 'Frequency in MHz, like 14.250' },
  },
  comment: {
    label: 'Comment', max_length: 200, validation: null,
  },
}

// Registry order — drives the edit modal's "remaining built-ins" section.
export const BUILTIN_ORDER = Object.keys(BUILTINS)

export function isBuiltin(name) {
  return Object.prototype.hasOwnProperty.call(BUILTINS, name)
}

// A field def (FieldInput / validation shape) for a built-in, at its registry
// defaults. required/remember/default are the neutral all-optional baseline.
export function builtinFieldDef(name) {
  const b = BUILTINS[name]
  return {
    name,
    label: b.label,
    max_length: b.max_length,
    validation: b.validation ?? undefined,
    required: false,
    remember: false,
    default: '',
  }
}

// Read a contact's value for a field, from the right place: built-ins are
// top-level columns, custom fields live in the `fields` blob.
export function readFieldValue(contact, name) {
  return isBuiltin(name) ? contact[name] : contact.fields?.[name]
}

// One `fields` item -> a resolved field def. Built-in refs pull label /
// max_length / validation from the registry; custom items already carry their
// own def. Per-item required/remember/default overlay the registry baseline.
export function resolveField(item) {
  if (isBuiltin(item.name)) {
    const base = builtinFieldDef(item.name)
    return {
      ...base,
      required: item.required ?? base.required,
      remember: item.remember ?? base.remember,
      default: item.default ?? base.default,
    }
  }
  return {
    ...item,
    required: item.required ?? false,
    remember: item.remember ?? false,
    default: item.default ?? '',
  }
}

// Ordered field defs for the callsign entry box: items with `entry: true`.
export function resolveEntryFields(config) {
  return (config.fields ?? []).filter((f) => f.entry).map(resolveField)
}

// Ordered field defs for the history list: items with `history: true`.
export function resolveHistoryFields(config) {
  return (config.fields ?? []).filter((f) => f.history).map(resolveField)
}

// Every template field, in order, for the edit modal (which always shows
// everything, plus any unreferenced built-ins afterwards).
export function resolveAllFields(config) {
  return (config.fields ?? []).map(resolveField)
}