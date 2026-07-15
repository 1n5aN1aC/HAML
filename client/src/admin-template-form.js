// Template editor form logic: converting between the template JSON schema
// (server/templates/example.json documents it) and the flat form state the
// AdminTemplateEditor component renders. Kept out of the component so the tricky
// round-trip rules stay pure and testable.
//
// The editor is one unified, reorderable list of rows. Each row is either a
// custom field definition or a built-in picked from the registry, with Entry
// and Column checkboxes. Row order drives both emitted lists: rows with
// `inEntry` become entry_list, rows with `inColumn` become contact_list.

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

// A blank custom-field row. Both display lists on by default (a new field is
// usually wanted in the entry box and the log).
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
    inColumn: true,
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
    inColumn: true,
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

// A display-list item -> its field name (bare string or {name} object).
function itemName(item) {
  return typeof item === 'string' ? item : item.name
}

// name -> the override object item (or {} for a bare-string reference).
function overrideMap(list) {
  const map = new Map()
  for (const item of list ?? []) {
    map.set(itemName(item), typeof item === 'object' ? item : {})
  }
  return map
}

// Loaded template JSON -> unified row form state. Row order follows entry_list,
// then any column-only references, then any custom fields referenced nowhere
// (modal-only). Overrides on entry_list objects win over the field's own
// defaults when reconstructing a row.
export function templateToForm(template, id) {
  const fieldByName = new Map((template.fields ?? []).map((f) => [f.name, f]))
  const entry = overrideMap(template.entry_list)
  const columns = overrideMap(template.contact_list)

  const ordered = [
    ...(template.entry_list ?? []).map(itemName),
    ...(template.contact_list ?? []).map(itemName),
    ...(template.fields ?? []).map((f) => f.name),
  ]
  const seen = new Set()
  const rows = []
  for (const name of ordered) {
    if (seen.has(name)) continue
    seen.add(name)
    const override = entry.get(name) ?? columns.get(name) ?? {}
    const inEntry = entry.has(name)
    const inColumn = columns.has(name)
    if (isBuiltin(name)) {
      rows.push({
        ...builtinRow(name),
        required: override.required ?? false,
        remember: override.remember ?? false,
        default: override.default ?? '',
        inEntry,
        inColumn,
      })
    } else {
      const f = fieldByName.get(name) ?? {}
      rows.push({
        kind: 'custom',
        name,
        label: f.label ?? '',
        required: override.required ?? f.required ?? false,
        remember: override.remember ?? f.remember ?? false,
        default: override.default ?? f.default ?? '',
        max_length: f.max_length != null ? String(f.max_length) : '',
        pattern: f.validation?.pattern ?? '',
        message: f.validation?.message ?? '',
        inEntry,
        inColumn,
      })
    }
  }
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

// A built-in entry_list item: bare name unless an override differs from the
// neutral registry baseline (all-optional, empty default).
function builtinItem(name, row) {
  const item = { name }
  if (row.required) item.required = true
  if (row.remember) item.remember = true
  if ((row.default ?? '') !== '') item.default = row.default
  return Object.keys(item).length === 1 ? name : item
}

// Form state -> fresh template JSON. Custom fields carry their own
// required/remember/default/validation on the field def and are referenced by
// bare name; built-ins live only in the lists, as objects when overridden.
// contact_list is always bare names (columns have no inputs, so no overrides).
export function formToTemplate(form) {
  const fields = []
  const entry_list = []
  const contact_list = []
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
      fields.push(field)
      if (row.inEntry) entry_list.push(name)
    } else if (row.inEntry) {
      entry_list.push(builtinItem(name, row))
    }
    if (row.inColumn) contact_list.push(name)
  }
  const template = {
    name: form.name.trim(),
    fields,
    bands: splitList(form.bands),
    modes: splitList(form.modes),
    duplicate_type: form.duplicate_type,
    entry_list,
    contact_list,
    export: form.export.trim() ? JSON.parse(form.export) : null,
  }
  return template
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
