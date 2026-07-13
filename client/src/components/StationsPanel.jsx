// Online stations roster (plan §3.5).
// The server sends `last_seen_at` for each station as a wall-clock epoch second;
// We tick once per second locally and compute the age here, applying the persisted server clock offset (kv: clock_offset)
// so this client agrees with the server even if its wall clock is skewed.

import { useEffect, useState } from 'react'
import { kvGet } from '../db.js'

function ageBand(age) {
  if (age < 15) return 'fresh'
  if (age <= 60) return 'stale'
  return 'old'
}

export default function StationsPanel({ stations, clientUuid, conflictUuids, bands = [] }) {
  // `offset` is server_now - Date.now() (set by sync.js after each pull).
  const [offset, setOffset] = useState(0)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let cancelled = false
    kvGet('clock_offset').then((v) => { if (!cancelled) setOffset(v ?? 0) })
    const id = setInterval(() => setTick((t) => t + 1), 1000)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  const serverNow = (Date.now() + offset) / 1000

  // Sort stations to same order as in config:  Lowest-first.
  // Off-Air (or anything unrecognized) is sorted last.
  const bandRank = (b) => {
    const i = bands.indexOf(b)
    return i === -1 ? bands.length : i
  }
  const sorted = [...stations].sort((a, b) => bandRank(a.band) - bandRank(b.band))

  return (
    <div className="stations-panel">
      {stations.length === 0 ? (
        <p className="placeholder">No stations online</p>
      ) : (
        <table className="stations">
          <tbody>
            {sorted.map((s) => {
              const age = Math.max(0, Math.floor(serverNow - s.last_seen_at))
              return (
                <tr
                  key={s.client_uuid}
                  className={conflictUuids?.has(s.client_uuid) ? 'conflict' : ''}
                >
                  <td className="cs">
                    {s.callsign}
                    <span className="initials"> {s.initials}</span>
                    {s.client_uuid === clientUuid && <span className="you"> (you)</span>}
                  </td>
                  <td>{s.band === 'Off-Air' ? s.band : `${s.band} • ${s.mode}`}</td>
                  <td className={`last-seen ${ageBand(age)}`}>
                    {age < 6 ? 'Now' : `${age}s`}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}
