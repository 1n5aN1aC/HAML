// Full contact statistics for the Statistics page. Forked from
// logging/StatsPanel (the logging tab's compact version) so the two can evolve
// independently — this one is the place for the richer, expanded displays.
// Both render the same .stats-panel CSS.
// Live view over Dexie, same pattern as ContactList; a 60s tick keeps the
// time-window numbers honest even when no contacts are being logged.

import { useEffect, useState } from 'react'
import { useLiveQuery } from 'dexie-react-hooks'
import { db } from '../../db.js'
import { SECTION_NAMES } from '../../sections.js'

export default function StatisticsPanel() {
  const contacts =
    useLiveQuery(() => db.contacts.filter((c) => !c.deleted).toArray(), []) ?? []

  const [, setTick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 60_000)
    return () => clearInterval(id)
  }, [])

  const inLastMinutes = (minutes) => {
    const cutoff = Date.now() - minutes * 60_000
    return contacts.filter((c) => new Date(c.qso_at).getTime() >= cutoff).length
  }

  // Sorted contact times (bad timestamps filtered out, same guard as
  // RateGraph) feed the average rate and longest-gap stats.
  const times = contacts
    .map((c) => new Date(c.qso_at).getTime())
    .filter(Number.isFinite)
    .sort((a, b) => a - b)

  // Average pace: contacts per hour from the first contact until now — the
  // 60s tick keeps it decaying honestly during dry spells.
  const elapsedH = times.length ? (Date.now() - times[0]) / 3_600_000 : 0
  const avgRate = elapsedH > 0 ? Math.round(times.length / elapsedH) : 0

  let longestGap = 0
  for (let i = 1; i < times.length; i++) {
    longestGap = Math.max(longestGap, times[i] - times[i - 1])
  }
  const formatGap = (ms) => {
    const mins = Math.round(ms / 60_000)
    return mins >= 60 ? `${Math.floor(mins / 60)}h ${mins % 60}m` : `${mins}m`
  }

  const uniqueCalls = new Set(contacts.map((c) => c.remote_callsign)).size

  // key extractor → [[key, count], …] sorted by count descending
  const tally = (get, list = contacts) => {
    const counts = {}
    list.forEach((c) => {
      const k = get(c) || 'Unknown'
      counts[k] = (counts[k] || 0) + 1
    })
    return Object.entries(counts).sort((a, b) => b[1] - a[1])
  }

  // per-operator rows, sorted by total (tally order): best sliding-hour rate
  // plus favorite band/mode with the share of that operator's contacts.
  const operators = tally((c) => c.operator_callsign).map(([op, total]) => {
    const theirs = contacts.filter(
      (c) => (c.operator_callsign || 'Unknown') === op,
    )
    // most contacts inside any 60-minute window (two pointers over sorted times)
    const times = theirs
      .map((c) => new Date(c.qso_at).getTime())
      .sort((a, b) => a - b)
    let maxRate = 0
    for (let i = 0, j = 0; j < times.length; j++) {
      while (times[j] - times[i] > 3_600_000) i++
      maxRate = Math.max(maxRate, j - i + 1)
    }
    const favorite = (get) => {
      const [k, n] = tally(get, theirs)[0]
      return `${k} (${Math.round((n / total) * 100)}%)`
    }
    return {
      op,
      total,
      maxRate,
      favBand: favorite((c) => c.band),
      favMode: favorite((c) => c.mode),
    }
  })

  // band × mode matrix: rows are bands (busiest first, from tally), columns
  // are modes, cells are counts, with a totals row and column.
  const bands = tally((c) => c.band)
  const modes = tally((c) => c.mode)
  const cellCounts = {}
  contacts.forEach((c) => {
    const key = `${c.band || 'Unknown'}\0${c.mode || 'Unknown'}`
    cellCounts[key] = (cellCounts[key] || 0) + 1
  })
  const cell = (band, mode) => cellCounts[`${band}\0${mode}`] || 0

  // Top sections, busiest first. Templates that track sections name the field
  // "section" (same convention as MapPanel); contacts without one land in
  // tally's Unknown bucket and are dropped, so events whose template has no
  // section field render no section stat at all.
  const topSections = tally((c) => c.section?.toUpperCase())
    .filter(([k]) => k !== 'Unknown')
    .slice(0, 10)

  return (
    <div className="stats-panel">
      <h3>Contact Stats</h3>
      <p><strong>Total Contacts:</strong> {contacts.length}</p>
      <p><strong>Unique Callsigns:</strong> {uniqueCalls}</p>
      <p><strong>QSO/h (Last 60 mins):</strong> {inLastMinutes(60)}</p>
      <p><strong>QSO/h (Last 15 mins):</strong> {inLastMinutes(15) * 4}</p>
      <p><strong>QSO/h (Average):</strong> {avgRate}</p>
      <p>
        <strong>Longest Gap:</strong>{' '}
        {times.length >= 2 ? formatGap(longestGap) : '—'}
      </p>
      <p><strong>Contacts by Band / Mode:</strong></p>
      {contacts.length === 0 ? (
        <p className="indent">No contacts yet</p>
      ) : (
        <table className="stats-matrix">
          <thead>
            <tr>
              <th></th>
              {modes.map(([mode]) => <th key={mode}>{mode}</th>)}
              <th>Total</th>
            </tr>
          </thead>
          <tbody>
            {bands.map(([band, bandTotal]) => (
              <tr key={band}>
                <th>{band}</th>
                {modes.map(([mode]) => (
                  <td key={mode}>{cell(band, mode) || ''}</td>
                ))}
                <td className="matrix-total">{bandTotal}</td>
              </tr>
            ))}
            <tr className="matrix-total">
              <th>Total</th>
              {modes.map(([mode, modeTotal]) => (
                <td key={mode}>{modeTotal}</td>
              ))}
              <td>{contacts.length}</td>
            </tr>
          </tbody>
        </table>
      )}
      <p><strong>Contacts by Operator:</strong></p>
      {operators.length === 0 ? (
        <p className="indent">No contacts yet</p>
      ) : (
        <table className="stats-matrix">
          <thead>
            <tr>
              <th></th>
              <th>Total</th>
              <th>Max Rate</th>
              <th>Fav Band</th>
              <th>Fav Mode</th>
            </tr>
          </thead>
          <tbody>
            {operators.map((o) => (
              <tr key={o.op}>
                <th>{o.op}</th>
                <td>{o.total}</td>
                <td>{o.maxRate}/h</td>
                <td>{o.favBand}</td>
                <td>{o.favMode}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {topSections.length > 0 && (
        <>
          <p><strong>Top Sections:</strong></p>
          <table className="stats-matrix">
            <thead>
              <tr>
                <th></th>
                <th>Contacts</th>
                <th style={{ textAlign: 'left' }}>Name</th>
              </tr>
            </thead>
            <tbody>
              {topSections.map(([section, count]) => (
                <tr key={section}>
                  <th>{section}</th>
                  <td>{count}</td>
                  <td style={{ textAlign: 'left' }}>{SECTION_NAMES[section]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}
