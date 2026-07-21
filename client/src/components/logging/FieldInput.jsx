// One template-defined field input (docs/SERVER.md, Template).
// Shared by the entry form and the edit modal so fields render identically.
//
// Live validation feedback:
//    green underline as soon as the value matches the field's pattern
//    red latches on blur and stays — even while editing — until the value is corrected or emptied.
//    Fresh typing before the first blur is never punished.
//    Empty values stay uncolored (emptiness is the 'required' flag's job, enforced at submit).
import { forwardRef, useState } from 'react'
import { sanitizeText, sanitizeFreeText } from '../../text-input.js'

// full-match semantics, same as contact-validation.js
function matches(pattern, value) {
  return new RegExp(`^(?:${pattern})$`).test(value)
}

const FieldInput = forwardRef(function FieldInput(
  { field, value, onChange, placeholder, onKeyDown, onBlurValidity, onBlur },
  ref,
) {
  const [latchedBad, setLatchedBad] = useState(false)
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
    if (field.validation) {
      const bad = trimmed && !ok
      setLatchedBad(bad)
      onBlurValidity?.(bad ? field.validation.message : null)
    }
    onBlur?.(e)
  }
  // Width sized to the longest value (max_length + 2), plus a fixed allowance for padding + border.
  // When the label is shown in the box as a placeholder, widens to fit too;
  // In the edit modal the label sits outside the box, so no placeholder is passed and the value alone drives the width.
  // Capped at 42ch so a long-storage field (e.g. comment, max_length=200) doesn't dominate the entry row.
  const chars = placeholder
    ? Math.max((field.max_length ?? 0) + 2, placeholder.length + 2)
    : (field.max_length ?? 0) + 2
  const cappedChars = Math.min(chars, 42)
  const width = `calc(${cappedChars}ch + 20px)`
  return (
    <input
      className="field-input"
      ref={ref}
      type="text"
      style={{ width, maxWidth: width }}
      value={value}
      placeholder={placeholder}
      maxLength={field.max_length}
      // Log data is uppercased and stripped to callsign-safe characters; a
      // freetext field (comment) keeps the operator's prose as typed.
      onChange={(e) => onChange(
        field.freetext
          ? sanitizeFreeText(e.target.value)
          : sanitizeText(e.target.value).toUpperCase(),
      )}
      onKeyDown={onKeyDown}
      onBlur={handleBlur}
      {...feedback}
    />
  )
})

export default FieldInput
