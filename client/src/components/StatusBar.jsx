// Top status bar: operator identity, band/mode, connection indicator.
// Logging stays disabled until every session value is set (App enforces it).
import { useEffect } from 'react'

const DEFAULT_MODE = (modes) =>
  modes.includes('Phone') ? 'Phone' : (modes[0] || '')

export default function StatusBar({ session, onSession, config, conflicts = [] }) {
  const set = (key) => (e) => onSession({ ...session, [key]: e.target.value })
  const mode = session.mode || DEFAULT_MODE(config.modes)
  const setMode = (e) => onSession({ ...session, mode: e.target.value })

  // Seed the default mode into session so App's gate opens immediately.
  useEffect(() => {
    if (!session.mode && mode) onSession({ ...session, mode })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <header className="status-bar">
      <span className="operator-info-label">Operator:</span>
      <input
        className="callsign"
        placeholder="Callsign"
        value={session.callsign}
        maxLength={10}
        onChange={set('callsign')}
      />
      <input
        className="initials"
        placeholder="Initials"
        value={session.initials}
        maxLength={4}
        onChange={set('initials')}
      />
      <label className="mode-label">
        Mode:&nbsp;
        <select value={mode} onChange={setMode}>
          {config.modes.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
      </label>
      <label className="band-label">
        Band:&nbsp;
        <select
          className={conflicts.length ? 'band-conflict' : ''}
          value={session.band}
          onChange={set('band')}
        >
          {['Off-Air', ...config.bands].map((b) => (
            <option key={b} value={b}>{b}</option>
          ))}
        </select>
      </label>
      {conflicts.length > 0 && (
        <span className="band-conflict-warning">
          ⚠ {session.band} in use by {conflicts.map((s) => s.callsign).join(', ')}
        </span>
      )}
      <span className="spacer" />
    </header>
  )
}
