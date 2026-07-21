// Log export — a section of the Settings tab, above the ADIF importer.
// Everything happens client-side from the local Dexie copy of the log
// (ADR-0001): this client exports what it has, including contacts still
// pending push, so the modal reports both counts and lets the operator judge.
import { useMemo, useState } from 'react'
import { db, download, exportRawEvent } from '../../db.js'
import {
  ADIF_BANDS, ADIF_MODES, BAND_ALIASES, MODE_ALIASES,
  breakdown, buildAdif, initialMapping,
} from '../../adif-export.js'

export default function ExportSection({ event }) {
  // null when the modal is closed; otherwise the loaded snapshot of the log.
  const [snapshot, setSnapshot] = useState(null) // { contacts, pending }
  const [bandMap, setBandMap] = useState({})
  const [modeMap, setModeMap] = useState({})
  const [error, setError] = useState('')

  const bands = useMemo(() => breakdown(snapshot?.contacts ?? [], 'band'), [snapshot])
  const modes = useMemo(() => breakdown(snapshot?.contacts ?? [], 'mode'), [snapshot])

  async function openModal() {
    const all = await db.contacts.toArray()
    // Deleted rows are dropped silently — a log export means the log as it
    // stands. Sort by parsed time: qso_at arrives in both the client's 'Z'
    // form and the server's '+00:00' form, which don't sort as strings.
    const contacts = all
      .filter((c) => !c.deleted)
      .sort((a, b) => Date.parse(a.qso_at) - Date.parse(b.qso_at))
    if (!contacts.length) {
      setError('This event has no contacts to export yet.')
      return
    }
    setError('')
    setBandMap(initialMapping(breakdown(contacts, 'band'), BAND_ALIASES))
    setModeMap(initialMapping(breakdown(contacts, 'mode'), MODE_ALIASES))
    setSnapshot({
      contacts,
      pending: contacts.filter((c) => c.sync_state === 'pending').length,
    })
  }

  function runExport() {
    const text = buildAdif(snapshot.contacts, { event, bandMap, modeMap })
    // HAML_<event name>_<date>.adi — spaces and punctuation in the event name
    // collapse to dashes, so the underscores stay meaningful as the separators
    // between the three parts of the filename.
    const name = (event.name || 'event').replace(/[^\w-]+/g, '-')
    download(`HAML_${name}_${new Date().toISOString().slice(0, 10)}.adi`, text)
    setSnapshot(null)
  }

  const mappingComplete =
    bands.every(([v]) => bandMap[v]) && modes.every(([v]) => modeMap[v])

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
    <section className="settings-section export-section">
      <h2>Export log</h2>

      <div className="import-choose-row">
        <button type="button" className="btn-primary import-choose" onClick={openModal}>
          Export Full ADIF
        </button>
        <span className="import-hint">
          Download every contact in this event as a standard <code>.adi</code> file,
          with all recorded fields. For archiving, or for loading the log into
          another program.
        </span>
      </div>
      {error && <p className="import-error">{error}</p>}

      <div className="import-choose-row export-second-row">
        <button type="button" className="btn-primary import-choose" disabled>
          Export Event for Submission
        </button>
        <span className="import-hint">
          Will produce a contest-ready file formatted to this event’s template
          export mapping. <em>Not implemented yet.</em>
        </span>
      </div>

      <div className="import-choose-row export-second-row">
        <button type="button" className="btn-primary import-choose" onClick={exportRawEvent}>
          Export Raw Data
        </button>
        <span className="import-hint">
          Download everything this browser holds for the event — contacts
          (including deleted ones and any not yet synced), chat, and the event
          config — as a <code>.json</code> snapshot. For archiving or
          troubleshooting, not for loading into another logger.
        </span>
      </div>

      {snapshot && (
        <div
          className="modal-backdrop"
          onMouseDown={(e) => e.target === e.currentTarget && setSnapshot(null)}
        >
          <div className="modal export-modal">
            <div className="modal-header">
              <span>Export Full ADIF</span>
              <button
                type="button"
                className="modal-close"
                onClick={() => setSnapshot(null)}
                title="Close"
              >
                ✕
              </button>
            </div>

            <p>
              <strong>{snapshot.contacts.length}</strong> contact
              {snapshot.contacts.length === 1 ? '' : 's'}
              {snapshot.pending > 0 && (
                <> ({snapshot.pending} not yet synced to the server)</>
              )}
            </p>

            {mappingTable(
              'Modes',
              'each mode in the log must be exported as an ADIF mode',
              modes, modeMap, setModeMap, ADIF_MODES,
            )}
            {mappingTable(
              'Bands',
              'each band in the log must be exported as an ADIF band',
              bands, bandMap, setBandMap, ADIF_BANDS,
            )}

            {!mappingComplete && (
              <p className="import-hint">
                Map every mode and band to an ADIF value to export.
              </p>
            )}

            <div className="modal-actions">
              <button type="button" onClick={() => setSnapshot(null)}>Cancel</button>
              <span className="spacer" />
              <button
                type="button"
                className="btn-primary"
                disabled={!mappingComplete}
                onClick={runExport}
              >
                Export {snapshot.contacts.length} contact
                {snapshot.contacts.length === 1 ? '' : 's'}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
