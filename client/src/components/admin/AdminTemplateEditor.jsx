// Inline template editor: replaces the admin sections while creating a new
// template or editing an existing one. All JSON <-> form conversion lives in
// admin-template-form.js; validation beyond the save gate is the server's
// job — its 400 messages surface in the error line.
//
// Fields are one unified, reorderable list. Each row is a custom field
// definition or a built-in picked from a dropdown, with "Show in Entry Box"
// and "Show in History List" checkboxes. Row order drives the template's
// single `fields` order; both the entry box and history list derive from it
// via each item's `entry` / `history` booleans.
import { useState } from 'react'
import { adminSaveTemplate } from '../../api.js'
import { BUILTINS } from '../../builtin-fields.js'
import {
  BUILTIN_CHOICES,
  DUPLICATE_TYPES,
  builtinRow,
  emptyForm,
  emptyRow,
  formComplete,
  formToTemplate,
  slugify,
  templateToForm,
} from '../../admin-template-form.js'

export default function AdminTemplateEditor({
  password,
  templateId, // null = creating a new template
  initial, // loaded template JSON, or null when creating
  existingIds,
  onDone, // onDone(saved: bool) — return to the admin lists
}) {
  const isNew = templateId == null
  const [form, setForm] = useState(() =>
    isNew ? emptyForm() : templateToForm(initial, templateId),
  )
  // Once the admin edits the id by hand, stop auto-slugging it from the name.
  const [idTouched, setIdTouched] = useState(false)
  const [error, setError] = useState('')

  function update(patch) {
    setForm((form) => ({ ...form, ...patch }))
  }

  function updateName(name) {
    update(isNew && !idTouched ? { name, id: slugify(name) } : { name })
  }

  function updateRow(index, patch) {
    setForm((form) => ({
      ...form,
      rows: form.rows.map((r, i) => (i === index ? { ...r, ...patch } : r)),
    }))
  }

  function addCustomRow() {
    update({ rows: [...form.rows, emptyRow()] })
  }

  function addBuiltinRow(name) {
    if (!name) return
    update({ rows: [...form.rows, builtinRow(name)] })
  }

  function removeRow(index) {
    update({ rows: form.rows.filter((_, i) => i !== index) })
  }

  function moveRow(index, delta) {
    const rows = [...form.rows]
    const target = index + delta
    if (target < 0 || target >= rows.length) return
    ;[rows[index], rows[target]] = [rows[target], rows[index]]
    update({ rows })
  }

  async function save(e) {
    e.preventDefault()
    if (
      isNew &&
      existingIds.includes(form.id) &&
      !window.confirm(`A template with id "${form.id}" already exists. Overwrite it?`)
    )
      return
    setError('')
    try {
      await adminSaveTemplate(password, form.id, formToTemplate(form))
      onDone(true)
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div className="tab-page admin-page">
      <form className="template-editor" onSubmit={save}>
        <section className="admin-section">
          <h2>{isNew ? 'New template' : `Edit template — ${templateId}`}</h2>
          <div className="template-globals">
            <label>
              Name
              <input
                value={form.name}
                placeholder="User-friendly name"
                onChange={(e) => updateName(e.target.value)}
                autoFocus
              />
            </label>
            <label>
              Id (filename)
              <input
                value={form.id}
                placeholder="Internal Name"
                disabled={!isNew}
                onChange={(e) => {
                  setIdTouched(true)
                  update({ id: e.target.value })
                }}
              />
            </label>
            <label>
              Bands (comma-separated)
              <input
                value={form.bands}
                placeholder="80m, 40m, 20m"
                onChange={(e) => update({ bands: e.target.value })}
              />
            </label>
            <label>
              Modes (comma-separated)
              <input
                value={form.modes}
                placeholder="CW, Phone, Digital"
                onChange={(e) => update({ modes: e.target.value })}
              />
            </label>
            <label>
              Duplicate warning
              <select
                value={form.duplicate_type}
                onChange={(e) => update({ duplicate_type: e.target.value })}
              >
                {DUPLICATE_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
            <label className="template-export">
              Export mapping
              <textarea
                value={form.export}
                rows={2}
                placeholder="JSON. This will be used to determine the way to export the log for submittal, but is not implemented yet. Leave blank."
                onChange={(e) => update({ export: e.target.value })}
              />
            </label>
          </div>
        </section>

        <section className="admin-section">
          <h2>Fields</h2>
          <p className="placeholder">
            Add custom fields or pick built-ins. <strong>Show in Entry Box</strong>{' '}
            adds the field to the callsign entry form; <strong>Show in History
            List</strong> adds it as a contact-log column. Order here is the
            template's single field order and drives both lists.
          </p>
          {form.rows.length === 0 && (
            <p className="placeholder">No fields yet — contacts log with just the callsign.</p>
          )}
          {form.rows.map((row, i) =>
            row.kind === 'builtin'
              ? renderBuiltinRow(row, i)
              : renderCustomRow(row, i),
          )}
          <div className="template-add-row">
            <button type="button" onClick={addCustomRow}>
              Add custom field
            </button>
            <select
              value=""
              onChange={(e) => {
                addBuiltinRow(e.target.value)
                e.target.value = ''
              }}
            >
              <option value="">Add built-in…</option>
              {BUILTIN_CHOICES.map((name) => (
                <option key={name} value={name}>
                  {BUILTINS[name].label}
                </option>
              ))}
            </select>
          </div>
        </section>

        {error && <p className="admin-error">{error}</p>}
        <div className="template-editor-actions">
          <button type="submit" className="btn-save" disabled={!formComplete(form)}>
            Save template
          </button>
          <button type="button" onClick={() => onDone(false)}>
            Cancel
          </button>
        </div>
      </form>
    </div>
  )

  // reorder/remove controls, shared by both row kinds
  function rowActions(i) {
    return (
      <div className="template-field-actions">
        <button type="button" disabled={i === 0} onClick={() => moveRow(i, -1)}>
          ▲
        </button>
        <button
          type="button"
          disabled={i === form.rows.length - 1}
          onClick={() => moveRow(i, 1)}
        >
          ▼
        </button>
        <button type="button" className="btn-danger" onClick={() => removeRow(i)}>
          Remove
        </button>
      </div>
    )
  }

  // Entry / History placement checkboxes, shared by both row kinds.
  function placementChecks(row, i) {
    return (
      <>
        <label className="template-check">
          <input
            type="checkbox"
            checked={row.inEntry}
            onChange={(e) => updateRow(i, { inEntry: e.target.checked })}
          />
          Show in Entry Box
        </label>
        <label className="template-check">
          <input
            type="checkbox"
            checked={row.inHistory}
            onChange={(e) => updateRow(i, { inHistory: e.target.checked })}
          />
          Show in History List
        </label>
      </>
    )
  }

  function renderCustomRow(row, i) {
    return (
      <fieldset className="template-field" key={i}>
        <div className="template-field-row">
          <label>
            Name
            <input
              value={row.name}
              placeholder="Internal field name"
              onChange={(e) => updateRow(i, { name: e.target.value })}
            />
          </label>
          <label>
            Label
            <input
              value={row.label}
              placeholder="Column Name"
              onChange={(e) => updateRow(i, { label: e.target.value })}
            />
          </label>
          <label>
            Default
            <input
              value={row.default}
              placeholder="Starts pre-populated"
              onChange={(e) => updateRow(i, { default: e.target.value })}
            />
          </label>
          <label>
            Max length
            <input
              type="number"
              min="1"
              className="template-num"
              value={row.max_length}
              onChange={(e) => updateRow(i, { max_length: e.target.value })}
            />
          </label>
          {rowActions(i)}
        </div>
        <div className="template-field-row">
          <label>
            Validation pattern (regex)
            <input
              className="template-pattern"
              value={row.pattern}
              placeholder="\d{1,2}[A-Z]{1,4}"
              onChange={(e) => updateRow(i, { pattern: e.target.value })}
            />
          </label>
          <label>
            Validation message
            <input
              className="template-message"
              value={row.message}
              placeholder="Shows when validation fails"
              onChange={(e) => updateRow(i, { message: e.target.value })}
            />
          </label>
        </div>
        <div className="template-field-row">
          <label className="template-check">
            <input
              type="checkbox"
              checked={row.required}
              onChange={(e) => updateRow(i, { required: e.target.checked })}
            />
            Required
          </label>
          <label className="template-check">
            <input
              type="checkbox"
              checked={row.remember}
              onChange={(e) => updateRow(i, { remember: e.target.checked })}
            />
            Remember per callsign
          </label>
          {placementChecks(row, i)}
        </div>
      </fieldset>
    )
  }

  function renderBuiltinRow(row, i) {
    const reg = BUILTINS[row.name] ?? { label: '', max_length: '', validation: null }
    return (
      <fieldset className="template-field template-builtin" key={i}>
        <div className="template-field-row">
          <label>
            Built-in
            <select
              value={row.name}
              onChange={(e) => updateRow(i, { name: e.target.value })}
            >
              {BUILTIN_CHOICES.map((name) => (
                <option key={name} value={name}>
                  {BUILTINS[name].label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Label
            <input value={reg.label} disabled />
          </label>
          <label>
            Default
            <input
              value={row.default}
              placeholder="Starts pre-populated"
              onChange={(e) => updateRow(i, { default: e.target.value })}
            />
          </label>
          <label>
            Max length
            <input className="template-num" value={reg.max_length} disabled />
          </label>
          {rowActions(i)}
        </div>
        <div className="template-field-row">
          <label>
            Validation pattern (built-in)
            <input
              className="template-pattern"
              value={reg.validation?.pattern ?? '—'}
              disabled
            />
          </label>
          <label className="template-check">
            <input
              type="checkbox"
              checked={row.required}
              onChange={(e) => updateRow(i, { required: e.target.checked })}
            />
            Required
          </label>
          <label className="template-check">
            <input
              type="checkbox"
              checked={row.remember}
              onChange={(e) => updateRow(i, { remember: e.target.checked })}
            />
            Remember per callsign
          </label>
          {placementChecks(row, i)}
        </div>
      </fieldset>
    )
  }
}