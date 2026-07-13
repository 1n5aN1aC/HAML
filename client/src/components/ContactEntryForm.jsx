// Entry form: remote callsign + the Event's template fields. Writes straight
// to Dexie as `pending` (ADR-0001 — local first, sync engine pushes later).
import { useEffect, useMemo, useRef, useState } from 'react'
import { db, kvGet } from '../db.js'
import { pushNow } from '../sync.js'
import { newUuid } from '../uuid.js'
import { validateContact } from '../contact-validation.js'
import { sanitizeText } from '../text-input.js'
import { playSubmit, playDuplicate } from '../sounds.js'
import { findDuplicate, findLatestContact } from '../dupes.js'
import FieldInput from './FieldInput.jsx'

// UTC + local wall clock, corrected by the same server clock offset used for
// QSO timestamps so what's shown matches what gets logged.
function EntryClock() {
  const [offset, setOffset] = useState(0)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    kvGet('clock_offset').then((v) => setOffset(v ?? 0))
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])
  const d = new Date(now + offset)
  const utc = d.toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC',
  })
  const local = d.toLocaleTimeString('en-US', {
    hour: 'numeric', minute: '2-digit', hour12: true,
  })
  return <span className="entry-clock">{utc} / {local}</span>
}

function defaultValues(fields) {
  return Object.fromEntries(fields.map((f) => [f.name, f.default ?? '']))
}

// same style as the entry clock's local half ("11:42 AM")
function formatLocalTime(iso) {
  const d = new Date(iso)
  if (isNaN(d)) return iso
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true })
}

export default function ContactEntryForm({ config, session, clientUuid, disabled }) {
  const fields = useMemo(
    () => [...config.fields].sort((a, b) => a.order - b.order),
    [config],
  )
  const [callsign, setCallsign] = useState('')
  const [values, setValues] = useState(() => defaultValues(fields))
  const [error, setError] = useState('')
  const [dupe, setDupe] = useState(null)
  const callsignRef = useRef(null)
  const fieldRefs = useRef([])
  fieldRefs.current = fields.map((_, i) => fieldRefs.current[i] ?? null)

  // Fields never contain spaces, so Space doubles as "next field" (wrapping,
  // like Tab) instead of typing a literal space.
  function handleFieldNav(e, index, order) {
    if (e.key === ' ') {
      e.preventDefault()
      order[(index + 1) % order.length]?.focus()
    } else if (e.key === 'Tab') {
      if (!e.shiftKey && index === order.length - 1) {
        e.preventDefault()
        order[0]?.focus()
      } else if (e.shiftKey && index === 0) {
        e.preventDefault()
        order[order.length - 1]?.focus()
      }
    }
  }

  // Advisory dupe check, fired when the callsign box loses focus (Tab, Space
  // navigation, or a click elsewhere). Warns but never blocks logging
  // (ADR-0003). The banner clears when the callsign text changes or on log.
  async function checkDuplicate() {
    if (!callsign) return
    const offset = (await kvGet('clock_offset')) ?? 0
    const match = await findDuplicate({
      callsign,
      band: session.band,
      mode: session.mode,
      duplicateType: config.duplicate_type,
      nowMs: Date.now() + offset,
    })
    setDupe(match)
    if (match) playDuplicate()
  }

  // "remember" autofill, fired on the same blur: copy the most recent
  // contact's values into remember-enabled fields, overwriting what's there.
  // No match leaves the fields alone; empty source values are skipped.
  async function autofillRemembered() {
    if (!callsign) return
    const latest = await findLatestContact(callsign)
    if (!latest) return
    setValues((prev) => {
      const next = { ...prev }
      for (const f of fields) {
        const v = latest.fields?.[f.name]
        if (f.remember && v != null && String(v).trim()) next[f.name] = v
      }
      return next
    })
  }

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
    playSubmit()
    setCallsign('')
    setValues(defaultValues(fields))
    setError('')
    setDupe(null)
    callsignRef.current?.focus()
  }

  return (
    <form className="entry-form" onSubmit={logContact}>
      <fieldset disabled={disabled}>
        <div className="entry-fields">
          <input
            ref={callsignRef}
            className="cs"
            placeholder="Callsign"
            value={callsign}
            onChange={(e) => {
              setCallsign(sanitizeText(e.target.value).toUpperCase())
              setDupe(null)
            }}
            onBlur={() => {
              checkDuplicate()
              autofillRemembered()
            }}
            onKeyDown={(e) =>
              handleFieldNav(e, 0, [callsignRef.current, ...fieldRefs.current])
            }
            autoFocus
          />
          {fields.map((f, i) => (
            <FieldInput
              key={f.name}
              ref={(el) => (fieldRefs.current[i] = el)}
              field={f}
              value={values[f.name]}
              placeholder={f.label + (f.required ? ' *' : '')}
              onChange={(v) => setValues({ ...values, [f.name]: v })}
              onKeyDown={(e) =>
                handleFieldNav(e, i + 1, [callsignRef.current, ...fieldRefs.current])
              }
            />
          ))}
          {/* invisible: keeps Enter-to-submit working without multiple
              fields blocking implicit submission (no visible button) */}
          <button type="submit" className="sr-only" tabIndex={-1} aria-hidden="true" />
          <EntryClock />
        </div>
        {/* a submit error takes the slot over the dupe banner while shown */}
        {!disabled && (dupe && !error ? (
          <div className="entry-dupe">
            ⚠ DUPLICATE — {dupe.remote_callsign} logged on {dupe.band} {dupe.mode} at{' '}
            {formatLocalTime(dupe.qso_at)}
          </div>
        ) : (
          <div className="entry-error">{error}</div>
        ))}
      </fieldset>
      {/* outside the fieldset so it isn't dimmed by fieldset:disabled */}
      {disabled && (
        <div className="entry-gate">
          Enter your callsign, initials, band, and mode above to start logging.
        </div>
      )}
    </form>
  )
}
