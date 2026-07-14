// Template editor form logic: converting between the template JSON schema
// (server/templates/example.json documents it) and the flat form state the
// AdminTemplateEditor component renders. Kept out of the component so the tricky
// round-trip rules stay pure and testable.

export const DUPLICATE_TYPES = ['any', 'band-mode', 'band-mode-day', 'none']

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

export function emptyField() {
  return {
    name: '',
    label: '',
    required: true,
    remember: true,
    inContactList: true,
    default: '',
    max_length: '',
    pattern: '',
    message: '',
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
    fields: [],
  }
}

// Loaded template JSON -> form state. Absent contact_list means every field
// shows, so every checkbox starts checked.
export function templateToForm(template, id) {
  const shown = template.contact_list && new Set(template.contact_list)
  return {
    id,
    name: template.name,
    bands: template.bands.join(', '),
    modes: template.modes.join(', '),
    duplicate_type: template.duplicate_type,
    export: template.export == null ? '' : JSON.stringify(template.export, null, 2),
    fields: [...template.fields]
      .sort((a, b) => a.order - b.order)
      .map((f) => ({
        name: f.name,
        label: f.label,
        required: f.required ?? false,
        remember: f.remember ?? false,
        inContactList: shown ? shown.has(f.name) : true,
        default: f.default ?? '',
        max_length: f.max_length != null ? String(f.max_length) : '',
        pattern: f.validation?.pattern ?? '',
        message: f.validation?.message ?? '',
      })),
  }
}

// Form state -> fresh template JSON. Order comes from list position; a
// validation object is only emitted when either box is filled (the server
// then insists on both); contact_list is omitted when every field is checked
// (absent = show all, so newly added fields keep appearing).
export function formToTemplate(form) {
  const fields = form.fields.map((f, i) => {
    const out = {
      name: f.name.trim(),
      label: f.label.trim(),
      required: f.required,
      remember: f.remember,
      default: f.default,
      order: i + 1,
      max_length: parseInt(f.max_length, 10),
    }
    if (f.pattern.trim() || f.message.trim()) {
      out.validation = { pattern: f.pattern.trim(), message: f.message.trim() }
    }
    return out
  })
  const template = {
    name: form.name.trim(),
    fields,
    bands: splitList(form.bands),
    modes: splitList(form.modes),
    duplicate_type: form.duplicate_type,
    export: form.export.trim() ? JSON.parse(form.export) : null,
  }
  const shown = form.fields.filter((f) => f.inContactList)
  if (shown.length !== form.fields.length) {
    template.contact_list = shown.map((f) => f.name.trim())
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
