// Top status bar: operator identity, band/mode, connection indicator.
// Logging stays disabled until every session value is set (App enforces it).
export default function StatusBar({ eventName, session, onSession, config, connected }) {
  const set = (key) => (e) => onSession({ ...session, [key]: e.target.value })

  return (
    <header className="status-bar">
      <span className="event-name">{eventName || 'HAML'}</span>
      <input
        className="callsign"
        placeholder="Callsign"
        value={session.callsign}
        onChange={set('callsign')}
      />
      <input
        className="initials"
        placeholder="Initials"
        value={session.initials}
        onChange={set('initials')}
      />
      <select value={session.band} onChange={set('band')}>
        <option value="">Band…</option>
        {config.bands.map((b) => (
          <option key={b} value={b}>{b}</option>
        ))}
      </select>
      <select value={session.mode} onChange={set('mode')}>
        <option value="">Mode…</option>
        {config.modes.map((m) => (
          <option key={m} value={m}>{m}</option>
        ))}
      </select>
      <span className="spacer" />
      <span
        className={connected ? 'conn conn-ok' : 'conn conn-down'}
        title={connected ? 'Connected to server' : 'Not connected — logging locally'}
      >
        ● {connected ? 'Connected' : 'Offline'}
      </span>
    </header>
  )
}
