import { useEffect, useState } from 'react'
import Dexie from 'dexie'
import { ContactList } from 'haml-client'
import { config, contacts, DB_NAME, DB_STORES } from './_fixtures'

// ContactList reads a live Dexie query over IndexedDB 'haml'; seed the store
// (then close our connection) before mounting so the first query returns rows.
function useSeededContacts(rows: unknown[]) {
  const [ready, setReady] = useState(false)
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const db = new Dexie(DB_NAME)
      db.version(1).stores(DB_STORES)
      await db.table('contacts').clear()
      await db.table('contacts').bulkPut(rows)
      db.close()
      if (!cancelled) setReady(true)
    })()
    return () => {
      cancelled = true
    }
  }, [])
  return ready
}

export const Populated = () => {
  const ready = useSeededContacts(contacts)
  if (!ready) return <div className="contact-list" />
  return <ContactList config={config} onSelect={() => {}} />
}
