// Strip anything but letters/digits — used to sanitize typed or pasted input
// in callsign/initials/free-text fields, since onChange fires with the final
// value in both cases.
export function alphanumeric(value) {
  return value.replace(/[^a-zA-Z0-9]/g, '')
}
