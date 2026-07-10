// Slim bar above StatusBar showing the active event and whether the
// realtime (WebSocket) signal layer is reachable. The StatusBar handles
// operator identity; this is the "where am I" line.
export default function TopBar({ eventName, connected }) {
  return (
    <header className="top-bar">
      <div className="brand">
        <img className="brand-icon" src="/favicon.svg" alt="" />
        <span className="brand-text">HAML</span>
      </div>
      <span className="spacer" />
      <span className="event-name">{eventName}</span>
      <span
        className={connected ? 'conn conn-ok' : 'conn conn-down'}
        title={connected ? 'Connected to server' : 'Not connected — logging locally'}
      >
        ● {connected ? 'Connected' : 'Offline'}
      </span>
    </header>
  )
}
