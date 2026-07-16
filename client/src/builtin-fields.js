// Built-in contact fields: a fixed roster every contact can carry regardless
// of the event's template (server/db.py BUILTIN_FIELDS mirrors this name list;
// a smoke test keeps the two honest). Templates declare one ordered `fields`
// list mixing custom field definitions and built-in references; each item
// opts into the callsign entry box (`entry`) and/or the history list
// (`history`) via two required booleans, and row/array order is the one order
// shared by both. This module is the display registry: labels, widths,
// validation patterns, and which built-ins auto-fill from the CallParser
// lookup.

// name -> { label, max_length, validation:{pattern,message}|null, autofill }
// autofill is the CallParser hit key whose value pre-fills the field, or null.
export const BUILTINS = {
  country: {
    label: 'Country', max_length: 40, validation: null, autofill: 'territory',
  },
  itu_zone: {
    label: 'ITU Zone', max_length: 2, autofill: null,
    validation: { pattern: '[1-9]|[1-8]\\d|90', message: 'ITU zone 1–90' },
  },
  cq_zone: {
    label: 'CQ Zone', max_length: 2, autofill: null,
    validation: { pattern: '[1-9]|[1-3]\\d|40', message: 'CQ zone 1–40' },
  },
  continent: {
    label: 'Continent', max_length: 2, autofill: 'continent',
    validation: {
      pattern: 'AF|AN|AS|EU|NA|OC|SA',
      message: 'Continent must be AF, AN, AS, EU, NA, OC, or SA',
    },
  },
  gridsquare: {
    label: 'Grid', max_length: 4, autofill: null,
    validation: {
      pattern: '[A-R]{2}\\d{2}',
      message: 'Maidenhead grid like CN84',
    },
  },
  state: {
    label: 'State', max_length: 2, autofill: null,
    validation: {
      pattern: 'AB|AK|AL|AR|AZ|BC|CA|CO|CT|DC|DE|DX|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MB|MD|ME|MI|MN|MO|MS|MT|NB|NC|ND|NE|NH|NJ|NL|NM|NS|NT|NU|NV|NY|OH|OK|ON|OR|PA|PE|QC|RI|SC|SD|SK|TN|TX|UT|VA|VT|WA|WI|WV|WY|YT',
      message: 'State must be the 2-letter code',
    },
  },
  section: {
    label: 'Section', max_length: 3, autofill: null,
    validation: {
      pattern: 'AB|AK|AL|AR|AZ|BC|CO|CT|DE|DX|EB|EMA|ENY|EPA|EWA|GA|GH|IA|ID|IL|IN|KS|KY|LA|LAX|MB|MDC|ME|MI|MN|MO|MS|MT|MX|NB|NC|ND|NE|NFL|NH|NL|NLI|NM|NNJ|NNY|NS|NTX|NV|OH|OK|ONE|ONN|ONS|OR|ORG|PAC|PE|PR|QC|RI|SB|SC|SCV|SD|SDG|SF|SFL|SJV|SK|SNJ|STX|SV|TER|TN|UT|VA|VI|VT|WCF|WI|WMA|WNY|WPA|WTX|WV|WWA|WY',
      message: 'Improper Section',
    },
  },
  frequency: {
    label: 'Frequency', max_length: 10, autofill: null,
    validation: { pattern: '(?:[1-9]\\d{0,3}|0)\\.\\d{3}', message: 'Frequency in MHz, like 14.250' },
  },
  rst_sent: {
    label: 'RST Sent', max_length: 3, autofill: null,
    validation: { pattern: '[1-5][1-9]\\d?', message: 'RST like 59 or 599' },
  },
  rst_received: {
    label: 'RST Rcvd', max_length: 3, autofill: null,
    validation: { pattern: '[1-5][1-9]\\d?', message: 'RST like 59 or 599' },
  },
  name: {
    label: 'Name', max_length: 20, validation: null, autofill: null,
  },
}

// Registry order — drives the edit modal's "remaining built-ins" section.
export const BUILTIN_ORDER = Object.keys(BUILTINS)

// The built-ins pre-filled from the CallParser prefix lookup. Stored on every
// contact (template or not); live-filled in the entry box when visible.
// The zones (itu_zone/cq_zone) are deliberately NOT here: a US call area
// doesn't encode where the station is (vanity calls, operators who moved),
// so the prefix database's zone guess is wrong often enough to be worse than
// nothing. They fill exclusively from the server lookup's coordinate-derived
// values (lookup-fill.js) and stay blank when it has no record.
export const AUTO_FIELDS = ['country', 'continent']

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