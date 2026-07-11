// The whole logging screen: StatusBar + two panes (contact list & entry form on
// the left; stations & chat on the right). The embedded ContactList reads a live
// Dexie query, so seed IndexedDB before mounting (same pattern as ContactList).
import { useEffect, useState } from 'react'
import Dexie from 'dexie'
import { LoggingTab } from 'haml-client'
import { config, session, clientUuid, stations, chat, contacts, DB_NAME, DB_STORES } from './_fixtures'

const noop = () => {}

export const Screen = () => {
  const [ready, setReady] = useState(false)
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const db = new Dexie(DB_NAME)
      db.version(1).stores(DB_STORES)
      await db.table('contacts').clear()
      await db.table('contacts').bulkPut(contacts)
      db.close()
      if (!cancelled) setReady(true)
    })()
    return () => {
      cancelled = true
    }
  }, [])
  if (!ready) return <div className="app" />
  return (
    <div className="app">
      <LoggingTab
        session={session}
        onSession={noop}
        config={config}
        clientUuid={clientUuid}
        sessionComplete={true}
        stations={stations}
        chat={chat}
        onChatSend={noop}
        onChatResend={noop}
      />
    </div>
  )
}
