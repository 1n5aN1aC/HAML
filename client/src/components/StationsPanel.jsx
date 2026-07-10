// Online stations roster (plan §3.5): fed by presence_list broadcasts, which
// arrive at least every 5s — last_seen comes from the server, no local ticking.
export default function StationsPanel({ stations, clientUuid }) {
  return (
    <div className="stations-panel">
      <h2>Stations</h2>
      {stations.length === 0 ? (
        <p className="placeholder">No stations online</p>
      ) : (
        <table className="stations">
          <tbody>
            {stations.map((s) => (
              <tr key={s.client_uuid}>
                <td className="cs">
                  {s.callsign}
                  <span className="initials"> {s.initials}</span>
                  {s.client_uuid === clientUuid && <span className="you"> (you)</span>}
                </td>
                <td>{s.band}</td>
                <td>{s.mode}</td>
                <td className={`last-seen ${s.last_seen < 10 ? 'fresh' : s.last_seen <= 60 ? 'stale' : 'old'}`}>
                  {s.last_seen < 5 ? 'Now' : `${s.last_seen}s`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
