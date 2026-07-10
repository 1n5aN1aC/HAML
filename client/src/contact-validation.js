// Shared contact validation — the single place entry-form and edit-modal
// rules live, so new rules apply to both automatically.

// Returns an error string, or null when the contact is valid.
export function validateContact({ remote_callsign, fields }, config) {
  const missing = config.fields
    .filter((f) => f.required && !String(fields[f.name] ?? '').trim())
    .map((f) => f.label)
  if (!String(remote_callsign ?? '').trim()) missing.unshift('Callsign')
  return missing.length ? `Required: ${missing.join(', ')}` : null
}
