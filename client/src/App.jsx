// App shell: boot, session gating, and the two-pane layout (plan §3.5).
import { useEffect, useState } from 'react'
import { boot } from './boot.js'
import { kvGet, kvSet } from './db.js'
import StatusBar from './components/StatusBar.jsx'
import ContactList from './components/ContactList.jsx'
import ContactEntryForm from './components/ContactEntryForm.jsx'

const EMPTY_SESSION = { callsign: '', initials: '', band: '', mode: '' }

export default function App() {
  const [state, setState] = useState({ status: 'loading' })
  const [session, setSession] = useState(EMPTY_SESSION)

  useEffect(() => {
    ;(async () => {
      const result = await boot()
      const saved = await kvGet('session')
      if (saved) setSession({ ...EMPTY_SESSION, ...saved })
      setState(result)
    })()
  }, [])

  function updateSession(next) {
    setSession(next)
    kvSet('session', next)
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

  const { event, clientUuid, connected } = state
  const config = event.config
  const sessionComplete = Boolean(
    session.callsign.trim() && session.initials.trim() && session.band && session.mode,
  )

  return (
    <div className="app">
      <StatusBar
        eventName={event.name}
        session={session}
        onSession={updateSession}
        config={config}
        connected={connected}
      />
      <main className="panes">
        <section className="left-pane">
          <ContactList config={config} />
          <ContactEntryForm
            config={config}
            session={session}
            clientUuid={clientUuid}
            disabled={!sessionComplete}
          />
        </section>
        <aside className="right-pane">
          <div className="stations-panel">
            <h2>Stations</h2>
            <p className="placeholder">Presence arrives with the WebSocket milestone.</p>
          </div>
          <div className="chat-panel">
            <h2>Chat</h2>
            <p className="placeholder">Chat arrives with the WebSocket milestone.</p>
          </div>
          <div className="future-panel" />
        </aside>
      </main>
    </div>
  )
}
