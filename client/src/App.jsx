// App shell: boot, session gating, and the tabbed layout (plan §3.5).
// All background machinery (sync, socket, presence, chat) lives here so
// it keeps running no matter which tab is shown.

import { useEffect, useState } from 'react'
import { boot } from './boot.js'
import { kvGet, kvSet, exportRawEvent } from './db.js'
import { startSync, pullNow } from './sync.js'
import { startSocket, setPresence } from './socket.js'
import { loadChat, refreshChat, applyChatBroadcast, sendMessage, resendMessage, clearChat } from './chat.js'
import { validTheme } from './themes.js'
import { playChat } from './sounds.js'
import TopBar from './components/TopBar.jsx'
import LoggingTab from './components/logging/LoggingTab.jsx'
import StatsTab from './components/stats/StatsTab.jsx'
import SettingsTab from './components/settings/SettingsTab.jsx'
import AdminTab from './components/admin/AdminTab.jsx'

const OFF_AIR = 'Off-Air'

const EMPTY_SESSION = { callsign: '', initials: '', band: OFF_AIR, mode: '' }

export default function App() {
  const [state, setState] = useState({ status: 'loading' })
  const [session, setSession] = useState(EMPTY_SESSION)
  const [connected, setConnected] = useState(false)
  const [stations, setStations] = useState([])
  const [chat, setChat] = useState([])
  const [tab, setTab] = useState('logging')
  // Persisted to localStorage (independent of dexie)
  // Also applied in index.html on-load before dexie exists, to prevent a flash
  // on load. The theme list is auto-discovered from themes/*.css (themes.js);
  // unknown/retired ids fall back to the default.
  const [theme, setTheme] = useState(() => validTheme(localStorage.getItem('haml-theme')))

  // index.html applies the saved id unvalidated; this corrects a retired id.
  useEffect(() => {
    document.documentElement.dataset.theme = theme
  }, [theme])

  function changeTheme(id) {
    setTheme(id)
    localStorage.setItem('haml-theme', id)
  }

  useEffect(() => {
    ;(async () => {
      const result = await boot()
      const saved = await kvGet('session')
      if (saved) setSession({ ...EMPTY_SESSION, ...saved })
      setChat(await loadChat())
      setState(result)
    })()
  }, [])

  // The sync engine and the WebSocket signal layer run whenever we're on a
  // ready Event. The WebSocket owns the connection indicator; sync does
  // data transfer. The socket also drives presence, chat, pokes, and
  // event-switch detection (ADR-0005).
  useEffect(() => {
    if (state.status !== 'ready') return
    setConnected(state.connected)
    // Offline continuation after an event mismatch: keep logging locally
    // against the old event, but never sync or announce presence — the
    // server is on a different event now (ADR-0002).
    if (state.offline) return
    // The WebSocket owns the connection indicator; sync just does data
    // transfer. Sync push/pull failures no longer flicker the status.
    const stopSync = startSync()
    const stopSocket = startSocket({
      onConnect: async () => {
        setConnected(true)
        pullNow()
        setChat(await refreshChat())
      },
      onDisconnect: () => {
        setConnected(false)
        setStations([])
      },
      onPresence: setStations,
      onChat: async (message) => {
        playChat()
        setChat(await applyChatBroadcast(message))
      },
      onChatCleared: async () => setChat(await clearChat()),
      onPoke: pullNow,
      onEvent: async (eventUuid) => {
        // Server switched Events under us: re-run boot, which surfaces the
        // mismatch warning (ADR-0002) instead of silently mixing logs.
        if (eventUuid !== state.event.event_uuid) setState(await boot())
      },
    })
    return () => {
      stopSocket()
      stopSync()
    }
  }, [state])

  // Announce presence whenever a callsign is entered — immediately on change, then the socket heartbeats it every 5s. 
  // A blank callsign stops the heartbeat; the server ages the entry out via PRESENCE_TTL.
  useEffect(() => {
    if (state.status !== 'ready') return
    if (session.callsign) {
      setPresence({
        client_uuid: state.clientUuid,
        callsign: session.callsign.toUpperCase(),
        initials: session.initials.toUpperCase() || '-', // server requires non-blank
        band: session.band,
        mode: session.mode,
      })
    } else {
      setPresence(null)
    }
  }, [state, session])

  function updateSession(next) {
    setSession(next)
    kvSet('session', next)
  }

  async function handleChatSend(text) {
    setChat(await sendMessage({ text, session, clientUuid: state.clientUuid }))
  }

  async function handleChatResend(uuid) {
    setChat(await resendMessage(uuid))
  }

  async function acceptNewEvent() {
    setState({ status: 'loading' })
    setState(await boot({ acceptNewEvent: true }))
  }

  // Keep working on the old event without the server. Nothing is wiped;
  // reloading the page brings the mismatch screen back.
  function continueOffline() {
    setState({
      status: 'ready',
      event: state.cached,
      clientUuid: state.clientUuid,
      connected: false,
      offline: true,
    })
  }

  if (state.status === 'loading') {
    return <div className="screen">Connecting...</div>
  }
  if (state.status === 'no-server') {
    return (
      <div className="screen">
        <p>Cannot reach the HAML server, and no event is cached locally.</p>
        <button onClick={() => window.location.reload()}>Retry</button>
      </div>
    )
  }
  // Server is up but has no active event (fresh install, or every event was
  // deleted): only the admin panel is useful, so show just that. Creating or
  // activating an event re-runs boot — via the cached-event mismatch screen
  // when stale local data needs the operator's wipe confirmation first.
  if (state.status === 'no-event') {
    return (
      <div className="app">
        <p className="admin-notice no-event-banner">
          The server has no active event — create or activate one below.
        </p>
        <AdminTab onEventChange={async () => setState(await boot())} />
      </div>
    )
  }
  if (state.status === 'mismatch') {
    return (
      <div className="screen">
        <p>
          The server is now running a different event:{' '}
          <strong>{state.event.name}</strong> (was: {state.cached.name}).
        </p>
        <p>
          Switching will <strong>erase all local data</strong> from the old event.
          Contacts not yet synced will be lost.
        </p>
        <div className="screen-actions">
          <button onClick={acceptNewEvent}>Switch to {state.event.name}</button>
          <button className="btn-secondary" onClick={continueOffline}>
            Continue offline with {state.cached.name}
          </button>
          <button className="btn-secondary" onClick={exportRawEvent}>
            Export local data
          </button>
        </div>
      </div>
    )
  }

  const { event, clientUuid } = state
  const config = event.config
  const sessionComplete = Boolean(
    session.callsign.trim() &&
      session.initials.trim() &&
      session.band !== OFF_AIR &&
      session.mode,
  )

  return (
    <div className="app">
      <TopBar
        connected={connected}
        eventName={event.name}
        activeTab={tab}
        onTab={setTab}
        theme={theme}
        onTheme={changeTheme}
      />
      {tab === 'logging' && (
        <LoggingTab
          session={session}
          onSession={updateSession}
          config={config}
          exchange={event.local_exchange || event.station_callsign}
          clientUuid={clientUuid}
          sessionComplete={sessionComplete}
          stations={stations}
          chat={chat}
          onChatSend={handleChatSend}
          onChatResend={handleChatResend}
        />
      )}
      {tab === 'stats' && <StatsTab />}
      {tab === 'settings' && (
        <SettingsTab
          config={config}
          event={event}
          session={session}
          clientUuid={clientUuid}
        />
      )}
      {tab === 'admin' && <AdminTab />}
    </div>
  )
}
