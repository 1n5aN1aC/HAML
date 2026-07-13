// Slim bar always shown at the top: brand, tab navigation, theme picker, the
// active event, and whether the realtime (WebSocket) signal layer is reachable.
const TABS = [
  { id: 'logging', label: 'Logging' },
  { id: 'radio', label: 'Radio' },
  { id: 'stats', label: 'Statistics' },
  { id: 'admin', label: 'Admin' },
]

const THEMES = [
  { id: 'light', emoji: '☀️', label: 'Light' },
  { id: 'dark', emoji: '🌙', label: 'Dark' },
  { id: 'blue', emoji: '🔵', label: 'Solarized' },
  { id: 'sepia', emoji: '📄', label: 'Sepia' },
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
      <div className="theme-picker" role="group" aria-label="Theme">
        {THEMES.map(({ id, emoji, label }) => (
          <button
            key={id}
            className={id === theme ? 'active' : ''}
            title={`${label} theme`}
            aria-label={`${label} theme`}
            aria-pressed={id === theme}
            onClick={() => onTheme(id)}
          >
            {emoji}
          </button>
        ))}
      </div>
      <span
        className={connected ? 'conn conn-ok' : 'conn conn-down'}
        title={connected ? 'Connected to server' : 'Not connected — logging locally'}
      >
        ● {connected ? 'Connected' : 'Offline'}
      </span>
    </header>
  )
}
