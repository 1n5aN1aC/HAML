// Full contact statistics for the Statistics page. Forked from
// logging/StatsPanel (the logging tab's compact version) so the two can evolve
// independently — this one is the place for the richer, expanded displays.
// Both render the same .stats-panel CSS.
// Live view over Dexie, same pattern as ContactList; a 60s tick keeps the
// time-window numbers honest even when no contacts are being logged.

import { useEffect, useState } from 'react'
import { useLiveQuery } from 'dexie-react-hooks'
import { db } from '../../db.js'

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

  // key extractor → [[key, count], …] sorted by count descending
  const tally = (get) => {
    const counts = {}
    contacts.forEach((c) => {
      const k = get(c) || 'Unknown'
      counts[k] = (counts[k] || 0) + 1
    })
    return Object.entries(counts).sort((a, b) => b[1] - a[1])
  }

  const asLine = (pairs) =>
    pairs.length === 0
      ? 'No contacts yet'
      : pairs.map(([k, n]) => `${k} (${n})`).join(', ')

  const byMode = tally((c) => c.mode)

  return (
    <div className="stats-panel">
      <h3>Contact Stats</h3>
      <p><strong>Total Contacts:</strong> {contacts.length}</p>
      <p><strong>Contacts last hour:</strong> {inLastMinutes(60)}</p>
      <p><strong>Contact rate (last 15 minutes):</strong> {inLastMinutes(15) * 4}/h</p>
      <p><strong>Contacts by Mode:</strong></p>
      {byMode.length === 0 ? (
        <p className="indent">No contacts yet</p>
      ) : (
        byMode.map(([mode, n]) => (
          <p className="indent" key={mode}>
            <strong>{mode}:</strong> {n}
          </p>
        ))
      )}
      <p><strong>Contacts by Band:</strong> {asLine(tally((c) => c.band))}</p>
      <p><strong>Contacts by Operator:</strong> {asLine(tally((c) => c.operator_callsign))}</p>
    </div>
  )
}
