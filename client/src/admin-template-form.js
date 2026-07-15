// Template editor form logic: converting between the template JSON schema
// (server/templates/example.json documents it) and the flat form state the
// AdminTemplateEditor component renders. Kept out of the component so the tricky
// round-trip rules stay pure and testable.
//
// The editor is one unified, reorderable list of rows. Each row is either a
// custom field definition or a built-in picked from the registry, with Entry
// and History checkboxes. The row order is the template's single `fields`
// order; the entry box and history list both derive from it via each item's
// `entry` / `history` booleans.

import { BUILTINS, isBuiltin } from './builtin-fields.js'

export const DUPLICATE_TYPES = ['any', 'band-mode', 'band-mode-day', 'none']

// Built-in names offered in the row's picker, in registry order.
export const BUILTIN_CHOICES = Object.keys(BUILTINS)

const ID_RE = /^[a-z0-9_-]+$/

// "ARRL Field Day!" -> "arrl-field-day", matching the server's id rule.
export function slugify(name) {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

// "80m, 40m , ,20m" -> ["80m", "40m", "20m"]
function splitList(text) {
  return text
    .split(',')
    .map((v) => v.trim())
    .filter(Boolean)
}

// A blank custom-field row. Both lists on by default (a new field is usually
// wanted in the entry box and the history list).
export function emptyRow() {
  return {
    kind: 'custom',
    name: '',
    label: '',
    required: true,
    remember: true,
    default: '',
    max_length: '',
    pattern: '',
    message: '',
    inEntry: true,
    inHistory: true,
  }
}

// A built-in row for `name`. Label/max_length/pattern come from the registry
// (shown disabled); only required/remember/default/placement are editable.
export function builtinRow(name) {
  return {
    kind: 'builtin',
    name,
    label: '',
    required: false,
    remember: false,
    default: '',
    max_length: '',
    pattern: '',
    message: '',
    inEntry: true,
    inHistory: true,
  }
}

export function emptyForm() {
  return {
    id: '',
    name: '',
    bands: '',
    modes: '',
    duplicate_type: 'band-mode',
    export: '',
    rows: [],
  }
}

// Loaded template JSON -> unified row form state. Each `fields` item maps
// directly to a row in template order; built-in items vs custom items are
// distinguished by the registry.
export function templateToForm(template, id) {
  const fieldByName = new Map((template.fields ?? []).map((f) => [f.name, f]))
  const rows = (template.fields ?? []).map((f) => {
    if (isBuiltin(f.name)) {
      return {
        ...builtinRow(f.name),
        required: f.required ?? false,
        remember: f.remember ?? false,
        default: f.default ?? '',
        inEntry: f.entry ?? false,
        inHistory: f.history ?? false,
      }
    }
    const def = fieldByName.get(f.name) ?? {}
    return {
      kind: 'custom',
      name: f.name,
      label: def.label ?? '',
      required: def.required ?? false,
      remember: def.remember ?? false,
      default: def.default ?? '',
      max_length: def.max_length != null ? String(def.max_length) : '',
      pattern: def.validation?.pattern ?? '',
      message: def.validation?.message ?? '',
      inEntry: f.entry ?? false,
      inHistory: f.history ?? false,
    }
  })
  return {
    id,
    name: template.name,
    bands: template.bands.join(', '),
    modes: template.modes.join(', '),
    duplicate_type: template.duplicate_type,
    export: template.export == null ? '' : JSON.stringify(template.export, null, 2),
    rows,
  }
}

// Form state -> fresh template JSON. A single ordered `fields` array carries
// everything: custom definitions carry their full def plus entry/history;
// built-in references carry their name plus entry/history and any overrides.
export function formToTemplate(form) {
  const fields = []
  for (const row of form.rows) {
    const name = row.name.trim()
    if (row.kind === 'custom') {
      const field = {
        name,
        label: row.label.trim(),
        required: row.required,
        remember: row.remember,
        default: row.default,
        max_length: parseInt(row.max_length, 10),
      }
      if (row.pattern.trim() || row.message.trim()) {
        field.validation = { pattern: row.pattern.trim(), message: row.message.trim() }
      }
      field.entry = !!row.inEntry
      field.history = !!row.inHistory
      fields.push(field)
    } else {
      const item = { name, entry: !!row.inEntry, history: !!row.inHistory }
      if (row.required) item.required = true
      if (row.remember) item.remember = true
      if ((row.default ?? '') !== '') item.default = row.default
      fields.push(item)
    }
  }
  return {
    name: form.name.trim(),
    fields,
    bands: splitList(form.bands),
    modes: splitList(form.modes),
    duplicate_type: form.duplicate_type,
    export: form.export.trim() ? JSON.parse(form.export) : null,
  }
}

// Light save gate: just the pieces whose absence makes saving pointless.
// Everything subtler is left to the server's validation messages.
export function formComplete(form) {
  if (!form.name.trim() || !ID_RE.test(form.id)) return false
  if (!splitList(form.bands).length || !splitList(form.modes).length) return false
  if (form.export.trim()) {
    try {
      JSON.parse(form.export)
    } catch {
      return false
    }
  }
  return true
}