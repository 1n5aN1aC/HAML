// The Logging tab: operator identity (StatusBar), the two-pane layout,
// and the contact edit modal. All data/state comes from App; only the
// modal's open/close state lives here, so switching tabs closes it.

import { useState } from 'react'
import StatusBar from './StatusBar.jsx'
import ContactList from './ContactList.jsx'
import ContactEntryForm from './ContactEntryForm.jsx'
import StationsPanel from './StationsPanel.jsx'
import ChatPanel from './ChatPanel.jsx'
import ContactModal from './ContactModal.jsx'

export default function LoggingTab({
  session,
  onSession,
  config,
  clientUuid,
  sessionComplete,
  stations,
  chat,
  onChatSend,
  onChatResend,
}) {
  const [editing, setEditing] = useState(null)

  return (
    <>
      <StatusBar session={session} onSession={onSession} config={config} />
      <main className="panes">
        <section className="left-pane">
          <ContactList config={config} onSelect={setEditing} />
          <ContactEntryForm
            config={config}
            session={session}
            clientUuid={clientUuid}
            disabled={!sessionComplete}
          />
        </section>
        <aside className="right-pane">
          <StationsPanel stations={stations} clientUuid={clientUuid} />
          <ChatPanel
            messages={chat}
            onSend={onChatSend}
            onResend={onChatResend}
            disabled={!session.callsign.trim() || !session.initials.trim()}
          />
          <div className="future-panel" />
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
