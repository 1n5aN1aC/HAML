// Entry form: remote callsign + the Event's entry fields (custom fields
// and built-ins, the template's `fields` items with `entry: true`). Writes
// straight to Dexie as `pending` (ADR-0001 — local first, sync engine pushes
// later). Built-ins are stored as top-level properties on the contact; custom
// fields live in the `fields` blob.
import { useEffect, useMemo, useRef, useState } from 'react'
import { db, kvGet } from '../../db.js'
import { pushNow } from '../../sync.js'
import { newUuid } from '../../uuid.js'
import { validateContact } from '../../contact-validation.js'
import { sanitizeText } from '../../text-input.js'
import { playSubmit, playDuplicate, playDx } from '../../sounds.js'
import { findDuplicate, findLatestContact } from '../../dupes.js'
import { init as initCallParser, isLoaded, lookup, distanceMiles } from '../../callparser.js'
import {
  AUTO_FIELDS, BUILTINS, isBuiltin, resolveEntryFields,
} from '../../builtin-fields.js'
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

// Zone fields whose CallParser source is zero-padded in the data file ('06'),
// Must strip the padding, or the bare-number validation pattern will reject it.
const ZERO_PADDED = new Set(['itu_zone', 'cq_zone'])

// The auto-filled value for a built-in from a CallParser hit (or '' when no hit / no loaded database).
function autoValue(hit, name) {
  if (!hit) return ''
  const raw = String(hit[BUILTINS[name].autofill] ?? '')
  return ZERO_PADDED.has(name) ? raw.replace(/^0+(?=\d)/, '') : raw
}

// same style as the entry clock's local half ("11:42 AM")
function formatLocalTime(iso) {
  const d = new Date(iso)
  if (isNaN(d)) return iso
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true })
}

export default function ContactEntryForm({ config, session, clientUuid, disabled }) {
  const fields = useMemo(() => resolveEntryFields(config), [config])
  // which auto-fill built-ins have a visible input (live-filled on blur)
  const visibleAutos = useMemo(
    () => fields.filter((f) => AUTO_FIELDS.includes(f.name)).map((f) => f.name),
    [fields],
  )
  const [callsign, setCallsign] = useState('')
  const [values, setValues] = useState(() => defaultValues(fields))
  // names the operator typed into by hand — at submit, a touched auto field's
  // value wins over the freshly recomputed lookup value
  const [touched, setTouched] = useState(() => new Set())
  const [error, setError] = useState('')
  const [dupe, setDupe] = useState(null)
  const callsignRef = useRef(null)
  const fieldRefs = useRef([])
  fieldRefs.current = fields.map((_, i) => fieldRefs.current[i] ?? null)

  // DXCC prefix database, loaded once in the background; until it arrives
  // the country label just stays empty.
  const [parserReady, setParserReady] = useState(isLoaded)
  useEffect(() => {
    initCallParser()
      .then(() => setParserReady(true))
      .catch((err) => console.warn('CallParser init failed:', err))
  }, [])

  // Country + distance label, recomputed per keystroke (the lookup is a
  // synchronous in-memory index walk). Distance needs the event's operator
  // location (config.location) and is suppressed for US contacts (ADIF 291),
  // matching the old app.
  const callStatus = useMemo(() => {
    if (!parserReady || callsign.length < 2) return ''
    const hit = lookup(callsign)
    if (!hit) return ''
    const loc = config.location
    const miles =
      loc && hit.adif !== '291'
        ? distanceMiles(hit, loc.latitude, loc.longitude)
        : null
    return miles != null ? `${hit.territory} (${miles.toLocaleString()} mi)` : hit.territory
  }, [callsign, parserReady, config])

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
  // Built-ins read from the contact's top-level columns, customs from its
  // `fields` blob. No match leaves the fields alone; empty sources are skipped.
  async function autofillRemembered() {
    if (!callsign) return
    const latest = await findLatestContact(callsign)
    if (!latest) return
    setValues((prev) => {
      const next = { ...prev }
      for (const f of fields) {
        if (!f.remember) continue
        const v = isBuiltin(f.name) ? latest[f.name] : latest.fields?.[f.name]
        if (v != null && String(v).trim()) next[f.name] = v
      }
      return next
    })
  }

  // Live-fill visible auto built-ins (country/zones/continent) from the
  // callsign lookup on the same blur, overwriting — parallel to remember.
  function autofillLookup() {
    if (!callsign || !parserReady || visibleAutos.length === 0) return
    const hit = lookup(callsign)
    setValues((prev) => {
      const next = { ...prev }
      for (const name of visibleAutos) next[name] = autoValue(hit, name)
      return next
    })
  }

  async function logContact(e) {
    e.preventDefault()
    const problem = validateContact(
      { remote_callsign: callsign, values },
      fields,
    )
    if (problem) {
      setError(problem)
      return
    }
    // Split the entry values into built-in columns and the custom `fields` blob.
    const builtins = {}
    const customFields = {}
    for (const f of fields) {
      if (isBuiltin(f.name)) builtins[f.name] = values[f.name]
      else customFields[f.name] = values[f.name]
    }
    // Recompute the four autos from the final callsign: a value the operator
    // visibly typed (touched) wins; otherwise the lookup value is authoritative
    // (this also fills the hidden autos, which have no input at all).
    const hit = parserReady ? lookup(callsign) : null
    for (const name of AUTO_FIELDS) {
      if (visibleAutos.includes(name) && touched.has(name)) continue
      builtins[name] = autoValue(hit, name)
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
      ...builtins,
      fields: customFields,
      sync_state: 'pending',
    })
    pushNow()
    // DX contacts get their own submit sound:
    if (String(builtins.section ?? '').trim() === 'DX') playDx()
    else playSubmit()
    setCallsign('')
    setValues(defaultValues(fields))
    setTouched(new Set())
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
            maxLength={10}
            value={callsign}
            onChange={(e) => {
              const next = sanitizeText(e.target.value).toUpperCase()
              // Emptying the callsign undoes any "remember"/auto autofill so the
              // stale values don't carry over to the next contact.
              if (callsign && !next) {
                setValues((prev) => {
                  const cleared = { ...prev }
                  for (const f of fields) {
                    if (f.remember || AUTO_FIELDS.includes(f.name)) {
                      cleared[f.name] = f.default ?? ''
                    }
                  }
                  return cleared
                })
                setTouched(new Set())
                setError('')
              }
              setCallsign(next)
              setDupe(null)
            }}
            onBlur={() => {
              checkDuplicate()
              autofillRemembered()
              autofillLookup()
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
              onChange={(v) => {
                setValues((prev) => ({ ...prev, [f.name]: v }))
                setTouched((prev) =>
                  prev.has(f.name) ? prev : new Set(prev).add(f.name),
                )
              }}
              onKeyDown={(e) =>
                handleFieldNav(e, i + 1, [callsignRef.current, ...fieldRefs.current])
              }
              // invalid on blur puts the rule's message in the error bar; a
              // valid blur clears it only if this field's message is showing,
              // so it never wipes another field's (or a submit) error
              onBlurValidity={(msg) =>
                setError((prev) =>
                  msg ?? (prev === f.validation?.message ? '' : prev),
                )
              }
            />
          ))}
          {callStatus && <span className="call-country">{callStatus}</span>}
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
