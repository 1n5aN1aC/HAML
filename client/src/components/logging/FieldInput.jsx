// One template-defined field input (docs/SERVER.md, Template).
// Shared by the entry form and the edit modal so fields render identically.
//
// Live validation feedback:
//    green underline as soon as the value matches the field's pattern
//    red latches on blur and stays — even while editing — until the value is corrected or emptied.
//    Fresh typing before the first blur is never punished.
//    Empty values stay uncolored (emptiness is the 'required' flag's job, enforced at submit).
import { forwardRef, useRef, useState } from 'react'
import { sanitizeText, sanitizeFreeText } from '../../text-input.js'

// full-match semantics, same as contact-validation.js
function matches(pattern, value) {
  return new RegExp(`^(?:${pattern})$`).test(value)
}

// Focusing an empty box spawns the prefix so the operator types only the digits
// Opted in per call site via `autoPrefix`, so the edit modal keeps plain behavior.
const AUTO_PREFIX = { their_park: 'US-' }

// A field left holding only its auto prefix counts as empty.
// Blur clears it, but a submit skips blur, so the submit path uses this to strip it before
// validating (a bare "US-" would otherwise fail the pattern and block logging).
export function stripAutoPrefix(name, value) {
  const p = AUTO_PREFIX[name]
  return p && String(value ?? '').trim() === p ? '' : value
}

const FieldInput = forwardRef(function FieldInput(
  { field, value, onChange, placeholder, onKeyDown, onBlurValidity, onBlur, enterKeyHint = 'send', autoPrefix = false },
  ref,
) {
  const [latchedBad, setLatchedBad] = useState(false)
  const prefix = autoPrefix ? AUTO_PREFIX[field.name] : undefined
  // Merge the forwarded ref with an internal one so we can move the caret after
  // a programmatic prefill (React parks it at 0 on an empty controlled input).
  const innerRef = useRef(null)
  const setRefs = (el) => {
    innerRef.current = el
    if (typeof ref === 'function') ref(el)
    else if (ref) ref.current = el
  }
  const trimmed = String(value ?? '').trim()
  const ok = field.validation && trimmed && matches(field.validation.pattern, trimmed)
  // correcting or emptying the value releases the latch (render-time reset)
  if (latchedBad && (ok || !trimmed)) setLatchedBad(false)
  let cls
  if (ok) cls = 'v-ok'
  else if (latchedBad) cls = 'v-bad'
  const feedback = field.validation && {
    className: cls,
    title: field.validation.message,
  }
  // Single blur handler: latch red / report validity for validated fields
  // (valid/empty reports null), then always fire the plain onBlur passthrough
  // (used by the entry form to auto-derive state <-> section on field exit).
  function handleBlur(e) {
    // A box left holding only the auto prefix (e.g. "US-") counts as empty:
    let effective = value
    if (prefix && String(value).trim() === prefix) {
      onChange('')
      effective = ''
    }
    if (field.validation) {
      const t = String(effective).trim()
      const bad = t && !matches(field.validation.pattern, t)
      setLatchedBad(bad)
      onBlurValidity?.(bad ? field.validation.message : null)
    }
    onBlur?.(e)
  }
  // Focusing an empty prefixed field drops in the prefix, caret after it.
  function handleFocus() {
    if (prefix && !String(value ?? '').trim()) {
      onChange(prefix)
      requestAnimationFrame(() => {
        const el = innerRef.current
        if (el) el.setSelectionRange(el.value.length, el.value.length)
      })
    }
  }
  // If user tpes a letter, assume they want a different country code
  function handleChange(e) {
    let next = field.freetext
      ? sanitizeFreeText(e.target.value)
      : sanitizeText(e.target.value).toUpperCase()
    if (prefix && String(value) === prefix) {
      const added = next.startsWith(prefix) ? next.slice(prefix.length) : next
      if (/^[A-Z]/.test(added)) next = added
    }
    onChange(next)
  }
  // Width sized to the longest value (max_length + 2), plus a fixed allowance for padding + border.
  // When the label is shown in the box as a placeholder, widens to fit too;
  // In the edit modal the label sits outside the box, so no placeholder is passed and the value alone drives the width.
  // Capped at 22ch so a long field (e.g. comment max_length=200, or a multi-park
  // their_park) doesn't dominate the entry row. Longer values still scroll within the box.
  const chars = placeholder
    ? Math.max((field.max_length ?? 0) + 2, placeholder.length + 2)
    : (field.max_length ?? 0) + 2
  const cappedChars = Math.min(chars, 22)
  const width = `calc(${cappedChars}ch + 20px)`
  return (
    <input
      className="field-input"
      ref={setRefs}
      type="text"
      // Soft-keyboard Return label. Entry form passes 'send' or 'next' (advances a field);
      enterKeyHint={enterKeyHint}
      style={{ width, maxWidth: width }}
      value={value}
      placeholder={placeholder}
      maxLength={field.max_length}
      // Log data is uppercased and stripped to callsign-safe characters; a
      // freetext field (comment) keeps the operator's prose as typed.
      onChange={handleChange}
      onFocus={handleFocus}
      onKeyDown={onKeyDown}
      onBlur={handleBlur}
      {...feedback}
    />
  )
})

export default FieldInput
