// Online stations roster (plan §3.5).
// The server sends `last_seen_at` as a wall-clock epoch second;
// We tick once per second locally and compute the age here, applying the persisted server clock offset (kv: clock_offset)
// so this client agrees with the server even if its wall clock is skewed.
import { useEffect, useState } from 'react'
import { kvGet } from '../db.js'

function ageBand(age) {
  if (age < 10) return 'fresh'
  if (age <= 60) return 'stale'
  return 'old'
}

export default function StationsPanel({ stations, clientUuid }) {
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

  return (
    <div className="stations-panel">
      <h2>Stations</h2>
      {stations.length === 0 ? (
        <p className="placeholder">No stations online</p>
      ) : (
        <table className="stations">
          <tbody>
            {stations.map((s) => {
              const age = Math.max(0, Math.floor(serverNow - s.last_seen_at))
              return (
                <tr key={s.client_uuid}>
                  <td className="cs">
                    {s.callsign}
                    <span className="initials"> {s.initials}</span>
                    {s.client_uuid === clientUuid && <span className="you"> (you)</span>}
                  </td>
                  <td>{s.band}</td>
                  <td>{s.mode}</td>
                  <td className={`last-seen ${ageBand(age)}`}>
                    {age < 5 ? 'Now' : `${age}s`}
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
