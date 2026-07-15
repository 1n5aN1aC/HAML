// Shared contact validation — the single place entry-form and edit-modal
// rules live, so new rules apply to both automatically.
//
// `fields` is the resolved field-def list (from builtin-fields.js) the caller
// is validating against, carrying per-event required flags and validation
// patterns. `values` is a flat name -> value map the caller assembles, pulling
// built-ins from the contact's top-level columns and custom fields from its
// `fields` blob.

// Returns an error string, or null when the contact is valid.
export function validateContact({ remote_callsign, values }, fields) {
  const errors = []
  const missing = fields
    .filter((f) => f.required && !String(values[f.name] ?? '').trim())
    .map((f) => f.label)
  if (!String(remote_callsign ?? '').trim()) missing.unshift('Callsign')
  if (missing.length) errors.push(`Required: ${missing.join(', ')}`)
  for (const f of fields) {
    if (!f.validation) continue
    const value = String(values[f.name] ?? '').trim()
    if (!value) continue // emptiness is the 'required' flag's job
    // full-match semantics, like the HTML input pattern attribute
    if (!new RegExp(`^(?:${f.validation.pattern})$`).test(value)) {
      errors.push(f.validation.message)
    }
  }
  return errors.length ? errors.join('; ') : null
}
