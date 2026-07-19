// ADIF import screen — the invisible `import` tab (reached via Settings).
// Everything happens client-side: parse the file, let the importer map
// modes/bands onto the event's lists, pick the operator identity, correct a
// wrong source clock, then write the rows to Dexie as pending and let the
// sync engine push them (ADR-0001 — same path as hand-logged contacts).
import { useMemo, useState } from 'react'
import { db, kvGet } from '../../db.js'
import { pushNow } from '../../sync.js'
import { newUuid } from '../../uuid.js'
import { sanitizeText } from '../../text-input.js'
import { isBuiltin, resolveAllFields } from '../../builtin-fields.js'
import { parseAdif, recordTimestamp } from '../../adif.js'

const BLANK = '(blank)'

// Distinct values of one ADIF field across the records, with counts.
// Missing/empty values group under BLANK so they get a mapping row too.
function breakdown(records, field) {
  const counts = new Map()
  for (const r of records) {
    const v = (r[field] ?? '').trim() || BLANK
    counts.set(v, (counts.get(v) ?? 0) + 1)
  }
  return [...counts.entries()]
}

// Initial mapping for a breakdown: case-insensitive exact match against the
// event's list, else 'Other' when the list has it, else unselected (blocks
// import until the operator picks).
function initialMapping(pairs, options) {
  const byLower = new Map(options.map((o) => [o.toLowerCase(), o]))
  const mapping = {}
  for (const [value] of pairs) {
    mapping[value] = byLower.get(value.toLowerCase()) ?? byLower.get('other') ?? ''
  }
  return mapping
}

// callsign|band|mode|minute — the dupe-skip identity (same-minute fuzz).
const dupeKey = (callsign, band, mode, iso) =>
  `${callsign.toUpperCase()}|${band}|${mode}|${iso.slice(0, 16)}`

const fmtUtc = (ms) => new Date(ms).toISOString().replace('T', ' ').slice(0, 19) + ' UTC'

// One built-in column from its ADIF source field(s); missing fields become ''.
function builtinValues(r) {
  return {
    frequency: r.FREQ ?? '',
    rst_sent: r.RST_SENT ?? '',
    rst_received: r.RST_RCVD ?? '',
    gridsquare: r.GRIDSQUARE ?? r.GRID ?? '',
    name: r.NAME ?? '',
    state: r.STATE ?? '',
    county: r.CNTY ?? '',
    country: r.COUNTRY ?? '',
  }
}

// A record's value for one template field, from the ADIF sources the importer
// understands: built-in columns via builtinValues, their_park via the P2P rule.
// Other custom fields have no ADIF source.
function fieldValue(record, field) {
  if (isBuiltin(field.name)) return builtinValues(record)[field.name] ?? ''
  if (field.name === 'their_park' && (record.SIG ?? '').trim().toUpperCase() === 'POTA') {
    return record.SIG_INFO ?? ''
  }
  return ''
}

export default function ImportTab({ config, session, clientUuid, onDone }) {
  const [file, setFile] = useState(null) // { name, usable: [{record, ms}], bad }
  const [modeMap, setModeMap] = useState({})
  const [bandMap, setBandMap] = useState({})
  const [callsign, setCallsign] = useState(session.callsign)
  const [initials, setInitials] = useState(session.initials)
  const [offset, setOffset] = useState({ days: 0, hours: 0, minutes: 0 })
  const [existingKeys, setExistingKeys] = useState(new Set())
  const [summary, setSummary] = useState(null) // { imported, dupes, bad }
  const [error, setError] = useState('')

  const modes = useMemo(() => breakdown(file?.usable.map((u) => u.record) ?? [], 'MODE'), [file])
  const bands = useMemo(() => breakdown(file?.usable.map((u) => u.record) ?? [], 'BAND'), [file])

  // The event's required fields decide syncability: the server rejects a
  // contact with a blank required field (api_rest post_contact), so a row that
  // can't satisfy one — from the ADIF or the field's template default — would
  // import locally but sit pending forever. Exclude those up front, visibly.
  const requiredFields = useMemo(
    () => resolveAllFields(config).filter((f) => f.required), [config],
  )
  const canSync = (record) => requiredFields.every(
    (f) => fieldValue(record, f).trim() || (f.default ?? '').trim(),
  )
  const unsyncable = useMemo(() => {
    if (!file) return { count: 0, labels: [] }
    const labels = new Set()
    let count = 0
    for (const u of file.usable) {
      const missing = requiredFields.filter(
        (f) => !fieldValue(u.record, f).trim() && !(f.default ?? '').trim(),
      )
      if (missing.length) {
        count++
        missing.forEach((f) => labels.add(f.label ?? f.name))
      }
    }
    return { count, labels: [...labels] }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file, requiredFields])

  // Each box is independently signed (spinners go negative); the shift is
  // their sum, so -1 days + 30 minutes = 23.5 hours back.
  const offsetMs =
    ((Number(offset.days) || 0) * 24 * 60 + (Number(offset.hours) || 0) * 60 +
      (Number(offset.minutes) || 0)) * 60_000

  // Resolve one usable row through the current settings; iso is offset-adjusted.
  const resolve = (u) => {
    const r = u.record
    return {
      record: r,
      iso: new Date(u.ms + offsetMs).toISOString(),
      call: (r.CALL ?? '').trim().toUpperCase(),
      band: bandMap[(r.BAND ?? '').trim() || BLANK] ?? '',
      mode: modeMap[(r.MODE ?? '').trim() || BLANK] ?? '',
    }
  }

  // Advisory dupe count under the current mapping/offset — the same rows the
  // import pass will skip (existing log + earlier rows in this file).
  const dupeCount = useMemo(() => {
    if (!file) return 0
    const seen = new Set(existingKeys)
    let dupes = 0
    for (const u of file.usable) {
      if (!canSync(u.record)) continue
      const { call, band, mode, iso } = resolve(u)
      const key = dupeKey(call, band, mode, iso)
      if (seen.has(key)) dupes++
      else seen.add(key)
    }
    return dupes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file, existingKeys, modeMap, bandMap, offsetMs])

  async function loadFile(f) {
    const text = await f.text()
    const records = parseAdif(text)
    const usable = []
    let bad = 0
    for (const record of records) {
      const ms = recordTimestamp(record)
      if (!(record.CALL ?? '').trim() || ms === null) bad++
      else usable.push({ record, ms })
    }
    if (!usable.length) {
      setError(records.length
        ? `No importable contacts in ${f.name} (${bad} unparseable records).`
        : `${f.name} doesn't look like an ADIF file — no records found.`)
      return
    }
    const recs = usable.map((u) => u.record)
    setModeMap(initialMapping(breakdown(recs, 'MODE'), config.modes))
    setBandMap(initialMapping(breakdown(recs, 'BAND'), config.bands))
    // Snapshot the existing log once for the dupe check (deleted rows don't count).
    const contacts = await db.contacts.toArray()
    setExistingKeys(new Set(
      contacts
        .filter((c) => !c.deleted)
        .map((c) => dupeKey(c.remote_callsign, c.band, c.mode, c.qso_at)),
    ))
    setError('')
    setFile({ name: f.name, usable, bad })
  }

  async function runImport() {
    const hasTheirPark = (config.fields ?? []).some((f) => f.name === 'their_park')
    const clockOffset = (await kvGet('clock_offset')) ?? 0
    const now = new Date(Date.now() + clockOffset).toISOString()
    const seen = new Set(existingKeys)
    const rows = []
    let requiredSkipped = 0
    for (const u of file.usable) {
      if (!canSync(u.record)) {
        requiredSkipped++
        continue
      }
      const { record, iso, call, band, mode } = resolve(u)
      const key = dupeKey(call, band, mode, iso)
      if (seen.has(key)) continue
      seen.add(key)
      const builtins = builtinValues(record)
      const fields = {}
      if (hasTheirPark && (record.SIG ?? '').trim().toUpperCase() === 'POTA') {
        fields.their_park = record.SIG_INFO ?? ''
      }
      // Blank required fields fall back to the template default (canSync
      // guaranteed one exists) — the server refuses the row otherwise.
      for (const f of requiredFields) {
        if (fieldValue(record, f).trim()) continue
        if (isBuiltin(f.name)) builtins[f.name] = f.default
        else fields[f.name] = f.default
      }
      rows.push({
        uuid: newUuid(),
        qso_at: iso,
        created_at: now,
        last_edited: now,
        remote_callsign: call,
        operator_callsign: callsign.trim().toUpperCase(),
        operator_initials: initials.trim().toUpperCase(),
        client_uuid: clientUuid,
        band,
        mode,
        deleted: false,
        ...builtins,
        fields,
        sync_state: 'pending',
      })
    }
    await db.contacts.bulkPut(rows)
    pushNow()
    setSummary({
      imported: rows.length,
      dupes: file.usable.length - rows.length - requiredSkipped,
      required: requiredSkipped,
      bad: file.bad,
    })
  }

  if (summary) {
    return (
      <div className="tab-page import-page">
        <h2>ADIF import complete</h2>
        <p>
          Imported <strong>{summary.imported}</strong> contact{summary.imported === 1 ? '' : 's'}
          {summary.dupes > 0 && <> · skipped {summary.dupes} duplicate{summary.dupes === 1 ? '' : 's'}</>}
          {summary.required > 0 && <> · {summary.required} missing required fields</>}
          {summary.bad > 0 && <> · {summary.bad} unparseable record{summary.bad === 1 ? '' : 's'}</>}
        </p>
        <div className="import-actions">
          <button type="button" className="btn-primary" onClick={onDone}>Done</button>
        </div>
      </div>
    )
  }

  if (!file) {
    return (
      <div className="tab-page import-page">
        <h2>Import ADIF as contacts</h2>
        <p>
          Pick an ADIF file (<code>.adi</code> / <code>.adif</code>) exported from another
          logger. Nothing is imported until you review and confirm.
        </p>
        <input
          type="file"
          accept=".adi,.adif"
          onChange={(e) => e.target.files[0] && loadFile(e.target.files[0])}
        />
        {error && <p className="import-error">{error}</p>}
        <div className="import-actions">
          <button type="button" onClick={onDone}>Cancel</button>
        </div>
      </div>
    )
  }

  const mappingComplete =
    modes.every(([v]) => modeMap[v]) && bands.every(([v]) => bandMap[v])
  const ready = mappingComplete && callsign.trim() && initials.trim()
  const importable = file.usable.length - dupeCount - unsyncable.count

  const mappingTable = (title, note, pairs, mapping, setMapping, options) => (
    <div className="import-section">
      <h2>
        {title} <span className="import-note">({note})</span>
      </h2>
      <table className="import-table">
        <tbody>
          {pairs.map(([value, count]) => (
            <tr key={value}>
              <td className="import-value">{value}</td>
              <td className="import-count">{count}</td>
              <td>
                <select
                  value={mapping[value] ?? ''}
                  onChange={(e) => setMapping({ ...mapping, [value]: e.target.value })}
                >
                  <option value="" disabled>— choose —</option>
                  {options.map((o) => (
                    <option key={o} value={o}>{o}</option>
                  ))}
                </select>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )

  return (
    <div className="tab-page import-page">
      <h2>Import ADIF as contacts</h2>
      <p>
        <strong>{file.name}</strong>: {file.usable.length} contact
        {file.usable.length === 1 ? '' : 's'}
        {file.bad > 0 && <> ({file.bad} unparseable record{file.bad === 1 ? '' : 's'} skipped)</>}
      </p>

      {mappingTable(
        'Modes',
        'each mode found in the file must be logged as one of this event’s modes',
        modes, modeMap, setModeMap, config.modes,
      )}
      {mappingTable(
        'Bands',
        'each band found in the file must be logged as one of this event’s bands',
        bands, bandMap, setBandMap, config.bands,
      )}

      <div className="import-section">
        <h2>Log as</h2>
        <div className="import-row">
          <label>
            Operator:
            <input
              value={callsign}
              onChange={(e) => setCallsign(sanitizeText(e.target.value).toUpperCase())}
              maxLength={10}
            />
          </label>
          <label>
            Initials:
            <input
              value={initials}
              onChange={(e) => setInitials(sanitizeText(e.target.value).toUpperCase())}
              maxLength={4}
            />
          </label>
        </div>
      </div>

      <div className="import-section">
        <h2>Time offset</h2>
        <p className="import-hint">
          If the clock on the original logging computer was wrong, correct every
          contact's time here. Positive values shift later, negative earlier.
        </p>
        <div className="import-row">
          {['days', 'hours', 'minutes'].map((unit) => (
            <label key={unit}>
              {unit[0].toUpperCase() + unit.slice(1)}:
              {/* uncontrolled on purpose: while "-" is being typed the input
                  reports badInput with value "", and a controlled write-back
                  of that would wipe the pending minus sign mid-edit */}
              <input
                type="number"
                defaultValue={offset[unit]}
                onChange={(e) => setOffset({ ...offset, [unit]: e.target.value })}
                // Chromium won't replace selected text with a minus sign — the
                // edit buffer becomes e.g. "0-" (badInput) and the box wedges.
                // Clearing first makes "-" always start a fresh negative entry;
                // the subsequent input events rebuild the state.
                onKeyDown={(e) => {
                  if (e.key === '-') e.target.value = ''
                }}
              />
            </label>
          ))}
        </div>
        <table className="import-table import-preview">
          <thead>
            <tr><th>Callsign</th><th>Logged time</th><th>Will import as</th></tr>
          </thead>
          <tbody>
            {file.usable.slice(0, 5).map((u, i) => (
              <tr key={i}>
                <td className="import-value">{(u.record.CALL ?? '').toUpperCase()}</td>
                <td>{fmtUtc(u.ms)}</td>
                <td>{fmtUtc(u.ms + offsetMs)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {unsyncable.count > 0 && (
        <p className="import-hint">
          {unsyncable.count} contact{unsyncable.count === 1 ? ' is' : 's are'} missing{' '}
          required field{unsyncable.labels.length === 1 ? '' : 's'} the event demands
          ({unsyncable.labels.join(', ')}) with no default to fall back on, and will
          be skipped.
        </p>
      )}
      {dupeCount > 0 && (
        <p className="import-hint">
          {dupeCount} contact{dupeCount === 1 ? ' looks' : 's look'} like{' '}
          duplicate{dupeCount === 1 ? '' : 's'} of already-logged contacts (same
          callsign, band, mode, and minute) and will be skipped.
        </p>
      )}
      {!ready && (
        <p className="import-hint">
          {mappingComplete
            ? 'Enter the operator callsign and initials to import.'
            : 'Map every mode and band to one of the event’s values to import.'}
        </p>
      )}

      <div className="import-actions">
        <button type="button" onClick={onDone}>Cancel</button>
        <button
          type="button"
          className="btn-primary"
          disabled={!ready || importable === 0}
          onClick={runImport}
        >
          Import {importable} contact{importable === 1 ? '' : 's'}
        </button>
      </div>
    </div>
  )
}
