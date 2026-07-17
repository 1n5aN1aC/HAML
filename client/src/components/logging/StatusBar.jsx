// Top status bar: operator identity, band/mode, connection indicator.
// Logging stays disabled until every session value is set (App enforces it).
import { useEffect } from 'react'
import { sanitizeText } from '../../text-input.js'

const DEFAULT_MODE = (modes) =>
  modes.includes('Phone') ? 'Phone' : (modes[0] || '')

export default function StatusBar({
  session, onSession, config, exchange, conflicts = [], bandsInUse = new Set(),
}) {
  const set = (key) => (e) => onSession({ ...session, [key]: e.target.value })
  const setAlphanumeric = (key) => (e) =>
    onSession({ ...session, [key]: sanitizeText(e.target.value) })
  // The saved session survives event switches (db.js wipeEventData), so its band/mode may not exist in this Event's config
  // A select whose value matches no option silently *displays* the first option while logging the stale value.
  // Treat anything not in the config as unset.
  const mode = config.modes.includes(session.mode)
    ? session.mode
    : DEFAULT_MODE(config.modes)
  const band = ['Off-Air', ...config.bands].includes(session.band)
    ? session.band
    : 'Off-Air'
  const setMode = (e) => onSession({ ...session, mode: e.target.value })

  // Seed the corrected values back into session
  useEffect(() => {
    if (session.mode !== mode || session.band !== band)
      onSession({ ...session, mode, band })
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
        onChange={setAlphanumeric('callsign')}
      />
      <input
        className="initials"
        placeholder="Initials"
        value={session.initials}
        maxLength={4}
        onChange={setAlphanumeric('initials')}
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
          value={band}
          onChange={set('band')}
        >
          {['Off-Air', ...config.bands].map((b) => (
            <option
              key={b}
              value={b}
              className={bandsInUse.has(b) ? 'band-in-use' : ''}
            >
              {b}
            </option>
          ))}
        </select>
      </label>
      {conflicts.length > 0 && (
        <span className="band-conflict-warning">
          ⚠ In use by {conflicts.map((s) => s.callsign).join(', ')}
        </span>
      )}
      <span className="spacer" />
      <span className="exchange-label">Exchange:</span>
      <span className="event-exchange">{exchange}</span>
    </header>
  )
}
