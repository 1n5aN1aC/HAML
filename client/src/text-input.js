// Strip anything but letters/digits/, - _ . / — used to sanitize typed or
// pasted input in callsign/initials/free-text fields, since onChange fires
// with the final value in both cases ('/' covers portable callsigns like
// W1AW/P). Space stays excluded: it is the entry form's next-field key and
// can never be data.
export function sanitizeText(value) {
  return value.replace(/[^a-zA-Z0-9,_.\/-]/g, '')
}

// Sanitize for fields that hold prose rather than log data (the `comment`
// built-in; `freetext: true` in the registry). Spaces, punctuation, and
// non-ASCII all survive — only control and format characters are dropped, each
// run collapsing to one space so a pasted "line1\r\nline2" lands as
// "line1 line2" in what is still a single-line input.
export function sanitizeFreeText(value) {
  return value.replace(/[\p{Cc}\p{Cf}]+/gu, ' ')
}
