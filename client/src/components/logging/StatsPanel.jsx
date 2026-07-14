// Compact contact stats for the logging tab (the "stats" side of the
// map/stats panel). The Statistics page has its own fork, statistics/StatisticsPanel,
// so this one can stay lean. Both render the same .stats-panel CSS.
// Live view over Dexie, same pattern as ContactList; a 60s tick keeps the
// time-window numbers honest even when no contacts are being logged.

import { useEffect, useState } from 'react'
import { useLiveQuery } from 'dexie-react-hooks'
import { db } from '../../db.js'

export default function StatsPanel() {
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
      {byMode.length === 0 ? (
        <p className="indent">No contacts yet</p>
      ) : (
        byMode.map(([mode, n]) => (
          <p className="indent" key={mode}>
            <strong>{mode}:</strong> {n}
          </p>
        ))
      )}
    </div>
  )
}