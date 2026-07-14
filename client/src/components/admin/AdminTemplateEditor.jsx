// Inline template editor: replaces the admin sections while creating a new
// template or editing an existing one. All JSON <-> form conversion lives in
// admin-template-form.js; validation beyond the save gate is the server's
// job — its 400 messages surface in the error line.
import { useState } from 'react'
import { adminSaveTemplate } from '../../api.js'
import {
  DUPLICATE_TYPES,
  emptyField,
  emptyForm,
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

  function updateField(index, patch) {
    setForm((form) => ({
      ...form,
      fields: form.fields.map((f, i) => (i === index ? { ...f, ...patch } : f)),
    }))
  }

  function addField() {
    update({ fields: [...form.fields, emptyField()] })
  }

  function removeField(index) {
    update({ fields: form.fields.filter((_, i) => i !== index) })
  }

  function moveField(index, delta) {
    const fields = [...form.fields]
    const target = index + delta
    if (target < 0 || target >= fields.length) return
    ;[fields[index], fields[target]] = [fields[target], fields[index]]
    update({ fields })
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
          {form.fields.length === 0 && (
            <p className="placeholder">No fields — contacts log with just the built-ins.</p>
          )}
          {form.fields.map((field, i) => (
            <fieldset className="template-field" key={i}>
              <div className="template-field-row">
                <label>
                  Name
                  <input
                    value={field.name}
                    placeholder="Internal field name"
                    onChange={(e) => updateField(i, { name: e.target.value })}
                  />
                </label>
                <label>
                  Label
                  <input
                    value={field.label}
                    placeholder="Column Name"
                    onChange={(e) => updateField(i, { label: e.target.value })}
                  />
                </label>
                <label>
                  Default
                  <input
                    value={field.default}
                    placeholder="Starts pre-populated"
                    onChange={(e) => updateField(i, { default: e.target.value })}
                  />
                </label>
                <label>
                  Max length
                  <input
                    type="number"
                    min="1"
                    className="template-num"
                    value={field.max_length}
                    onChange={(e) => updateField(i, { max_length: e.target.value })}
                  />
                </label>
                <div className="template-field-actions">
                  <button type="button" disabled={i === 0} onClick={() => moveField(i, -1)}>
                    ▲
                  </button>
                  <button
                    type="button"
                    disabled={i === form.fields.length - 1}
                    onClick={() => moveField(i, 1)}
                  >
                    ▼
                  </button>
                  <button type="button" className="btn-danger" onClick={() => removeField(i)}>
                    Remove
                  </button>
                </div>
              </div>
              <div className="template-field-row">
                <label>
                  Validation pattern (regex)
                  <input
                    className="template-pattern"
                    value={field.pattern}
                    placeholder="\d{1,2}[A-Z]{1,4}"
                    onChange={(e) => updateField(i, { pattern: e.target.value })}
                  />
                </label>
                <label>
                  Validation message
                  <input
                    className="template-message"
                    value={field.message}
                    placeholder="Shows when validation fails"
                    onChange={(e) => updateField(i, { message: e.target.value })}
                  />
                </label>
              </div>
              <div className="template-field-row">
                <label className="template-check">
                  <input
                    type="checkbox"
                    checked={field.required}
                    onChange={(e) => updateField(i, { required: e.target.checked })}
                  />
                  Required
                </label>
                <label className="template-check">
                  <input
                    type="checkbox"
                    checked={field.remember}
                    onChange={(e) => updateField(i, { remember: e.target.checked })}
                  />
                  Remember per callsign
                </label>
                <label className="template-check">
                  <input
                    type="checkbox"
                    checked={field.inContactList}
                    onChange={(e) => updateField(i, { inContactList: e.target.checked })}
                  />
                  Show in history list
                </label>
              </div>
            </fieldset>
          ))}
          <button type="button" onClick={addField}>
            Add field
          </button>
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
}
