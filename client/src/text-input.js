// Strip anything but letters/digits/, - _ . / — used to sanitize typed or
// pasted input in callsign/initials/free-text fields, since onChange fires
// with the final value in both cases ('/' covers portable callsigns like
// W1AW/P). Space stays excluded: it is the entry form's next-field key and
// can never be data.
export function sanitizeText(value) {
  return value.replace(/[^a-zA-Z0-9,_.\/-]/g, '')
}
