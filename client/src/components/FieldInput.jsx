// One template-defined field input (ADR-0003 types: text, number, choice).
// Shared by the entry form and the edit modal so fields render identically.
//
// Live validation feedback: green border as soon as the value matches the
// field's pattern; red only latches on blur, and clears while editing —
// mid-typing is never punished. Empty values stay uncolored (emptiness is
// the 'required' flag's job, enforced at submit).
import { forwardRef, useState } from 'react'
import { sanitizeText } from '../text-input.js'

// full-match semantics, same as contact-validation.js
function matches(pattern, value) {
  return new RegExp(`^(?:${pattern})$`).test(value)
}

const FieldInput = forwardRef(function FieldInput(
  { field, value, onChange, placeholder, onKeyDown },
  ref,
) {
  const [focused, setFocused] = useState(false)
  const trimmed = String(value ?? '').trim()
  let cls
  if (field.validation && trimmed) {
    if (matches(field.validation.pattern, trimmed)) cls = 'v-ok'
    else if (!focused) cls = 'v-bad'
  }
  const feedback = field.validation && {
    className: cls,
    title: field.validation.message,
    onFocus: () => setFocused(true),
    onBlur: () => setFocused(false),
  }
  // Sized so both the longest value (max_length + 2) and the label shown as the placeholder (+ 2) fit.
  // Choice fields have no max_length; their width is label-based.
  const label = placeholder ?? field.label
  const width = `${Math.max((field.max_length ?? 0) + 2, label.length + 2)}ch`
  if (field.type === 'choice') {
    return (
      <select ref={ref} className="field-input" style={{ width }} value={value} onChange={(e) => onChange(e.target.value)} onKeyDown={onKeyDown} {...feedback}>
        <option value="">{placeholder}</option>
        {(field.options ?? []).map((o) => (
          <option key={o} value={o}>{o}</option>
        ))}
      </select>
    )
  }
  return (
    <input
      className="field-input"
      ref={ref}
      type={field.type === 'number' ? 'number' : 'text'}
      style={{ width }}
      value={value}
      placeholder={placeholder}
      maxLength={field.max_length}
      onChange={(e) =>
        onChange(field.type === 'number' ? e.target.value : sanitizeText(e.target.value).toUpperCase())
      }
      onKeyDown={onKeyDown}
      {...feedback}
    />
  )
})

export default FieldInput
