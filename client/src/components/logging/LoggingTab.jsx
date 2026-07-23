// The Logging tab: operator identity (StatusBar), the two-pane layout,
// and the contact edit modal. All data/state comes from App; only the
// modal's open/close state lives here, so switching tabs closes it.

import { useState } from 'react'
import { useLiveQuery } from 'dexie-react-hooks'
import { kvGet } from '../../db.js'
import StatusBar from './StatusBar.jsx'
import ContactList from './ContactList.jsx'
import ContactEntryForm from './ContactEntryForm.jsx'
import StationsPanel from './StationsPanel.jsx'
import ChatPanel from './ChatPanel.jsx'
import MapStatsPanel from './MapStatsPanel.jsx'
import ContactModal from './ContactModal.jsx'

export default function LoggingTab({
  session,
  onSession,
  config,
  exchange,
  clientUuid,
  sessionComplete,
  stations,
  chat,
  onChatSend,
  onChatResend,
}) {
  const [editing, setEditing] = useState(null)

  // Band-conflict warning: another station (seen within the last 15s) is on
  // our band. No timer — the roster rebroadcasts on every heartbeat (≥ every
  // 5s, including our own), so `stations` re-renders us often enough. Ages
  // use the live server clock offset, same as StationsPanel.
  const offset = useLiveQuery(() => kvGet('clock_offset'), [], 0) ?? 0
  const serverNow = (Date.now() + offset) / 1000
  const conflicts =
    session.band === 'Off-Air'
      ? []
      : stations.filter(
          (s) =>
            s.client_uuid !== clientUuid &&
            s.band === session.band &&
            serverNow - s.last_seen_at <= 61,  //Exclude stale clients.  Had to increase to 61 because browsers throttle timer to 1 minute in background tabs.
        )

  // Bands other live stations are on, marked inside the band dropdown so the
  // operator sees occupied bands while choosing (same staleness cutoff).
  const bandsInUse = new Set(
    stations
      .filter(
        (s) =>
          s.client_uuid !== clientUuid &&
          s.band !== 'Off-Air' &&
          serverNow - s.last_seen_at <= 61,
      )
      .map((s) => s.band),
  )

  return (
    <>
      <StatusBar
        session={session}
        onSession={onSession}
        config={config}
        exchange={exchange}
        conflicts={conflicts}
        bandsInUse={bandsInUse}
      />
      <div className="status-bar-separator" />
      {/* Mobile-only sticky exchange. Rendered here (a sibling of the status
          bar and .panes, i.e. a direct child of the scrolling .app) rather than
          inside StatusBar, so position: sticky can pin it to the top and keep it
          there after the top bar and status bar scroll away. Hidden on desktop,
          where the exchange stays in the status bar. */}
      <div className="exchange-bar">
        <span className="exchange-label">Exchange:</span>
        <span className="event-exchange">{exchange}</span>
      </div>
      <main className="panes">
        <section className="left-pane">
          <ContactList config={config} onSelect={setEditing} />
          <ContactEntryForm
            config={config}
            session={session}
            clientUuid={clientUuid}
            disabled={!sessionComplete}
          />
          <div className="future-panel-left" />
        </section>
        <aside className="right-pane">
          <StationsPanel
            stations={stations}
            clientUuid={clientUuid}
            bands={config.bands}
            conflictUuids={new Set(conflicts.map((s) => s.client_uuid))}
          />
          <ChatPanel
            messages={chat}
            onSend={onChatSend}
            onResend={onChatResend}
            disabled={!session.callsign.trim() || !session.initials.trim()}
          />
          <MapStatsPanel />
        </aside>
      </main>
      {editing && (
        <ContactModal
          contact={editing}
          config={config}
          clientUuid={clientUuid}
          onClose={() => setEditing(null)}
        />
      )}
    </>
  )
}
