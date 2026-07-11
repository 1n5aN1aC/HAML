// Entry form: remote callsign + the Event's template fields. Writes straight
// to Dexie as `pending` (ADR-0001 — local first, sync engine pushes later).
import { useMemo, useRef, useState } from 'react'
import { db, kvGet } from '../db.js'
import { pushNow } from '../sync.js'
import { newUuid } from '../uuid.js'
import { validateContact } from '../contact-validation.js'
import FieldInput from './FieldInput.jsx'

function defaultValues(fields) {
  return Object.fromEntries(fields.map((f) => [f.name, f.default ?? '']))
}

export default function ContactEntryForm({ config, session, clientUuid, disabled }) {
  const fields = useMemo(
    () => [...config.fields].sort((a, b) => a.order - b.order),
    [config],
  )
  const [callsign, setCallsign] = useState('')
  const [values, setValues] = useState(() => defaultValues(fields))
  const [error, setError] = useState('')
  const callsignRef = useRef(null)

  async function logContact(e) {
    e.preventDefault()
    const problem = validateContact(
      { remote_callsign: callsign, fields: values },
      config,
    )
    if (problem) {
      setError(problem)
      return
    }
    // QSO time defaults from server-corrected time (clock offset, plan §3.3);
    // the offset is written by the sync engine and is absent until first sync.
    const offset = (await kvGet('clock_offset')) ?? 0
    const now = new Date(Date.now() + offset).toISOString()
    await db.contacts.put({
      uuid: newUuid(),
      qso_at: now,
      created_at: now,
      last_edited: now,
      remote_callsign: callsign.trim().toUpperCase(),
      operator_callsign: session.callsign.trim().toUpperCase(),
      operator_initials: session.initials.trim().toUpperCase(),
      client_uuid: clientUuid,
      band: session.band,
      mode: session.mode,
      deleted: false,
      fields: values,
      sync_state: 'pending',
    })
    pushNow()
    setCallsign('')
    setValues(defaultValues(fields))
    setError('')
    callsignRef.current?.focus()
  }

  return (
    <form className="entry-form" onSubmit={logContact}>
      {disabled && (
        <div className="entry-gate">
          Enter your callsign, initials, band, and mode above to start logging.
        </div>
      )}
      <fieldset disabled={disabled}>
        <div className="entry-fields">
          <input
            ref={callsignRef}
            className="cs"
            placeholder="Callsign"
            value={callsign}
            onChange={(e) => setCallsign(e.target.value.toUpperCase())}
            autoFocus
          />
          {fields.map((f) => (
            <FieldInput
              key={f.name}
              field={f}
              value={values[f.name]}
              placeholder={f.label + (f.required ? ' *' : '')}
              onChange={(v) => setValues({ ...values, [f.name]: v })}
            />
          ))}
          <button type="submit">Log it</button>
        </div>
        {error && <div className="entry-error">{error}</div>}
      </fieldset>
    </form>
  )
}
