// Shared contact validation — the single place entry-form and edit-modal
// rules live, so new rules apply to both automatically.

// Returns an error string, or null when the contact is valid.
export function validateContact({ remote_callsign, fields }, config) {
  const errors = []
  const missing = config.fields
    .filter((f) => f.required && !String(fields[f.name] ?? '').trim())
    .map((f) => f.label)
  if (!String(remote_callsign ?? '').trim()) missing.unshift('Callsign')
  if (missing.length) errors.push(`Required: ${missing.join(', ')}`)
  for (const f of config.fields) {
    if (!f.validation) continue
    const value = String(fields[f.name] ?? '').trim()
    if (!value) continue // emptiness is the 'required' flag's job
    // full-match semantics, like the HTML input pattern attribute
    if (!new RegExp(`^(?:${f.validation.pattern})$`).test(value)) {
      errors.push(f.validation.message)
    }
  }
  return errors.length ? errors.join('; ') : null
}
