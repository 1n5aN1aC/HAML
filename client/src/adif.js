// Minimal ADIF parser (client-side import). ADIF is a stream of
// <NAME:length[:type]>value tokens; records end at <EOR>, and an optional
// header (anything before <EOH>) is skipped. No dependencies, no validation —
// interpretation of the fields is the import screen's job.

// Parse a whole ADIF file into raw records: [{ CALL: 'AC9XW', ... }, ...].
// Field names are uppercased; values are sliced exactly to their declared
// length (whitespace between tokens is ignored, per spec).
export function parseAdif(text) {
  // Skip the header if one exists. Files with no <EOH> have no header.
  const eoh = text.search(/<eoh>/i)
  let pos = eoh === -1 ? 0 : eoh + 5
  const records = []
  let current = {}
  const tag = /<([A-Za-z_][A-Za-z0-9_]*)(?::(\d+)(?::[^>]*)?)?>/g
  tag.lastIndex = pos
  let m
  while ((m = tag.exec(text)) !== null) {
    const name = m[1].toUpperCase()
    if (name === 'EOR') {
      if (Object.keys(current).length) records.push(current)
      current = {}
      continue
    }
    const length = m[2] ? parseInt(m[2], 10) : 0
    current[name] = text.slice(tag.lastIndex, tag.lastIndex + length)
    tag.lastIndex += length
  }
  // Flush a trailing record with no closing <EOR> (truncated file, or a writer
  // that omits the final one) instead of silently dropping it — the import
  // screen's usability checks decide whether the partial fields amount to a
  // contact.
  if (Object.keys(current).length) records.push(current)
  return records
}

// QSO_DATE (YYYYMMDD) + TIME_ON (HHMMSS or HHMM), both UTC per the ADIF spec,
// as epoch milliseconds — or null when either is missing/unparseable. Callers
// apply the operator's clock-offset correction and format from here.
export function recordTimestamp(record) {
  const date = record.QSO_DATE
  const time = record.TIME_ON
  if (!/^\d{8}$/.test(date ?? '') || !/^\d{4}(\d{2})?$/.test(time ?? '')) return null
  const iso = `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}` +
    `T${time.slice(0, 2)}:${time.slice(2, 4)}:${time.slice(4, 6) || '00'}Z`
  const ms = Date.parse(iso)
  return Number.isNaN(ms) ? null : ms
}
