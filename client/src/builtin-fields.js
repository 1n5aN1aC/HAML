// Built-in contact fields: a fixed roster every contact can carry regardless
// of the event's template (server/db.py BUILTIN_FIELDS mirrors this name list;
// a smoke test keeps the two honest). Templates only decide *display* — which
// built-ins (and custom fields) appear in the entry box (entry_list) and the
// contact-log columns (contact_list). This module is the display registry:
// labels, widths, validation patterns, and which built-ins auto-fill from the
// CallParser lookup.

// name -> { label, max_length, validation:{pattern,message}|null, autofill }
// autofill is the CallParser hit key whose value pre-fills the field, or null.
export const BUILTINS = {
  country: {
    label: 'Country', max_length: 40, validation: null, autofill: 'territory',
  },
  itu_zone: {
    label: 'ITU Zone', max_length: 2, autofill: 'itu',
    validation: { pattern: '[1-9]|[1-8]\\d|90', message: 'ITU zone 1–90' },
  },
  cq_zone: {
    label: 'CQ Zone', max_length: 2, autofill: 'cq',
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
      pattern: 'AK|AL|AR|AZ|CA|CO|CT|DC|DE|DX|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|VT|WA|WI|WV|WY',
      message: 'State must be a 2-letter state abbreviation like OR, or DX',
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

// The built-ins pre-filled from the callsign lookup. Stored on every contact
// (template or not); live-filled in the entry box when visible.
export const AUTO_FIELDS = ['country', 'itu_zone', 'cq_zone', 'continent']

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

// One entry_list/contact_list item -> a resolved field def. Object items carry
// per-event overrides (required/remember/default) merged over the base def.
function resolveItem(item, config) {
  const name = typeof item === 'string' ? item : item.name
  const base = isBuiltin(name)
    ? builtinFieldDef(name)
    : (config.fields ?? []).find((f) => f.name === name)
  if (!base) return null
  const override = item && typeof item === 'object' ? item : {}
  return {
    ...base,
    required: override.required ?? base.required ?? false,
    remember: override.remember ?? base.remember ?? false,
    default: override.default ?? base.default ?? '',
  }
}

// Ordered field defs for the callsign-entry box.
export function resolveEntryFields(config) {
  return config.entry_list.map((item) => resolveItem(item, config)).filter(Boolean)
}

// Ordered field defs for the contact-log columns.
export function resolveColumnFields(config) {
  return config.contact_list.map((item) => resolveItem(item, config)).filter(Boolean)
}
