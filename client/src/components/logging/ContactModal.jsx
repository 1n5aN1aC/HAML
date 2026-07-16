// Edit/delete modal for one contact. Saving and deleting are ordinary sync
// writes (ADR-0001): bump last_edited, mark pending, stamp this machine's
// Client UUID as last editor (ADR-0004), and let the push/pull loops converge.
import { useEffect, useState } from 'react'
import { db, kvGet } from '../../db.js'
import { pushNow } from '../../sync.js'
import { validateContact } from '../../contact-validation.js'
import { sanitizeText } from '../../text-input.js'
import {
  BUILTIN_ORDER, builtinFieldDef, isBuiltin, resolveAllFields,
} from '../../builtin-fields.js'
import { SECTION_TO_STATE, STATE_TO_SECTION } from '../../sections.js'
import FieldInput from './FieldInput.jsx'

// ISO ↔ datetime-local strings. UTC variant treats the input as UTC; local
// variant treats the input as the browser's local time.
const isoToUtcInput = (iso) => new Date(iso).toISOString().slice(0, 16)
const isoToLocalInput = (iso) => {
  const d = new Date(iso)
  if (isNaN(d)) return ''
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}
const utcInputToIso = (value) => new Date(value + 'Z').toISOString()
const localInputToIso = (value) => {
  // datetime-local has no zone; the browser parses it as local time.
  const d = new Date(value)
  return isNaN(d) ? '' : d.toISOString()
}

export default function ContactModal({ contact, config, clientUuid, onClose }) {
  const [form, setForm] = useState({
    qso_at: contact.qso_at,
    qso_at_local: isoToLocalInput(contact.qso_at),
    qso_at_utc: isoToUtcInput(contact.qso_at),
    remote_callsign: contact.remote_callsign,
    operator_callsign: contact.operator_callsign,
    operator_initials: contact.operator_initials,
    band: contact.band,
    mode: contact.mode,
    fields: { ...contact.fields },
    // built-in columns live top-level on the contact; hold them all so every
    // built-in can be edited even when the template lists none
    builtins: Object.fromEntries(BUILTIN_ORDER.map((n) => [n, contact[n] ?? ''])),
  })
  const [error, setError] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)
  // Field names the operator has edited this session:
  // A dirty-state set for the whole modal.  Prevents automation from modifying something the user has already touched.
  // Consumed by automatic logic such as the state<->section derivation
  const [touched, setTouched] = useState(() => new Set())
  const markTouched = (name) =>
    setTouched((prev) => (prev.has(name) ? prev : new Set(prev).add(name)))

  // When the user blurs either time field, re-derive the other from the
  // canonical ISO timestamp kept in qso_at.
  const onUtcBlur = () => {
    const iso = utcInputToIso(form.qso_at_utc)
    if (!iso || iso === form.qso_at) return
    setForm({
      ...form,
      qso_at: iso,
      qso_at_utc: isoToUtcInput(iso),
      qso_at_local: isoToLocalInput(iso),
    })
  }
  const onLocalBlur = () => {
    const iso = localInputToIso(form.qso_at_local)
    if (!iso || iso === form.qso_at) return
    setForm({
      ...form,
      qso_at: iso,
      qso_at_utc: isoToUtcInput(iso),
      qso_at_local: isoToLocalInput(iso),
    })
  }

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const set = (key) => (e) => {
    setForm({ ...form, [key]: e.target.value })
    markTouched(key)
  }
  const setUpper = (key) => (e) => {
    setForm({ ...form, [key]: sanitizeText(e.target.value).toUpperCase() })
    markTouched(key)
  }

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
    const values = {}
    for (const f of allFields) {
      values[f.name] = isBuiltin(f.name)
        ? form.builtins[f.name] ?? ''
        : form.fields[f.name] ?? ''
    }
    const problem = validateContact(
      { remote_callsign: form.remote_callsign, values },
      allFields,
    )
    if (problem) {
      setError(problem)
      return
    }
    await write({
      qso_at: form.qso_at,
      remote_callsign: form.remote_callsign.trim(),
      operator_callsign: form.operator_callsign.trim(),
      operator_initials: form.operator_initials.trim(),
      band: form.band,
      mode: form.mode,
      ...form.builtins, // built-in columns, top-level
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

  // The modal always shows everything: every template field in template order
  // (custom definitions and built-in references alike), then a divider, then
  // any built-ins the template doesn't reference (so every built-in can be
  // edited on demand).
  const templateFields = resolveAllFields(config)
  const referenced = new Set(templateFields.map((f) => f.name))
  const modalBuiltins = BUILTIN_ORDER
    .filter((n) => !referenced.has(n))
    .map(builtinFieldDef)
  const allFields = [...templateFields, ...modalBuiltins]

  // Blur of an edited section/state field derives the counterpart (state from
  // section, or section from state) into an untouched counterpart. Gated on the
  // source being touched, so merely tabbing through never rewrites saved data.
  const deriveCounterpart = (name) => {
    if (!touched.has(name)) return
    const counterpart = name === 'section' ? 'state' : 'section'
    if (touched.has(counterpart)) return
    setForm((prev) => {
      const derived = name === 'section'
        ? SECTION_TO_STATE[String(prev.builtins.section ?? '').trim()]
        : STATE_TO_SECTION[String(prev.builtins.state ?? '').trim()]
      if (!derived) return prev
      return { ...prev, builtins: { ...prev.builtins, [counterpart]: derived } }
    })
  }

  const renderField = (f) => {
    const builtin = isBuiltin(f.name)
    const crossFill = builtin && (f.name === 'section' || f.name === 'state')
    const value = builtin ? form.builtins[f.name] ?? '' : form.fields[f.name] ?? ''
    const onChange = (v) => {
      setForm((prev) =>
        builtin
          ? { ...prev, builtins: { ...prev.builtins, [f.name]: v } }
          : { ...prev, fields: { ...prev.fields, [f.name]: v } },
      )
      markTouched(f.name)
    }
    return (
      <label key={f.name}>
        {f.label}:
        {f.required && '*'}
        <FieldInput
          field={f}
          value={value}
          onChange={onChange}
          onBlur={crossFill ? () => deriveCounterpart(f.name) : undefined}
        />
      </label>
    )
  }

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
            Time (UTC):
            <input
              type="datetime-local"
              value={form.qso_at_utc}
              onChange={(e) => { setForm({ ...form, qso_at_utc: e.target.value }); markTouched('qso_at') }}
              onBlur={onUtcBlur}
            />
          </label>
          <label>
            Time (local):
            <input
              type="datetime-local"
              value={form.qso_at_local}
              onChange={(e) => { setForm({ ...form, qso_at_local: e.target.value }); markTouched('qso_at') }}
              onBlur={onLocalBlur}
            />
          </label>
          <div className="entry-break" />
          <label>
            Operator:
            <input value={form.operator_callsign} onChange={setUpper('operator_callsign')} maxLength={10} />
          </label>
          <label>
            Initials:
            <input value={form.operator_initials} onChange={setUpper('operator_initials')} maxLength={4} />
          </label>
          <hr className="entry-separator" />
          <label>
            Callsign:
            <input className="cs" value={form.remote_callsign} onChange={setUpper('remote_callsign')} maxLength={10} />
          </label>
          <label>
            Band:
            <select value={form.band} onChange={set('band')}>
              {config.bands.map((b) => (
                <option key={b} value={b}>{b}</option>
              ))}
            </select>
          </label>
          <label>
            Mode:
            <select value={form.mode} onChange={set('mode')}>
              {config.modes.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </label>
          <div className="entry-break" />
          {templateFields.map(renderField)}
          {modalBuiltins.length > 0 && <hr className="entry-separator" />}
          {modalBuiltins.map(renderField)}
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
