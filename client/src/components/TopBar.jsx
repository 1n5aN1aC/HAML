// Slim bar always shown at the top: brand, tab navigation, theme picker, the
// active event, and whether the realtime (WebSocket) signal layer is reachable.
import { THEMES } from '../themes.js'

const TABS = [
  { id: 'logging', label: 'Logging' },
  { id: 'radio', label: 'Radio' },
  { id: 'stats', label: 'Statistics' },
  { id: 'admin', label: 'Admin' },
]

export default function TopBar({ eventName, connected, activeTab, onTab, theme, onTheme }) {
  return (
    <header className="top-bar">
      <div className="brand">
        <img className="brand-icon" src="/favicon.svg" alt="" />
        <span className="brand-text">HAML</span>
      </div>
      <nav className="tabs">
        {TABS.map(({ id, label }) => (
          <button
            key={id}
            className={id === activeTab ? 'tab active' : 'tab'}
            onClick={() => onTab(id)}
          >
            {label}
          </button>
        ))}
      </nav>
      <span className="spacer" />
      <span className="event-name">{eventName}</span>
      <select
        className="theme-picker"
        value={theme}
        title="Theme"
        aria-label="Theme"
        onChange={(e) => onTheme(e.target.value)}
      >
        {THEMES.map(({ id, label }) => (
          <option key={id} value={id}>
            {label}
          </option>
        ))}
      </select>
      <span
        className={connected ? 'conn conn-ok' : 'conn conn-down'}
        title={connected ? 'Connected to server' : 'Not connected — logging locally'}
      >
        ● {connected ? 'Connected' : 'Offline'}
      </span>
    </header>
  )
}
