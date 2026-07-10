// Edit/delete modal for one contact. Saving and deleting are ordinary sync
// writes (ADR-0001): bump last_edited, mark pending, stamp this machine's
// Client UUID as last editor (ADR-0004), and let the push/pull loops converge.
import { useEffect, useState } from 'react'
import { db, kvGet } from '../db.js'
import { pushNow } from '../sync.js'
import { validateContact } from '../contact-validation.js'
import FieldInput from './FieldInput.jsx'

// ISO ↔ the datetime-local input, treated as UTC on both sides.
const isoToInput = (iso) => new Date(iso).toISOString().slice(0, 16)
const inputToIso = (value) => new Date(value + 'Z').toISOString()

export default function ContactModal({ contact, config, clientUuid, onClose }) {
  const [form, setForm] = useState({
    qso_at: isoToInput(contact.qso_at),
    remote_callsign: contact.remote_callsign,
    operator_callsign: contact.operator_callsign,
    operator_initials: contact.operator_initials,
    band: contact.band,
    mode: contact.mode,
    fields: { ...contact.fields },
  })
  const [error, setError] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value })
  const setUpper = (key) => (e) =>
    setForm({ ...form, [key]: e.target.value.toUpperCase() })

  async function write(changes) {
    const offset = (await kvGet('clock_offset')) ?? 0
    await db.contacts.put({
      ...contact,
      ...changes,
      last_edited: new Date(Date.now() + offset).toISOString(),
      client_uuid: clientUuid,
      sync_state: 'pending',
    })
    pushNow()
    onClose()
  }

  async function save(e) {
    e.preventDefault()
    const problem = validateContact(
      { remote_callsign: form.remote_callsign, fields: form.fields },
      config,
    )
    if (problem) {
      setError(problem)
      return
    }
    await write({
      qso_at: inputToIso(form.qso_at),
      remote_callsign: form.remote_callsign.trim(),
      operator_callsign: form.operator_callsign.trim(),
      operator_initials: form.operator_initials.trim(),
      band: form.band,
      mode: form.mode,
      fields: form.fields,
    })
  }

  async function remove() {
    if (!confirmDelete) {
      setConfirmDelete(true)
      return
    }
    await write({ deleted: true })
  }

  const templateFields = [...config.fields].sort((a, b) => a.order - b.order)

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <form className="modal" onSubmit={save}>
        <div className="modal-header">
          <span>Edit contact</span>
          <button type="button" className="modal-close" onClick={onClose} title="Close">
            ✕
          </button>
        </div>
        <div className="entry-fields">
          <label>
            Time (UTC)
            <input type="datetime-local" value={form.qso_at} onChange={set('qso_at')} />
          </label>
          <label>
            Callsign
            <input className="cs" value={form.remote_callsign} onChange={setUpper('remote_callsign')} />
          </label>
          <label>
            Band
            <select value={form.band} onChange={set('band')}>
              {config.bands.map((b) => (
                <option key={b} value={b}>{b}</option>
              ))}
            </select>
          </label>
          <label>
            Mode
            <select value={form.mode} onChange={set('mode')}>
              {config.modes.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </label>
          {templateFields.map((f) => (
            <label key={f.name}>
              {f.label}
              {f.required && ' *'}
              <FieldInput
                field={f}
                value={form.fields[f.name] ?? ''}
                onChange={(v) => setForm({ ...form, fields: { ...form.fields, [f.name]: v } })}
              />
            </label>
          ))}
          <label>
            Operator
            <input value={form.operator_callsign} onChange={setUpper('operator_callsign')} maxLength={10} />
          </label>
          <label>
            Initials
            <input value={form.operator_initials} onChange={setUpper('operator_initials')} maxLength={4} />
          </label>
        </div>
        {error && <div className="entry-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="btn-danger" onClick={remove}>
            {confirmDelete ? 'Really delete?' : 'Delete'}
          </button>
          <span className="spacer" />
          <button type="button" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn-primary">Save</button>
        </div>
        <div className="modal-footer">
          Created {new Date(contact.created_at).toISOString().replace('T', ' ').slice(0, 19)} UTC
          · last edited {new Date(contact.last_edited).toISOString().replace('T', ' ').slice(0, 19)} UTC
        </div>
      </form>
    </div>
  )
}
