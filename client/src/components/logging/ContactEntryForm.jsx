// Entry form: remote callsign + the Event's entry fields (custom fields
// and built-ins, the template's `fields` items with `entry: true`). Writes
// straight to Dexie as `pending` (docs/CLIENT.md — local first, sync engine pushes
// later). Built-ins are stored as top-level properties on the contact; custom
// fields live in the `fields` blob.
import { useEffect, useMemo, useRef, useState } from 'react'
import { useLiveQuery } from 'dexie-react-hooks'
import { db, kvGet } from '../../db.js'
import { pushNow } from '../../sync.js'
import { newUuid } from '../../uuid.js'
import { validateContact } from '../../contact-validation.js'
import { sanitizeText } from '../../text-input.js'
import { playSubmit, playDuplicate, playDx, playError } from '../../sounds.js'
import { findDuplicate, findLatestContact } from '../../dupes.js'
import { BUILTIN_ORDER, isBuiltin, resolveEntryFields } from '../../builtin-fields.js'
import { SECTION_TO_STATE, STATE_TO_SECTION } from '../../sections.js'
import { lookupCallsign } from '../../api.js'
import { isPlausibleCallsign, lookupPatchFromRecord } from '../../lookup-fill.js'
import FieldInput from './FieldInput.jsx'

// UTC + local wall clock, corrected by the same server clock offset used for
// QSO timestamps so what's shown matches what gets logged.
function EntryClock() {
  // Attach react query so the displayed clock corrects itself when clock_offset is written.
  const offset = useLiveQuery(() => kvGet('clock_offset'), [], 0) ?? 0
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
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

// The full in-memory contact state: the event's entry fields at their defaults,
// plus every built-in the entry fields don't already cover, defaulted to ''.
// Every built-in living in state (visible or hidden) lets background fills —
// the server lookup, remember, and any future async enrichment fetcher —
// write it before submit saves the state verbatim.
function defaultValues(fields) {
  const base = Object.fromEntries(fields.map((f) => [f.name, f.default ?? '']))
  for (const name of BUILTIN_ORDER) {
    if (!(name in base)) base[name] = ''
  }
  return base
}

// Apply patches (name -> value maps) over a values map in order, last writer
// winning, but never overwriting a name the operator has touched. The shared
// merge step for both the blur fills and the submit-time final values.
function mergeUntouched(values, touched, ...patches) {
  const next = { ...values }
  for (const patch of patches) {
    for (const [name, v] of Object.entries(patch)) {
      if (touched.has(name)) continue
      next[name] = v
    }
  }
  return next
}

// Cross-fill patch: derive state from section and/or section from state, as a
// name -> value patch over the current values. section -> state is authoritative
// (every mapped section has one state); state -> section only fires for states
// owning a single section (SECTION_TO_STATE/STATE_TO_SECTION handle the omissions).
// Emits nothing for unmapped/partial values. mergeUntouched applies it, so it
// never overwrites a touched field and never marks the counterpart touched.
function crossFillPatch(values) {
  const patch = {}
  const state = SECTION_TO_STATE[String(values.section ?? '').trim()]
  if (state) patch.state = state
  const section = STATE_TO_SECTION[String(values.state ?? '').trim()]
  if (section) patch.section = section
  return patch
}

// Reset every untouched field to its empty-state default without touching anything the operator typed into
// (a touched field keeps its value and its touched flag, so later fills still leave it alone).
// Apply on every callsign text change so in-flight async patches (server lookup, remember) are wiped before the
// next contact's fills land — submitting before the new fills arrive then logs blanks, never the previous station's data.
function clearUntouchedFields(prev, touched, fields, hiddenBuiltins) {
  const cleared = { ...prev }
  for (const f of fields) {
    if (!touched.has(f.name)) cleared[f.name] = f.default ?? ''
  }
  // hidden built-ins have no entry input, so they can never be touched
  for (const name of hiddenBuiltins) cleared[name] = ''
  return cleared
}

// same style as the entry clock's local half ("11:42 AM")
function formatLocalTime(iso) {
  const d = new Date(iso)
  if (isNaN(d)) return iso
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true })
}

export default function ContactEntryForm({ config, session, clientUuid, disabled }) {
  const fields = useMemo(() => resolveEntryFields(config), [config])
  // built-ins with no entry input of their own, reset alongside the entry
  // fields when the callsign is cleared so hidden fills can't leak forward
  const hiddenBuiltins = useMemo(() => {
    const entryNames = new Set(fields.map((f) => f.name))
    return BUILTIN_ORDER.filter((name) => !entryNames.has(name))
  }, [fields])
  const [callsign, setCallsign] = useState('')
  const [values, setValues] = useState(() => defaultValues(fields))
  // names the operator typed into by hand — at submit, a touched auto field's
  // value wins over the freshly recomputed lookup value
  const [touched, setTouched] = useState(() => new Set())
  // Live mirror `touched` for async fills that resolve long after they fired (the server lookup can take up to 15s):
  // merging with the closure's stale `touched` would overwrite fields the operator typed into while the request was in flight.
  const touchedRef = useRef(touched)
  touchedRef.current = touched
  const [error, setError] = useState('')
  const [dupe, setDupe] = useState(null)
  const callsignRef = useRef(null)
  const fieldRefs = useRef([])
  fieldRefs.current = fields.map((_, i) => fieldRefs.current[i] ?? null)
  // Pending debounced server-lookup POST only cancelled by the next change, by Escape, and on unmount.
  const idleTimerRef = useRef(null)
  // The callsign text each in-flight server lookup was fired for.
  // Updated on every change, reset to '' on submit;
  // a response lands only when this still equals the box's current content.
  // Covers both operator edits (the lookup fires for the old text) and post-submit (box goes to '').
  const callsignLiveRef = useRef('')

  // Drop any pending debounced server lookup on unmount so its setValues never fires into an unmounted tree.
  useEffect(() => () => {
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
  }, [])

  // The most recent successful server lookup record: drives the country + miles
  // label (recomputed only when the record changes, not per keystroke). Cleared
  // on every callsign text change, on submit, and on Escape so a stale label
  // never sits next to a new contact.
  const [serverRecord, setServerRecord] = useState(null)

  // Country + distance label. The server returns a canonical record with
  // `country` (always set on a hit), `continent`, and a request-time `distance`
  // in km when the event has a location; we show country + miles when both
  // are present, country alone when the event has no location, nothing on a miss.
  const callStatus = useMemo(() => {
    if (!serverRecord?.country) return ''
    const km = serverRecord.distance
    if (km == null) return serverRecord.country
    const mi = Math.round(km * 0.621371)
    return `${serverRecord.country} (${mi.toLocaleString()} mi)`
  }, [serverRecord])

  // Log fields never contain spaces, so Space doubles as "next field" (wrapping, like Tab) instead of typing a literal space.
  // A freetext field (comment) opts out via allowSpace and takes the literal space; Tab still moves on.
  // Escape in any entry field resets the whole form and returns focus to the callsign box.
  function handleFieldNav(e, index, order, allowSpace = false) {
    if (e.key === ' ' && !allowSpace) {
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
    } else if (e.key === 'Escape') {
      e.preventDefault()
      resetForm()
    }
  }

  // Advisory dupe check, fired when the callsign box loses focus (Tab, Space
  // navigation, or a click elsewhere). Warns but never blocks logging
  // (docs/SERVER.md, Template). The banner clears when the callsign text changes or on log.
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

  // "remember" fill: the most recent contact's values for the remember-enabled
  // fields, as a name -> value patch. Built-ins read from the contact's
  // top-level columns, customs from its `fields` blob; empty sources and a
  // no-match callsign yield an empty patch (nothing to remember).
  async function rememberPatch(call) {
    if (!call) return {}
    const latest = await findLatestContact(call)
    if (!latest) return {}
    const patch = {}
    for (const f of fields) {
      if (!f.remember) continue
      const v = isBuiltin(f.name) ? latest[f.name] : latest.fields?.[f.name]
      if (v != null && String(v).trim()) patch[f.name] = v
    }
    return patch
  }

  // Blur fills: apply the remember patch when its Dexie scan resolves.
  // The lookup patch is gone — server-lookup fills land via the debounced
  // fireServerLookup path in the callsign onChange (cancel-and-relaunch on
  // each keystroke), and any value already merged into `values` from an
  // in-flight server response survives untouched through submit's final merge.
  function applyBlurFills() {
    if (!callsign) return
    rememberPatch(callsign).then((remember) => {
      // Race guard, same as the server lookup: if the callsign was edited, cleared (Escape),
      // or submitted while the Dexie scan was in flight, this patch belongs to the old station — drop it.
      if (callsignLiveRef.current !== callsign) return
      // touchedRef, not the closure's touched: the Dexie scan resolves after
      // this render, and fields typed into meanwhile must win.
      setValues((prev) => mergeUntouched(prev, touchedRef.current, remember))
    })
  }

  // Fired when a section/state entry field loses focus: derive the counterpart
  // (state from section, or section from state) into an untouched field, so it
  // populates visibly as soon as the operator leaves the box. Submit re-runs the
  // same patch, so hidden counterparts and edit-then-Enter are covered there.
  function applyCrossFill() {
    setValues((prev) => mergeUntouched(prev, touched, crossFillPatch(prev)))
  }

  // Fire an async server callsign-lookup POST and apply the patch on success.
  // The only callsign-lookup the form drives. Skips non-plausible callsigns
  // and silently swallows every rejection (404/408/502 plus network errors):
  // enrichment is best-effort, never blocks.
  // Race guard: if the operator has changed the callsign since the request fired, the response is dropped.
  function fireServerLookup(call) {
    if (!isPlausibleCallsign(call)) return
    lookupCallsign(call)
      .then((record) => {
        if (callsignLiveRef.current !== call) return
        const patch = lookupPatchFromRecord(record)
        setValues((prev) => {
          // touchedRef, not the closure's touched: the response can land long after this render, and fields typed into meanwhile must win.
          const nowTouched = touchedRef.current
          const merged = mergeUntouched(prev, nowTouched, patch)
          // Live cross-fill: a freshly-derived state should populate its
          // section on screen, the same way submit does for the saved row.
          return mergeUntouched(merged, nowTouched, crossFillPatch(merged))
        })
        // Cache the record so the country/distance label can render from it.
        setServerRecord(record)
      })
      .catch(() => { /* silent miss — see comment above */ })
  }

  async function logContact(e) {
    e.preventDefault()
    // Run the full blur pipeline over the current state so an edit-then-Enter
    // with no blur still logs the right values: dupe check (sounds and all —
    // the banner it sets is wiped by the reset below), then the lookup and
    // remember fills over untouched fields. The merged map is saved verbatim.
    await checkDuplicate()
    // Remember first, then cross-fill last over the merged interim so a
    // section-derived state beats a remembered one and a remembered section can
    // still derive state (last acting wins). Cross-fill also reaches hidden
    // built-ins (e.g. hidden state from a visible section on section-only events).
    // Server-lookup fills already merged into `values` survive untouched through
    // the submit-time merge, so re-running them here would just be redundant.
    const merged = mergeUntouched(
      values, touched, await rememberPatch(callsign),
    )
    const finalValues = mergeUntouched(merged, touched, crossFillPatch(merged))
    const problem = validateContact(
      { remote_callsign: callsign, values: finalValues },
      fields,
    )
    if (problem) {
      playError()
      setError(problem)
      return
    }
    // Split the merged values into built-in columns and the custom `fields`
    // blob: every built-in key (entry field or hidden) becomes a top-level
    // column, and the remaining entry fields are customs.
    const builtins = {}
    const customFields = {}
    for (const [name, v] of Object.entries(finalValues)) {
      if (isBuiltin(name)) builtins[name] = v
      else customFields[name] = v
    }
    // QSO time defaults from server-corrected time (clock offset, docs/CLIENT.md);
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
    // DX contacts get their own submit sound!
    const isDx = String(finalValues.section ?? '').trim() === 'DX'
      || String(finalValues.state ?? '').trim() === 'DX'
    if (isDx) playDx()
    else playSubmit()
    setCallsign('')
    setValues(defaultValues(fields))
    setTouched(new Set())
    setError('')
    setDupe(null)
    setServerRecord(null) // drop any stale country/miles label so the next QSO starts blank
    // Mirror the cleared callsign box in the live ref so any in-flight server-lookup response is dropped.
    callsignLiveRef.current = ''
    callsignRef.current?.focus()
  }

  // Escape in any entry field wipes the in-progress contact (callsign, every
  // visible + hidden value, touched flags, error, dupe banner, pending server
  // lookup) and returns focus to the callsign box — same end-state as a fresh
  // post-submit form so the operator can start the next QSO immediately.
  function resetForm() {
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
    setCallsign('')
    setValues(defaultValues(fields))
    setTouched(new Set())
    setError('')
    setDupe(null)
    setServerRecord(null) // drop any stale country/miles label
    callsignLiveRef.current = ''
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
              // No .toUpperCase() here: transforming the typed text makes React write a value the DOM
              // doesn't already hold, and that assignment drops the caret to the end mid-edit.
              // CSS uppercases on screen and submit uppercases what's stored; every consumer
              // (dupes, the server) normalizes case on its own.
              const next = sanitizeText(e.target.value)
              // Mirror the live value for the server-lookup race guard.
              callsignLiveRef.current = next
              if (next !== callsign) {
                // Every callsign text change resets every untouched field, not just remember/auto fills.
                // Stale server-lookup patches or remember fills from the previous station don't carry forward into the next contact.
                // Fully emptying the field goes further: ALL fields clear, and leavs no touched flags,
                const effectiveTouched = next ? touched : new Set()
                setValues((prev) => clearUntouchedFields(prev, effectiveTouched, fields, hiddenBuiltins))
                // Drop any cached server record so the country/miles label clears
                // immediately on edit — the next lookup will repopulate it.
                setServerRecord(null)
                if (!next) {
                  setTouched(new Set())
                  setError('')
                }
              }
              setCallsign(next)
              setDupe(null)
              // (Re)start the idle debounce that fires fireServerLookup 100ms after typing settles
              if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
              idleTimerRef.current = setTimeout(() => fireServerLookup(next), 100)
            }}
            onBlur={() => {
              // No lookup here: Idle debounce from last keystroke survives blur and fires on its own.
              checkDuplicate()
              applyBlurFills()
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
              placeholder={f.label}
              onChange={(v) => {
                setValues((prev) => ({ ...prev, [f.name]: v }))
                setTouched((prev) =>
                  prev.has(f.name) ? prev : new Set(prev).add(f.name),
                )
              }}
              onKeyDown={(e) =>
                handleFieldNav(e, i + 1, [callsignRef.current, ...fieldRefs.current], f.freetext)
              }
              // leaving section/state derives the counterpart (state <-> section)
              onBlur={
                f.name === 'section' || f.name === 'state' ? applyCrossFill : undefined
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
