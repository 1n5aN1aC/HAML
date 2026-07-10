// App shell: boot, session gating, and the tabbed layout (plan §3.5).
// All background machinery (sync, socket, presence, chat) lives here so
// it keeps running no matter which tab is shown.

import { useEffect, useState } from 'react'
import { boot } from './boot.js'
import { kvGet, kvSet } from './db.js'
import { startSync, pullNow } from './sync.js'
import { startSocket, setPresence } from './socket.js'
import { loadChat, refreshChat, applyChatBroadcast, sendMessage, resendMessage } from './chat.js'
import TopBar from './components/TopBar.jsx'
import LoggingTab from './components/LoggingTab.jsx'
import RadioTab from './components/RadioTab.jsx'
import AdminTab from './components/AdminTab.jsx'

const OFF_AIR = 'Off-Air'

const EMPTY_SESSION = { callsign: '', initials: '', band: OFF_AIR, mode: '' }

export default function App() {
  const [state, setState] = useState({ status: 'loading' })
  const [session, setSession] = useState(EMPTY_SESSION)
  const [connected, setConnected] = useState(false)
  const [stations, setStations] = useState([])
  const [chat, setChat] = useState([])
  const [tab, setTab] = useState('logging')

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
      onChat: async (message) => setChat(await applyChatBroadcast(message)),
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

  // Announce identity/band/mode as presence once the session is complete —
  // immediately on change, then the socket heartbeats it every 5s.
  useEffect(() => {
    if (state.status !== 'ready') return
    const complete =
      session.callsign.trim() && session.initials.trim() && session.band && session.mode
    if (complete) {
      setPresence({
        client_uuid: state.clientUuid,
        callsign: session.callsign.trim().toUpperCase(),
        initials: session.initials.trim().toUpperCase(),
        band: session.band,
        mode: session.mode,
      })
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

  if (state.status === 'loading') {
    return <div className="screen">Connecting…</div>
  }
  if (state.status === 'no-server') {
    return (
      <div className="screen">
        <p>Cannot reach the HAML server, and no event is cached locally.</p>
        <button onClick={() => window.location.reload()}>Retry</button>
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
        <button onClick={acceptNewEvent}>Switch to {state.event.name}</button>
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
      />
      {tab === 'logging' && (
        <LoggingTab
          session={session}
          onSession={updateSession}
          config={config}
          clientUuid={clientUuid}
          sessionComplete={sessionComplete}
          stations={stations}
          chat={chat}
          onChatSend={handleChatSend}
          onChatResend={handleChatResend}
        />
      )}
      {tab === 'radio' && <RadioTab />}
      {tab === 'admin' && <AdminTab />}
    </div>
  )
}
