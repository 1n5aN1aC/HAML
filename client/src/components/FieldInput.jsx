// One template-defined field input (ADR-0003 types: text, number, choice).
// Shared by the entry form and the edit modal so fields render identically.
//
// Live validation feedback: green border as soon as the value matches the
// field's pattern; red only latches on blur, and clears while editing —
// mid-typing is never punished. Empty values stay uncolored (emptiness is
// the 'required' flag's job, enforced at submit).
import { useState } from 'react'

// full-match semantics, same as contact-validation.js
function matches(pattern, value) {
  return new RegExp(`^(?:${pattern})$`).test(value)
}

export default function FieldInput({ field, value, onChange, placeholder }) {
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
  if (field.type === 'choice') {
    return (
      <select value={value} onChange={(e) => onChange(e.target.value)} {...feedback}>
        <option value="">{placeholder}</option>
        {(field.options ?? []).map((o) => (
          <option key={o} value={o}>{o}</option>
        ))}
      </select>
    )
  }
  return (
    <input
      type={field.type === 'number' ? 'number' : 'text'}
      value={value}
      placeholder={placeholder}
      onChange={(e) =>
        onChange(field.type === 'number' ? e.target.value : e.target.value.toUpperCase())
      }
      {...feedback}
    />
  )
}
